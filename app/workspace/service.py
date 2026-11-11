"""OAuth execution boundary. Reads and previews cannot dispatch mutations."""
import base64
import hashlib
import hmac
import json
import secrets
import time
from email.message import EmailMessage
from urllib.parse import urlsplit

import httpx

from app.workspace.catalog import build_url, catalog, is_authorized, validate_call

MAX_MEDIA_BYTES = 8 * 1024 * 1024


class WorkspaceError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class WorkspaceService:
    def __init__(self, google_client, signing_key, transport=None):
        self.google = google_client
        self.key = signing_key.encode()
        self.transport = transport

    def authorize(self, user_id, service, method):
        connection = self.google.get_user_google_connection(user_id)
        if not connection or connection.status == "disconnected":
            raise WorkspaceError("NOT_CONNECTED", "Chưa liên kết Google. Vui lòng mở Portal để kết nối.")
        if connection.status != "connected":
            raise WorkspaceError("RECONNECT_REQUIRED", "Cần kết nối lại Google trên Portal.")
        if not is_authorized(service, method, (connection.scopes or "").split()):
            raise WorkspaceError("PERMISSION_DENIED", f"Chưa cấp quyền cho {service}.{method}. Kết nối lại Google và cấp quyền ứng dụng này.")

    def _normalize(self, service, kind, payload):
        if not isinstance(payload, dict) or set(payload) - {"method", "parameters", "body", "media", "mail", "_ticket"}:
            raise ValueError("Unknown Workspace request fields")
        method = payload.get("method", "")
        body = dict(payload.get("body") or {})
        mail = payload.get("mail")
        if mail is not None:
            if service != "gmail" or kind != "write" or method not in {"users.messages.send", "users.drafts.create", "users.drafts.update"}:
                raise ValueError("mail is only supported by Gmail send and draft create/update")
            if not isinstance(mail, dict) or set(mail) - {"to", "cc", "bcc", "subject", "text", "html", "in_reply_to", "references", "attachments"}:
                raise ValueError("Invalid mail fields")
            message = EmailMessage()
            for field, header in (("to", "To"), ("cc", "Cc"), ("bcc", "Bcc"), ("subject", "Subject"), ("in_reply_to", "In-Reply-To"), ("references", "References")):
                if mail.get(field):
                    value = mail[field]
                    message[header] = ", ".join(value) if isinstance(value, list) else value
            message.set_content(mail.get("text", ""))
            if mail.get("html"):
                message.add_alternative(mail["html"], subtype="html")
            for attachment in mail.get("attachments", []):
                data = self._decode_media(attachment)
                mime = attachment.get("mime_type", "application/octet-stream").split("/", 1)
                message.add_attachment(data, maintype=mime[0], subtype=mime[1], filename=attachment.get("filename", "attachment"))
            mime_bytes = message.as_bytes()
            if len(mime_bytes) > MAX_MEDIA_BYTES:
                raise ValueError('Email MIME exceeds 8 MiB')
            raw = base64.urlsafe_b64encode(mime_bytes).decode()
            if method == "users.messages.send":
                body["raw"] = raw
            else:
                body.setdefault("message", {})["raw"] = raw
        spec, params, body = validate_call(service, kind, method, payload.get("parameters"), body)
        media = payload.get("media")
        if media is not None:
            if kind != "write" or not spec.get("mediaUpload"):
                raise ValueError("This method does not support upload")
            self._decode_media(media)
        return spec, params, body

    def _decode_media(self, media):
        if not isinstance(media, dict) or set(media) - {"data_base64", "mime_type", "filename"}:
            raise ValueError("Invalid media fields")
        encoded = media.get("data_base64", "")
        if not isinstance(encoded, str) or len(encoded) > MAX_MEDIA_BYTES * 4 // 3 + 4:
            raise ValueError("Media exceeds 8 MiB")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("Invalid base64 media") from exc
        if len(data) > MAX_MEDIA_BYTES:
            raise ValueError("Media exceeds 8 MiB")
        mime = media.get("mime_type", "application/octet-stream")
        if not isinstance(mime, str) or "/" not in mime or "\r" in mime or "\n" in mime:
            raise ValueError("Invalid MIME type")
        return data

    def _send(self, user_id, service, spec, params, body, media=None):
        url = build_url(service, spec, params, upload=media is not None)
        host = urlsplit(url).hostname or ""
        if urlsplit(url).scheme != "https" or not (host == "www.googleapis.com" or host.endswith(".googleapis.com")):
            raise ValueError("Untrusted Google API host")
        query = {key: value for key, value in params.items() if spec.get("parameters", {}).get(key, {}).get("location") != "path"}
        headers = {"Authorization": "Bearer " + self.google.oauth_service.get_valid_access_token(user_id)}
        kwargs = {"params": query, "headers": headers}
        if media is not None:
            raw = self._decode_media(media)
            boundary = "workspace_" + secrets.token_hex(12)
            headers["Content-Type"] = f"multipart/related; boundary={boundary}"
            query["uploadType"] = "multipart"
            kwargs["content"] = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
                                 + json.dumps(body, ensure_ascii=False).encode() + f"\r\n--{boundary}\r\nContent-Type: {media.get('mime_type', 'application/octet-stream')}\r\n\r\n".encode()
                                 + raw + f"\r\n--{boundary}--\r\n".encode())
        elif body or "request" in spec:
            kwargs["json"] = body
        try:
            with httpx.Client(timeout=9.0, follow_redirects=False, transport=self.transport) as client:
                with client.stream(spec["httpMethod"], url, **kwargs) as response:
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > MAX_MEDIA_BYTES:
                            raise WorkspaceError("UNKNOWN_RESULT" if spec["kind"] == "write" else "RESPONSE_TOO_LARGE", "Kết quả vượt 8 MiB. Chọn phạm vi nhỏ hơn hoặc tải trực tiếp trên Google.")
                    if response.status_code >= 500:
                        code = "UNKNOWN_RESULT" if spec["kind"] == "write" else "SERVICE_ERROR"
                        raise WorkspaceError(code, f"Google tạm thời lỗi ({response.status_code}); không tự gửi lại thao tác ghi.")
                    if response.status_code >= 300:
                        codes = {401: "RECONNECT_REQUIRED", 403: "PERMISSION_DENIED", 404: "NOT_FOUND", 409: "CONFLICT", 412: "CONFLICT", 429: "RATE_LIMIT"}
                        raise WorkspaceError(codes.get(response.status_code, "SERVICE_ERROR"), f"Google trả HTTP {response.status_code}. Kiểm tra API đã bật, quyền tài khoản và thông tin yêu cầu.")
                    if not raw:
                        return {}
                    download = params.get("alt") == "media" or (spec.get("supportsMediaDownload") and "response" not in spec)
                    if not download and "json" in response.headers.get("content-type", ""):
                        return json.loads(raw)
                    return {"content_base64": base64.b64encode(raw).decode(), "mime_type": response.headers.get("content-type", "application/octet-stream"), "size_bytes": len(raw)}
        except (httpx.RequestError, json.JSONDecodeError) as exc:
            code = "UNKNOWN_RESULT" if spec["kind"] == "write" else "SERVICE_ERROR"
            raise WorkspaceError(code, "Kết nối Google gián đoạn; chưa rõ kết quả thao tác ghi." if code == "UNKNOWN_RESULT" else "Không thể kết nối Google.") from exc

    def read(self, user_id, service, payload):
        spec, params, body = self._normalize(service, "read", payload)
        self.authorize(user_id, service, payload["method"])
        return {"status": "OK", "data": self._send(user_id, service, spec, params, body)}

    def _snapshot(self, user_id, service, method, params):
        candidate = method.rsplit(".", 1)[0] + ".get"
        target = catalog()[service]["methods"].get(candidate)
        if not target or target["kind"] != "read":
            return None
        required = [k for k, v in target.get("parameters", {}).items() if v.get("required")]
        if not all(key in params for key in required):
            return None
        snapshot_params = {k: v for k, v in params.items() if k in target.get("parameters", {}) and k not in {"fields", "alt"}}
        if service == "contacts":
            snapshot_params.setdefault("personFields", "names,emailAddresses,phoneNumbers,metadata")
        self.authorize(user_id, service, candidate)
        spec, snapshot_params, body = validate_call(service, "read", candidate, snapshot_params, {})
        data = self._send(user_id, service, spec, snapshot_params, body)
        return {"method": candidate, "parameters": snapshot_params, "hash": digest(data), "revision_id": data.get("revisionId") if isinstance(data, dict) else None}

    def _sign(self, payload):
        if not self.key:
            raise WorkspaceError("MISCONFIGURED", "Chưa cấu hình khóa mã hóa Gateway.")
        raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
        return raw + "." + hmac.new(self.key, raw.encode(), hashlib.sha256).hexdigest()

    def prepare(self, user_id, service, payload):
        if "_ticket" in payload:
            raise ValueError("Preview ticket cannot be provided by caller")
        _, params, _ = self._normalize(service, "write", payload)
        self.authorize(user_id, service, payload["method"])
        snapshot = self._snapshot(user_id, service, payload["method"], params)
        ticket = self._sign({"user": user_id, "service": service, "hash": digest(payload), "expires": time.time() + 300, "snapshot": snapshot})
        return {"status": "OK", "arguments": {**payload, "_ticket": ticket}, "snapshot": snapshot}

    def download_ticket(self, user_id, service, payload):
        return self._sign({"kind": "download", "user": user_id, "service": service, "request": payload, "expires": time.time() + 300})

    def download(self, user_id, ticket):
        try:
            raw, signature = ticket.rsplit(".", 1)
            expected = hmac.new(self.key, raw.encode(), hashlib.sha256).hexdigest()
            if len(ticket) > 20000 or not self.key or not hmac.compare_digest(signature, expected):
                raise ValueError("Invalid signature")
            payload = json.loads(base64.urlsafe_b64decode(raw))
            if payload.get("kind") != "download" or payload["user"] != user_id or payload["expires"] < time.time():
                raise ValueError("Wrong user or expired download")
        except (ValueError, KeyError, TypeError) as exc:
            raise WorkspaceError("INVALID_DOWNLOAD", "Liên kết tải không hợp lệ hoặc hết hạn.") from exc
        return self.read(user_id, payload["service"], payload["request"])

    def execute(self, user_id, service, arguments):
        payload = {k: v for k, v in arguments.items() if k != "_ticket"}
        ticket = arguments.get("_ticket", "")
        try:
            raw, signature = ticket.rsplit(".", 1)
            expected = hmac.new(self.key, raw.encode(), hashlib.sha256).hexdigest()
            if not self.key or not hmac.compare_digest(signature, expected):
                raise ValueError("Invalid signature")
            signed = json.loads(base64.urlsafe_b64decode(raw))
            if signed["user"] != user_id or signed["service"] != service or signed["hash"] != digest(payload) or signed["expires"] < time.time():
                raise ValueError("Preview changed or expired")
        except (ValueError, KeyError, TypeError) as exc:
            raise WorkspaceError("INVALID_PREVIEW", "Bản xem trước không hợp lệ hoặc hết hạn. Vui lòng tạo lại.") from exc
        spec, params, body = self._normalize(service, "write", payload)
        self.authorize(user_id, service, payload["method"])
        snapshot = signed.get("snapshot")
        if snapshot:
            current = self._snapshot(user_id, service, payload["method"], params)
            if not current or current["hash"] != snapshot["hash"]:
                raise WorkspaceError("CONFLICT", "Dữ liệu đã đổi từ lúc xem trước. Vui lòng tạo yêu cầu mới.")
            if service in {"docs", "slides"} and snapshot.get("revision_id"):
                body = {**body, "writeControl": {"requiredRevisionId": snapshot["revision_id"]}}
        return {"status": "OK", "data": self._send(user_id, service, spec, params, body, payload.get("media"))}
