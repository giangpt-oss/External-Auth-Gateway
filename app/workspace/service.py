"""OAuth execution boundary. Reads and previews cannot dispatch mutations."""
import base64
import hashlib
import hmac
import json
import random
import secrets
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from threading import Lock
from urllib.parse import urlsplit

import httpx
from sqlalchemy.orm import sessionmaker

from app.workspace.catalog import build_url, catalog, is_authorized, validate_call
from app.workspace.google_mcp import translate
from app.workspace.network_slots import outbound_slot
from app.workspace.selection_manager import SelectionManager, SwitchLockError, current_account_pin
from app.workspace.telemetry import measure_stage

MAX_MEDIA_BYTES = 8 * 1024 * 1024


class WorkspaceError(ValueError):
    def __init__(self, code, message, *, retry_after=None, provider=None):
        self.code = code
        self.retry_after = retry_after
        self.provider = provider
        super().__init__(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class WorkspaceService:
    def __init__(self, google_client, signing_key, transport=None, *, use_mcp=False, http_client=None, router=None, circuit_breaker=None):
        self.google = google_client
        self.key = signing_key.encode()
        self.transport = transport
        from app.workspace.circuit_breaker import AccountServiceCircuitBreaker
        from app.workspace.protocol_router import ProtocolRouter
        self.circuit_breaker = circuit_breaker or AccountServiceCircuitBreaker()
        self.router = router or ProtocolRouter(circuit_breaker=self.circuit_breaker, mcp_enabled=use_mcp)
        self.use_mcp = use_mcp

        # Persistent synchronous HTTP connection pool (connect_timeout 3s, pool_timeout 2s, HTTP/2)
        if isinstance(http_client, httpx.Client):
            self._shared_client = http_client
            self._owns_client = False
        elif transport is not None:
            self._shared_client = httpx.Client(
                timeout=httpx.Timeout(9.0, connect=3.0, pool=2.0),
                limits=httpx.Limits(max_keepalive_connections=20, max_connections=50, keepalive_expiry=30.0),
                transport=transport,
            )
            self._owns_client = True
        elif getattr(google_client, "_http_pool", None) and isinstance(google_client._http_pool, httpx.Client):
            self._shared_client = google_client._http_pool
            self._owns_client = False
        else:
            self._shared_client = httpx.Client(
                http2=True,
                timeout=httpx.Timeout(9.0, connect=3.0, pool=2.0),
                limits=httpx.Limits(max_keepalive_connections=20, max_connections=50, keepalive_expiry=30.0),
            )
            self._owns_client = True

        self._mcp_clients = {}
        self._mcp_lock = Lock()

    def get_mcp_client(self, service: str, user_id: int, connection_version: int, account_id: int | None = None):
        key = (service, user_id, account_id)
        with self._mcp_lock:
            current = self._mcp_clients.get(key)
            if current and current[0] == connection_version:
                return current[1]
            from app.workspace.mcp_client import MCPSyncClient

            client = MCPSyncClient(service=service, http_client=self._shared_client)
            self._mcp_clients[key] = (connection_version, client)
            if len(self._mcp_clients) > 512:
                self._mcp_clients.pop(next(iter(self._mcp_clients)))
            return client

    def close(self):
        if getattr(self, "_owns_client", False) and hasattr(self, "_shared_client") and not self._shared_client.is_closed:
            self._shared_client.close()

    @property
    def use_mcp(self):
        return getattr(self, "_use_mcp", False)

    @use_mcp.setter
    def use_mcp(self, value):
        self._use_mcp = bool(value)
        if hasattr(self, "router") and hasattr(self.router, "mcp_enabled"):
            self.router.mcp_enabled = self._use_mcp

    @measure_stage("permission_check_ms")
    def authorize(self, user_id, service, method, account_id=None):
        pin = current_account_pin(user_id)
        if pin is not None and account_id is None:
            account_id = pin.account_id
        connection = self.google.get_user_google_connection(user_id, account_id=account_id)
        if not connection or connection.status == "disconnected":
            raise WorkspaceError("NOT_CONNECTED", "Chưa liên kết Google. Vui lòng mở Portal để kết nối.")
        if connection.status != "connected":
            raise WorkspaceError("RECONNECT_REQUIRED", "Cần kết nối lại Google trên Portal.")
        if not is_authorized(service, method, (connection.scopes or "").split()):
            raise WorkspaceError("PERMISSION_DENIED", f"Chưa cấp quyền cho {service}.{method}. Kết nối lại Google và cấp quyền ứng dụng này.")
        return connection

    @contextmanager
    def _execution_scope(self, user_id, account_id=None, selection_version=None, *, write=False):
        factory = getattr(self.google.oauth_service, "session_factory", None)
        if not isinstance(factory, sessionmaker):
            yield None
            return
        try:
            with SelectionManager.admit_activity(
                factory, user_id, account_id=account_id, expected_version=selection_version,
                has_outbound_write=write,
            ) as pin:
                yield pin
        except SwitchLockError as exc:
            raise WorkspaceError(exc.code, exc.message) from exc

    def _access_token(self, user_id, account_id, *, force_refresh=False):
        options = {"google_account_id": account_id} if account_id is not None else {}
        if force_refresh:
            options["force_refresh"] = True
        return self.google.oauth_service.get_valid_access_token(user_id, **options)

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

    def _send(self, user_id, service, method, spec, params, body, media=None, *, force_rest=False, account_id=None):
        from app.workspace.network_slots import NetworkSlotUnavailable, request_deadline

        token = None
        deadline = request_deadline.get() or (time.monotonic() + 30)
        writes = spec["kind"] == "write"
        for attempt in range(3 if not writes else 1):
            if time.monotonic() >= deadline:
                raise WorkspaceError("DEADLINE_EXCEEDED", "Workspace request deadline expired.")
            try:
                return self._send_once(user_id, service, method, spec, params, body, media,
                                       force_rest=force_rest, token=token, deadline=deadline, account_id=account_id)
            except NetworkSlotUnavailable as exc:
                expired = time.monotonic() >= deadline
                code = "DEADLINE_EXCEEDED" if expired else "RATE_LIMIT"
                raise WorkspaceError(code, str(exc)) from exc
            except WorkspaceError as exc:
                if writes or attempt == 2:
                    raise
                if exc.code == "RECONNECT_REQUIRED" and token is None:
                    token = self._access_token(user_id, account_id, force_refresh=True)
                    continue
                if exc.code not in {"SERVICE_ERROR", "RATE_LIMIT", "QUOTA_EXCEEDED"}:
                    raise
                if exc.provider == "mcp" and exc.code == "SERVICE_ERROR" and not params.get("pageToken"):
                    force_rest = True
                delay = exc.retry_after if exc.retry_after is not None else 0.5 * 2 ** attempt + random.uniform(0, 0.25)
                if time.monotonic() + delay >= deadline:
                    raise
                time.sleep(delay)

    def _send_once(self, user_id, service, method, spec, params, body, media=None, *, force_rest, token, deadline, account_id=None):
        from app.workspace.adapters import decode_cursor
        from app.workspace.network_slots import NetworkSlotUnavailable
        from app.workspace.protocol_router import ProtocolChoice
        if hasattr(self.router, "mcp_enabled"):
            self.router.mcp_enabled = self.use_mcp
        protocol_owner = (user_id, account_id) if account_id is not None else user_id
        choice = (ProtocolChoice.REST if force_rest else
                  self.router.select_protocol(service, method, params, body, media, user_id=protocol_owner))
        cursor = params.get("pageToken")
        if isinstance(cursor, str) and cursor.startswith("ws:"):
            try:
                _, page = decode_cursor(cursor[3:], expected_protocol=choice.value)
            except ValueError as exc:
                raise WorkspaceError("INVALID_CURSOR", "Hãy bắt đầu lại phân trang khi đổi giao thức.") from exc
            params = {**params, "pageToken": page}

        if choice == ProtocolChoice.MCP:
            mcp_call = translate(service, method, params, body, media)
            if mcp_call:
                access_token = token or self._access_token(user_id, account_id)
                connection = self.google.get_user_google_connection(user_id, account_id=account_id)
                conn_ver = getattr(connection, "connection_version", 1) if connection else 1
                conn_id = getattr(connection, "id", None)
                if not isinstance(conn_id, int):
                    conn_id = account_id
                mcp_client = self.get_mcp_client(service, user_id, conn_ver, account_id=conn_id)
                try:
                    with outbound_slot(user_id, write=spec["kind"] == "write"), measure_stage("external_call_ms", protocol="mcp"):
                        result = mcp_client.call_tool(
                            token=access_token,
                            name=mcp_call[0],
                            arguments=mcp_call[1],
                            user_id=user_id,
                            connection_version=conn_ver,
                        )
                        self.circuit_breaker.record_success(protocol_owner, service)
                        return self._tag_cursor(result, "mcp")
                except NetworkSlotUnavailable:
                    raise
                except Exception as exc:
                    status_code = getattr(exc, "status_code", None)
                    # 401 (auth), 403 (permissions), 429 (rate limits) are client/account/quota errors,
                    # not MCP infrastructure outages. Do NOT trip the circuit breaker for these!
                    if status_code is None or status_code >= 500:
                        self.circuit_breaker.record_failure(protocol_owner, service)
                    code = {401: "RECONNECT_REQUIRED", 403: "PERMISSION_DENIED", 429: "RATE_LIMIT"}.get(
                        status_code, "INVALID_REQUEST" if status_code is not None and status_code < 500
                        else "UNKNOWN_RESULT" if spec["kind"] == "write" else "SERVICE_ERROR"
                    )
                    raise WorkspaceError(code, "Google Workspace MCP không trả kết quả thành công.", provider="mcp") from exc
        url = build_url(service, spec, params, upload=media is not None)
        host = urlsplit(url).hostname or ""
        if urlsplit(url).scheme != "https" or not (host == "www.googleapis.com" or host.endswith(".googleapis.com")):
            raise ValueError("Untrusted Google API host")
        query = {key: value for key, value in params.items() if spec.get("parameters", {}).get(key, {}).get("location") != "path"}
        headers = {"Authorization": "Bearer " + (token or self._access_token(user_id, account_id))}
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
            client = self._shared_client
            with outbound_slot(user_id, write=spec["kind"] == "write"), measure_stage("external_call_ms", protocol="rest"):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkspaceError("DEADLINE_EXCEEDED", "Workspace request deadline expired.")
                kwargs["timeout"] = httpx.Timeout(min(9.0, remaining), connect=min(3.0, remaining), pool=min(2.0, remaining))
                return self._tag_cursor(self._read_response(client, spec, url, params, kwargs, deadline), "rest")
        except (httpx.RequestError, json.JSONDecodeError) as exc:
            code = "UNKNOWN_RESULT" if spec["kind"] == "write" else "SERVICE_ERROR"
            raise WorkspaceError(code, "Kết nối Google gián đoạn; chưa rõ kết quả thao tác ghi." if code == "UNKNOWN_RESULT" else "Không thể kết nối Google.") from exc

    @staticmethod
    def _tag_cursor(result, protocol):
        from app.workspace.adapters import encode_cursor

        if isinstance(result, dict) and isinstance(result.get("nextPageToken"), str) and result["nextPageToken"]:
            return {**result, "nextPageToken": "ws:" + encode_cursor(protocol, result["nextPageToken"])}
        return result

    def _read_response(self, client, spec, url, params, kwargs, deadline):
        with client.stream(spec["httpMethod"], url, **kwargs) as response:
            raw = bytearray()
            for chunk in response.iter_bytes():
                if time.monotonic() >= deadline:
                    code = "UNKNOWN_RESULT" if spec["kind"] == "write" else "DEADLINE_EXCEEDED"
                    raise WorkspaceError(code, "Workspace request deadline expired.")
                raw.extend(chunk)
                if len(raw) > MAX_MEDIA_BYTES:
                    raise WorkspaceError("UNKNOWN_RESULT" if spec["kind"] == "write" else "RESPONSE_TOO_LARGE", "Kết quả vượt 8 MiB. Chọn phạm vi nhỏ hơn hoặc tải trực tiếp trên Google.")
            if response.status_code >= 500:
                code = "UNKNOWN_RESULT" if spec["kind"] == "write" else "SERVICE_ERROR"
                raise WorkspaceError(code, f"Google tạm thời lỗi ({response.status_code}); không tự gửi lại thao tác ghi.")
            if response.status_code >= 300:
                codes = {401: "RECONNECT_REQUIRED", 403: "PERMISSION_DENIED", 404: "NOT_FOUND", 409: "CONFLICT", 412: "CONFLICT", 429: "RATE_LIMIT"}
                code = codes.get(response.status_code, "INVALID_REQUEST")
                if response.status_code == 403:
                    details = raw.decode("utf-8", "replace").lower()
                    if any(reason in details for reason in ("quotaexceeded", "ratelimitexceeded", "dailylimitexceeded")):
                        code = "QUOTA_EXCEEDED"
                    elif "insufficientpermissions" in details or "insufficient authentication scopes" in details:
                        code = "MISSING_SCOPE"
                    elif "accessnotconfigured" in details or "service_disabled" in details:
                        code = "SERVICE_DISABLED"
                retry_after = None
                retry_header = response.headers.get("Retry-After")
                if retry_header:
                    try:
                        retry_after = max(0, float(retry_header))
                    except ValueError:
                        try:
                            retry_after = max(0, (parsedate_to_datetime(retry_header) - datetime.now(UTC)).total_seconds())
                        except (ValueError, TypeError, OverflowError):
                            pass
                raise WorkspaceError(code, f"Google trả HTTP {response.status_code}. Kiểm tra API đã bật, quyền tài khoản và thông tin yêu cầu.", retry_after=retry_after)
            if not raw:
                return {}
            download = params.get("alt") == "media" or (spec.get("supportsMediaDownload") and "response" not in spec)
            if not download and "json" in response.headers.get("content-type", ""):
                return json.loads(raw)
            return {"content_base64": base64.b64encode(raw).decode(), "mime_type": response.headers.get("content-type", "application/octet-stream"), "size_bytes": len(raw)}

    def read(self, user_id, service, payload, *, force_rest=False, account_id=None):
        spec, params, body = self._normalize(service, "read", payload)
        with self._execution_scope(user_id, account_id) as pin:
            account_id = pin.account_id if pin else account_id
            self.authorize(user_id, service, payload["method"], account_id=account_id)
            options = {"force_rest": True} if force_rest else {}
            return {"status": "OK", "data": self._send(user_id, service, payload["method"], spec, params, body, account_id=account_id, **options)}

    def _snapshot(self, user_id, service, method, params, account_id=None):
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
        self.authorize(user_id, service, candidate, account_id=account_id)
        spec, snapshot_params, body = validate_call(service, "read", candidate, snapshot_params, {})
        data = self._send(user_id, service, candidate, spec, snapshot_params, body, account_id=account_id)
        return {"method": candidate, "parameters": snapshot_params, "hash": digest(data), "revision_id": data.get("revisionId") if isinstance(data, dict) else None}

    def _sign(self, payload):
        if not self.key:
            raise WorkspaceError("MISCONFIGURED", "Chưa cấu hình khóa mã hóa Gateway.")
        raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
        return raw + "." + hmac.new(self.key, raw.encode(), hashlib.sha256).hexdigest()

    def prepare(self, user_id, service, payload, account_id=None):
        if "_ticket" in payload:
            raise ValueError("Preview ticket cannot be provided by caller")
        _, params, _ = self._normalize(service, "write", payload)
        with self._execution_scope(user_id, account_id) as pin:
            connection = self.authorize(user_id, service, payload["method"], account_id=account_id)
            conn_id = pin.account_id if pin else getattr(connection, "id", None)
            conn_email = getattr(connection, "email", None)
            conn_id = conn_id if isinstance(conn_id, int) else None
            conn_email = conn_email if isinstance(conn_email, str) else None
            snapshot = self._snapshot(user_id, service, payload["method"], params, account_id=conn_id)
            selection_ver = pin.selection_version if pin else 1

        ticket = self._sign({
            "user": user_id,
            "service": service,
            "hash": digest(payload),
            "expires": time.time() + 300,
            "snapshot": snapshot,
            "account_id": conn_id,
            "selection_version": selection_ver,
        })
        return {
            "status": "OK",
            "arguments": {**payload, "_ticket": ticket},
            "snapshot": snapshot,
            "executing_account": {
                "id": conn_id,
                "email": conn_email,
                "selection_version": selection_ver,
            },
        }

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

        account_id_from_ticket = signed.get("account_id")
        ver_from_ticket = signed.get("selection_version")
        spec, params, body = self._normalize(service, "write", payload)
        with self._execution_scope(user_id, account_id_from_ticket, ver_from_ticket, write=True) as pin:
            account_id = pin.account_id if pin else account_id_from_ticket
            self.authorize(user_id, service, payload["method"], account_id=account_id)
            snapshot = signed.get("snapshot")
            if snapshot:
                current = self._snapshot(user_id, service, payload["method"], params, account_id=account_id)
                if not current or current["hash"] != snapshot["hash"]:
                    raise WorkspaceError("CONFLICT", "Dữ liệu đã đổi từ lúc xem trước. Vui lòng tạo yêu cầu mới.")
                if service in {"docs", "slides"} and snapshot.get("revision_id"):
                    body = {**body, "writeControl": {"requiredRevisionId": snapshot["revision_id"]}}
            return {"status": "OK", "data": self._send(user_id, service, payload["method"], spec, params, body, payload.get("media"), account_id=account_id)}
