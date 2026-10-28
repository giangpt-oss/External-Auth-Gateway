import hashlib
import json
import logging
import secrets
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from app.google.client import GoogleClient
from app.google.oauth import GoogleOAuthService
from app.internal.cache import identity_cache
from app.internal.security import verify_agent_secret
from app.models import ActionOperation, TelegramConnection, User
from app.odoo.client import (
    OdooAccessError,
    OdooAccountInactiveError,
    OdooAuthError,
    OdooClient,
    OdooExternalUserError,
    OdooNetworkError,
)
from app.odoo.service import OdooService
from app.security import decrypt_secret

_logger = logging.getLogger(__name__)


def _verify_odoo_account(odoo_svc: OdooService | None, uid: int, secret: str) -> dict[str, Any]:
    """Hàm dùng chung kiểm tra tài khoản Odoo trước khi cấp quyền hoặc thực hiện API nghiệp vụ."""
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "ODOO_CREDENTIAL_INVALID", "message": "Thông tin xác thực Odoo bị thiếu hoặc đã bị thu hồi. Vui lòng đăng nhập lại qua portal."},
        )
    if not odoo_svc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "ODOO_UNAVAILABLE", "message": "Không thể kết nối đến máy chủ Odoo."},
        )
    try:
        return odoo_svc.validate_user_account(uid=uid, secret=secret)
    except OdooAccountInactiveError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "ODOO_ACCOUNT_INACTIVE", "message": str(e)},
        ) from e
    except OdooExternalUserError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "EXTERNAL_USER_FORBIDDEN", "message": str(e)},
        ) from e
    except (OdooAuthError, OdooAccessError) as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "ODOO_CREDENTIAL_INVALID", "message": f"Xác thực Odoo thất bại: {e}"},
        ) from e
    except OdooNetworkError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "ODOO_UNAVAILABLE", "message": f"Mất kết nối hoặc quá hạn chờ Odoo: {e}"},
        ) from e
    except Exception as e:
        err_msg = str(e).lower()
        if "timeout" in err_msg or "connection" in err_msg:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "ODOO_UNAVAILABLE", "message": f"Lỗi kết nối Odoo: {e}"},
            ) from e
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "ODOO_CREDENTIAL_INVALID", "message": f"Không thể xác minh tài khoản Odoo: {e}"},
        ) from e

router = APIRouter(
    prefix="/internal/v1",
    tags=["Internal Integration"],
    dependencies=[Depends(verify_agent_secret)],
)


class IdentityResolveRequest(BaseModel):
    telegram_user_id: int
    chat_id: int
    contract_version: int = 1


def _get_google_client(request: Request) -> GoogleClient:
    settings = request.app.state.settings
    session_factory = request.app.state.session_factory
    redirect_uri = f"{settings.PUBLIC_BASE_URL.rstrip('/')}/oauth/google/callback"
    oauth_service = GoogleOAuthService(
        session_factory=session_factory,
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        redirect_uri=redirect_uri,
        encryption_key=settings.ENCRYPTION_KEY or "",
        allowed_domains=settings.GOOGLE_WORKSPACE_DOMAINS,
    )
    return GoogleClient(oauth_service)


def _get_odoo_service(request: Request, user: User) -> OdooService | None:
    settings = request.app.state.settings
    odoo_url = user.tenant or settings.ODOO_URL
    odoo_db = user.database or settings.ODOO_DB
    if not odoo_url or not odoo_db:
        return None
    odoo_client = OdooClient(url=odoo_url, db=odoo_db)
    return OdooService(odoo_client)


@router.post("/identity/resolve")
def resolve_identity(
    req: IdentityResolveRequest,
    request: Request,
):
    settings = request.app.state.settings
    session_factory = request.app.state.session_factory
    with session_factory() as db:
        # Check matching active connection in private chat
        conn = (
            db.query(TelegramConnection)
            .filter_by(
                telegram_user_id=req.telegram_user_id,
                private_chat_id=req.chat_id,
                status="connected",
            )
            .first()
        )
        if not conn:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "NOT_CONNECTED", "message": "Telegram connection not found"},
            )

        user = db.query(User).filter_by(id=conn.user_id).first()
        if not user or not user.active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "INACTIVE_USER", "message": "User account is disabled or missing"},
            )

        if not conn.binding_id:
            conn.binding_id = f"tg_bind_{secrets.token_hex(16)}"
            db.commit()

        if req.contract_version == 2:
            # Decrypt secret for employee profile & permissions
            secret = ""
            if user.credential and user.credential.encrypted_secret and settings.ENCRYPTION_KEY:
                try:
                    secret = decrypt_secret(user.credential.encrypted_secret, settings.ENCRYPTION_KEY)
                except Exception:
                    secret = ""

            g_conn = user.google_connection
            g_status = g_conn.status if g_conn else "disconnected"
            g_scopes = g_conn.scopes.split() if g_conn and g_conn.scopes else []

            secret_hash = hashlib.sha256(secret.encode()).hexdigest()
            scopes_key = ",".join(sorted(g_scopes))
            cache_key = f"{user.id}:{user.tenant}:{user.database}:{user.odoo_uid}:{secret_hash}:{g_status}:{scopes_key}"

            cached = identity_cache.get(cache_key)
            if cached:
                job_title = cached.get("job_title")
                department = cached.get("department")
                manager_name = cached.get("manager_name")
                permissions = cached.get("permissions", [])
                auth_checked_at = cached.get("authorization_checked_at") or datetime.now(UTC).isoformat()
            else:
                odoo_svc = _get_odoo_service(request, user)
                # Xác minh Odoo account trước khi cấp quyền
                _verify_odoo_account(odoo_svc, user.odoo_uid, secret)

                job_title = None
                department = None
                manager_name = None
                if odoo_svc and secret:
                    profile = odoo_svc.get_employee_profile(user.odoo_uid, secret)
                    job_title = profile.get("job_title")
                    department = profile.get("department")
                    manager_name = profile.get("manager_name")

                permissions = odoo_svc.compute_permissions(
                    uid=user.odoo_uid,
                    secret=secret,
                    is_internal_user=bool(user.active),
                    google_status=g_status,
                    google_scopes=g_scopes,
                )
                auth_checked_at = datetime.now(UTC).isoformat()
                identity_cache.set(
                    cache_key,
                    {
                        "job_title": job_title,
                        "department": department,
                        "manager_name": manager_name,
                        "permissions": permissions,
                        "authorization_checked_at": auth_checked_at,
                    },
                )

            return {
                "contract_version": 2,
                "status": "OK",
                "user_id": user.id,
                "tenant": user.tenant,
                "database": user.database,
                "odoo_uid": user.odoo_uid,
                "name": user.name,
                "login": user.login,
                "tz": user.tz or "Asia/Bangkok",
                "telegram_binding_id": conn.binding_id,
                "job_title": job_title,
                "department": department,
                "manager_name": manager_name,
                "permissions": permissions,
                "authorization_checked_at": auth_checked_at,
            }

        return {
            "status": "OK",
            "user_id": user.id,
            "tenant": user.tenant,
            "database": user.database,
            "odoo_uid": user.odoo_uid,
            "name": user.name,
            "login": user.login,
            "tz": user.tz or "Asia/Bangkok",
        }


@router.get("/users/{user_id}/connections")
def get_user_connections(
    user_id: int,
    request: Request,
):
    session_factory = request.app.state.session_factory
    with session_factory() as db:
        user = db.query(User).filter_by(id=user_id).first()
        if not user or not user.active:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "USER_NOT_FOUND", "message": "User not found or inactive"},
            )

        tele_conn = user.telegram_connection
        google_conn = user.google_connection

        tele_status = "OK" if (tele_conn and tele_conn.status == "connected") else "NOT_CONNECTED"
        if tele_conn and tele_conn.status == "blocked":
            tele_status = "BLOCKED"

        g_status = "NOT_CONNECTED"
        g_scopes: list[str] = []
        if google_conn:
            if google_conn.status == "connected":
                g_status = "OK"
            elif google_conn.status == "reconnect_required":
                g_status = "RECONNECT_REQUIRED"
            else:
                g_status = "NOT_CONNECTED"
            if google_conn.scopes:
                g_scopes = google_conn.scopes.split()

        return {
            "status": "OK",
            "user_id": user.id,
            "odoo": {
                "status": "OK",
                "login": user.login,
                "odoo_uid": user.odoo_uid,
            },
            "telegram": {
                "status": tele_status,
                "telegram_user_id": tele_conn.telegram_user_id if tele_conn else None,
                "username": tele_conn.username if tele_conn else None,
            },
            "google": {
                "status": g_status,
                "email": google_conn.email if google_conn else None,
                "scopes": g_scopes,
            },
        }


@router.get("/users/{user_id}/calendar/events")
def get_calendar_events(
    user_id: int,
    request: Request,
    time_min: str = Query(..., description="Start ISO datetime"),
    time_max: str = Query(..., description="End ISO datetime"),
):
    session_factory = request.app.state.session_factory
    with session_factory() as db:
        user = db.query(User).filter_by(id=user_id).first()
        if not user or not user.active:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "USER_NOT_FOUND", "message": "User not found or inactive"},
            )
        g_conn = user.google_connection

    if not g_conn or g_conn.status == "disconnected":
        return {"status": "NOT_CONNECTED", "events": []}
    if g_conn.status == "reconnect_required":
        return {"status": "RECONNECT_REQUIRED", "events": []}

    # Check scopes
    scopes = g_conn.scopes.split() if g_conn.scopes else []
    has_cal = any("calendar" in s for s in scopes)
    if not has_cal:
        return {
            "status": "PERMISSION_DENIED",
            "events": [],
            "instruction": "Vui lòng cấp quyền Google Calendar.",
        }

    client = _get_google_client(request)
    try:
        data = client.list_calendar_events(
            user_id=user_id,
            calendar_id="primary",
            time_min=time_min,
            time_max=time_max,
            single_events=True,
        )
        items = data.get("items", [])
        events = []
        for it in items:
            start_info = it.get("start", {})
            end_info = it.get("end", {})
            is_all_day = "date" in start_info and "dateTime" not in start_info
            events.append(
                {
                    "id": it.get("id"),
                    "summary": it.get("summary") or "(Không có tiêu đề)",
                    "description": it.get("description") or "",
                    "start": start_info.get("dateTime") or start_info.get("date"),
                    "end": end_info.get("dateTime") or end_info.get("date"),
                    "is_all_day": is_all_day,
                    "location": it.get("location") or "",
                    "link": it.get("htmlLink") or "",
                }
            )
        return {"status": "OK", "events": events}
    except Exception as e:
        from app.google.oauth import GoogleAuthRevokedError

        if isinstance(e, GoogleAuthRevokedError):
            return {"status": "RECONNECT_REQUIRED", "events": []}
        import httpx

        if isinstance(e, httpx.TimeoutException):
            return {"status": "TIMEOUT", "events": []}
        return {"status": "ERROR", "events": [], "message": str(e)}


@router.get("/users/{user_id}/gmail/unread")
def get_unread_emails(
    user_id: int,
    request: Request,
):
    session_factory = request.app.state.session_factory
    with session_factory() as db:
        user = db.query(User).filter_by(id=user_id).first()
        if not user or not user.active:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "USER_NOT_FOUND", "message": "User not found or inactive"},
            )

    client = _get_google_client(request)
    return client.list_unread_emails(user_id=user_id, max_results=5)


@router.get("/users/{user_id}/crm/leads")
def get_crm_leads(
    user_id: int,
    request: Request,
    search_term: str | None = Query(None, description="Search term for lead name"),
    limit: int = Query(5, ge=1, le=50, description="Max leads to return"),
):
    settings = request.app.state.settings
    session_factory = request.app.state.session_factory
    with session_factory() as db:
        user = db.query(User).filter_by(id=user_id).first()
        if not user or not user.active:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "USER_NOT_FOUND", "message": "User not found or inactive"},
            )

        secret = ""
        if user.credential and user.credential.encrypted_secret and settings.ENCRYPTION_KEY:
            try:
                secret = decrypt_secret(user.credential.encrypted_secret, settings.ENCRYPTION_KEY)
            except Exception:
                secret = ""

        odoo_svc = _get_odoo_service(request, user)
        # Xác minh tài khoản Odoo trước khi đọc CRM
        _verify_odoo_account(odoo_svc, user.odoo_uid, secret)

        try:
            leads = odoo_svc.search_my_leads(
                uid=user.odoo_uid,
                secret=secret,
                search_term=search_term,
                limit=limit,
            )
            return {"status": "OK", "leads": leads}
        except Exception as e:
            from app.odoo.client import OdooAccessError

            if isinstance(e, OdooAccessError):
                return {"status": "PERMISSION_DENIED", "leads": [], "message": str(e)}
            return {"status": "ERROR", "leads": [], "message": str(e)}


class UserActionRequest(BaseModel):
    operation_id: str
    telegram_user_id: int
    chat_id: int
    telegram_binding_id: str
    action: str
    arguments: dict[str, Any] = {}


@router.post("/users/{user_id}/actions")
def execute_user_action(
    user_id: int,
    req: UserActionRequest,
    request: Request,
):
    settings = request.app.state.settings
    session_factory = request.app.state.session_factory
    with session_factory() as db:
        user = db.query(User).filter_by(id=user_id).first()
        if not user or not user.active:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "USER_NOT_FOUND", "message": "User not found or inactive"},
            )

        # 1. Verify Telegram connection and exact binding_id
        conn = user.telegram_connection
        if (
            not conn
            or conn.status != "connected"
            or conn.telegram_user_id != req.telegram_user_id
            or conn.private_chat_id != req.chat_id
            or conn.binding_id != req.telegram_binding_id
        ):
            return {
                "status": "RECONNECT_REQUIRED",
                "message": "Liên kết Telegram không hợp lệ hoặc đã bị thay đổi. Vui lòng liên kết lại.",
            }

        # 2. Decrypt secret & compute permissions
        secret = ""
        if user.credential and user.credential.encrypted_secret and settings.ENCRYPTION_KEY:
            try:
                secret = decrypt_secret(user.credential.encrypted_secret, settings.ENCRYPTION_KEY)
            except Exception:
                secret = ""

        odoo_svc = _get_odoo_service(request, user)
        # Xác minh tài khoản Odoo trước khi cấp quyền thực thi action
        _verify_odoo_account(odoo_svc, user.odoo_uid, secret)

        g_conn = user.google_connection
        g_status = g_conn.status if g_conn else "disconnected"
        g_scopes = g_conn.scopes.split() if g_conn and g_conn.scopes else []

        permissions = set(
            odoo_svc.compute_permissions(
                uid=user.odoo_uid,
                secret=secret,
                is_internal_user=bool(user.active),
                google_status=g_status,
                google_scopes=g_scopes,
            )
        )

        action_perm_map = {
            "calendar.create": "calendar.write",
            "calendar.update": "calendar.write",
            "gmail.send": "gmail.send",
            "crm.create": "crm.create",
            "crm.update": "crm.write",
        }
        if req.action not in action_perm_map:
            return {"status": "SERVICE_ERROR", "message": f"Action không được hỗ trợ: {req.action}"}

        needed_perm = action_perm_map[req.action]
        if needed_perm not in permissions:
            return {
                "status": "PERMISSION_DENIED",
                "message": f"Tài khoản chưa được cấp quyền thực hiện {req.action} (yêu cầu {needed_perm}).",
            }

        # 3. Idempotency & Concurrency check with ActionOperation
        args = req.arguments or {}
        payload_data = {"action": req.action, "arguments": args}
        payload_hash = hashlib.sha256(json.dumps(payload_data, sort_keys=True).encode("utf-8")).hexdigest()

        op = db.query(ActionOperation).filter_by(user_id=user_id, operation_id=req.operation_id).first()
        if op:
            if op.payload_hash != payload_hash:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={"code": "PAYLOAD_MISMATCH", "message": f"operation_id '{req.operation_id}' đã tồn tại với tham số khác."},
                )
            if op.status == "completed":
                return json.loads(op.result_json) if op.result_json else {"status": "completed"}
            if op.status == "in_progress":
                created_at = op.created_at
                is_expired = False
                if created_at:
                    c_dt = created_at if created_at.tzinfo else created_at.replace(tzinfo=UTC)
                    if (datetime.now(UTC) - c_dt).total_seconds() > 30:
                        is_expired = True
                if is_expired:
                    op.status = "unknown_result"
                    op.error_message = "Thao tác trước đó quá hạn theo dõi chưa có kết quả, không thể tự động thực hiện lại."
                    db.commit()
                    return {"status": "UNKNOWN_RESULT", "message": op.error_message}
                return {"status": "IN_PROGRESS", "message": "Thao tác đang được thực thi bởi một tiến trình khác."}
            if op.status == "unknown_result":
                return {"status": "UNKNOWN_RESULT", "message": "Thao tác trước đó chưa rõ kết quả do quá hạn chờ, không thể tự động thực hiện lại."}
            if op.status == "failed":
                return {"status": "SERVICE_ERROR", "message": op.error_message or "Thao tác trước đó đã thất bại."}

        # Tạo bản ghi atomic claim, xử lý race condition nếu hai request cùng operation_id insert đồng thời
        from sqlalchemy.exc import IntegrityError
        try:
            op = ActionOperation(
                operation_id=req.operation_id,
                user_id=user_id,
                action=req.action,
                payload_hash=payload_hash,
                status="in_progress",
                telegram_binding_id=req.telegram_binding_id,
            )
            db.add(op)
            db.commit()
        except IntegrityError as err:
            db.rollback()
            op = db.query(ActionOperation).filter_by(user_id=user_id, operation_id=req.operation_id).first()
            if op:
                if op.payload_hash != payload_hash:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={"code": "PAYLOAD_MISMATCH", "message": f"operation_id '{req.operation_id}' đã tồn tại với tham số khác."},
                    ) from err
                if op.status == "completed":
                    return json.loads(op.result_json) if op.result_json else {"status": "completed"}
                if op.status == "in_progress":
                    return {"status": "IN_PROGRESS", "message": "Thao tác đang được thực thi bởi một tiến trình khác."}
                if op.status == "unknown_result":
                    return {"status": "UNKNOWN_RESULT", "message": "Thao tác trước đó chưa rõ kết quả do quá hạn chờ, không thể tự động thực hiện lại."}
                if op.status == "failed":
                    return {"status": "SERVICE_ERROR", "message": op.error_message or "Thao tác trước đó đã thất bại."}

        # 4. Thực thi action với schema cố định
        try:
            if req.action == "calendar.create":
                summary = args.get("summary")
                start_time = args.get("start_time")
                end_time = args.get("end_time")
                if not summary or not start_time or not end_time:
                    op.status = "failed"
                    op.error_message = "Thiếu tham số bắt buộc cho calendar.create"
                    db.commit()
                    return {
                        "status": "SERVICE_ERROR",
                        "message": "Thiếu tham số bắt buộc cho calendar.create (summary, start_time, end_time).",
                    }
                ev_data = {
                    "summary": summary,
                    "description": args.get("description", ""),
                    "start": {"dateTime": start_time},
                    "end": {"dateTime": end_time},
                    "location": args.get("location", ""),
                }
                if args.get("attendees"):
                    ev_data["attendees"] = [{"email": a} for a in args["attendees"]]
                google_client = _get_google_client(request)
                res = google_client.create_calendar_event(
                    user_id=user_id,
                    calendar_id="primary",
                    event_data=ev_data,
                )
                if not isinstance(res, dict) or not res.get("id"):
                    op.status = "failed"
                    op.error_message = "Google Calendar không trả về ID sự kiện"
                    db.commit()
                    return {"status": "SERVICE_ERROR", "message": op.error_message}

                final_res = {"status": "completed", "result": res}
                op.status = "completed"
                op.result_json = json.dumps(final_res, ensure_ascii=False)
                db.commit()
                return final_res

            elif req.action == "calendar.update":
                event_id = args.get("event_id")
                if not event_id:
                    op.status = "failed"
                    op.error_message = "Thiếu tham số event_id"
                    db.commit()
                    return {
                        "status": "SERVICE_ERROR",
                        "message": "Thiếu tham số event_id cho calendar.update.",
                    }
                ev_data = {}
                if "summary" in args:
                    ev_data["summary"] = args["summary"]
                if "description" in args:
                    ev_data["description"] = args["description"]
                if "start_time" in args:
                    ev_data["start"] = {"dateTime": args["start_time"]}
                if "end_time" in args:
                    ev_data["end"] = {"dateTime": args["end_time"]}
                if "location" in args:
                    ev_data["location"] = args["location"]
                if "attendees" in args:
                    ev_data["attendees"] = [{"email": a} for a in args["attendees"]]

                import httpx
                google_client = _get_google_client(request)
                try:
                    res = google_client.update_calendar_event(
                        user_id=user_id,
                        calendar_id="primary",
                        event_id=event_id,
                        event_data=ev_data,
                        etag=args.get("etag") or args.get("if_match"),
                    )
                except httpx.HTTPStatusError as http_err:
                    if http_err.response.status_code in [412, 409]:
                        op.status = "failed"
                        op.error_message = "Calendar data changed after preview (If-Match conflict)"
                        db.commit()
                        return {
                            "status": "CONFLICT",
                            "message": "Sự kiện Lịch Google đã bị thay đổi trên hệ thống sau khi tạo bản xem trước (If-Match conflict). Vui lòng tạo yêu cầu mới.",
                        }
                    raise http_err

                if not isinstance(res, dict) or not res.get("id"):
                    op.status = "failed"
                    op.error_message = "Google Calendar không trả về ID sự kiện cập nhật"
                    db.commit()
                    return {"status": "SERVICE_ERROR", "message": op.error_message}

                final_res = {"status": "completed", "result": res}
                op.status = "completed"
                op.result_json = json.dumps(final_res, ensure_ascii=False)
                db.commit()
                return final_res

            elif req.action == "gmail.send":
                to = args.get("to")
                subject = args.get("subject")
                body_text = args.get("body_text") or args.get("body")
                if not to or not subject or not body_text:
                    op.status = "failed"
                    op.error_message = "Thiếu tham số gửi thư"
                    db.commit()
                    return {
                        "status": "SERVICE_ERROR",
                        "message": "Thiếu tham số bắt buộc cho gmail.send (to, subject, body_text).",
                    }
                google_client = _get_google_client(request)
                res = google_client.send_email(
                    user_id=user_id, to=to, subject=subject, body_text=body_text
                )
                if isinstance(res, dict) and res.get("status") in ["NOT_CONNECTED", "RECONNECT_REQUIRED", "PERMISSION_DENIED", "SERVICE_ERROR"]:
                    op.status = "failed"
                    op.error_message = res.get("message")
                    db.commit()
                    return res

                if not isinstance(res, dict) or not res.get("id"):
                    op.status = "failed"
                    op.error_message = res.get("message") if isinstance(res, dict) and res.get("message") else "Không nhận được message ID từ Google"
                    db.commit()
                    return {"status": "SERVICE_ERROR", "message": op.error_message}

                final_res = {"status": "completed", "result": res}
                op.status = "completed"
                op.result_json = json.dumps(final_res, ensure_ascii=False)
                db.commit()
                return final_res

            elif req.action == "crm.create":
                name = args.get("name")
                if not name:
                    op.status = "failed"
                    op.error_message = "Thiếu tên cơ hội"
                    db.commit()
                    return {
                        "status": "SERVICE_ERROR",
                        "message": "Thiếu tham số bắt buộc 'name' cho crm.create.",
                    }
                if not odoo_svc or not secret:
                    op.status = "failed"
                    op.error_message = "Chưa có thông tin xác thực Odoo"
                    db.commit()
                    return {"status": "NOT_CONNECTED", "message": "Chưa có thông tin xác thực Odoo."}
                res = odoo_svc.create_lead(
                    uid=user.odoo_uid,
                    secret=secret,
                    name=name,
                    contact_name=args.get("contact_name"),
                    email_from=args.get("email_from"),
                    phone=args.get("phone"),
                    description=args.get("description"),
                    expected_revenue=args.get("expected_revenue"),
                    stage_id=args.get("stage_id"),
                )
                if not isinstance(res, dict) or not res.get("id"):
                    op.status = "failed"
                    op.error_message = "Odoo không trả về ID cơ hội CRM mới tạo"
                    db.commit()
                    return {"status": "SERVICE_ERROR", "message": op.error_message}

                final_res = {"status": "completed", "result": res}
                op.status = "completed"
                op.result_json = json.dumps(final_res, ensure_ascii=False)
                db.commit()
                return final_res

            elif req.action == "crm.update":
                lead_id = args.get("lead_id")
                if not lead_id:
                    op.status = "failed"
                    op.error_message = "Thiếu lead_id"
                    db.commit()
                    return {
                        "status": "SERVICE_ERROR",
                        "message": "Thiếu tham số bắt buộc 'lead_id' cho crm.update.",
                    }
                if not odoo_svc or not secret:
                    op.status = "failed"
                    op.error_message = "Chưa có thông tin xác thực Odoo"
                    db.commit()
                    return {"status": "NOT_CONNECTED", "message": "Chưa có thông tin xác thực Odoo."}

                try:
                    res = odoo_svc.update_lead(
                        uid=user.odoo_uid,
                        secret=secret,
                        lead_id=int(lead_id),
                        name=args.get("name"),
                        contact_name=args.get("contact_name"),
                        email_from=args.get("email_from"),
                        phone=args.get("phone"),
                        description=args.get("description"),
                        expected_revenue=args.get("expected_revenue"),
                        stage_id=args.get("stage_id"),
                        expected_write_date=args.get("expected_write_date") or args.get("write_date"),
                    )
                except ValueError as val_err:
                    err_text = str(val_err)
                    is_conflict = "write_date" in err_text or "thay đổi trên hệ thống" in err_text
                    op.status = "failed"
                    op.error_message = err_text
                    db.commit()
                    return {
                        "status": "CONFLICT" if is_conflict else "SERVICE_ERROR",
                        "message": err_text,
                    }

                final_res = {"status": "completed", "result": res}
                op.status = "completed"
                op.result_json = json.dumps(final_res, ensure_ascii=False)
                db.commit()
                return final_res

        except Exception as e:
            import httpx

            from app.google.oauth import GoogleAuthRevokedError

            if isinstance(e, GoogleAuthRevokedError):
                op.status = "failed"
                op.error_message = "Google token revoked"
                db.commit()
                return {
                    "status": "RECONNECT_REQUIRED",
                    "message": "Google token bị thu hồi hoặc cần xác thực lại.",
                }
            if isinstance(e, httpx.TimeoutException):
                op.status = "unknown_result"
                op.error_message = "Timeout waiting for external service"
                db.commit()
                return {
                    "status": "UNKNOWN_RESULT",
                    "message": "Thời gian chờ gọi dịch vụ ngoài quá hạn (chưa xác định kết quả).",
                }
            op.status = "failed"
            op.error_message = str(e)
            db.commit()
            return {"status": "SERVICE_ERROR", "message": str(e)}


@router.get("/users/{user_id}/actions/{operation_id}")
def get_user_action_status(
    user_id: int,
    operation_id: str,
    telegram_user_id: int = Query(...),
    chat_id: int = Query(...),
    telegram_binding_id: str = Query(...),
    request: Request = None,
):
    """Đọc trạng thái thao tác ghi từ sổ ActionOperation phục vụ đối soát.

    Quy tắc an toàn:
    - Chỉ đọc ledger; tuyệt đối không tạo operation hoặc gọi provider ghi.
    - Kiểm tra user, Telegram binding và quyền hiện tại.
    - Operation cũ thiếu binding không tự động trả kết quả chi tiết.
    - Trả 'not_found' nếu chưa có trong sổ (không đồng nghĩa thao tác chưa từng xảy ra).
    """
    session_factory = request.app.state.session_factory
    with session_factory() as db:
        user = db.query(User).filter_by(id=user_id).first()
        if not user or not user.active:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "USER_NOT_FOUND", "message": "User not found or inactive"},
            )

        tele_conn = user.telegram_connection
        if (
            not tele_conn
            or tele_conn.telegram_user_id != telegram_user_id
            or tele_conn.private_chat_id != chat_id
            or tele_conn.binding_id != telegram_binding_id
            or tele_conn.status != "connected"
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "TELEGRAM_BINDING_MISMATCH", "message": "Telegram binding mismatch or disconnected"},
            )

        op = db.query(ActionOperation).filter_by(user_id=user_id, operation_id=operation_id).first()
        if not op:
            return {
                "operation_id": operation_id,
                "action": None,
                "status": "not_found",
                "result": None,
                "updated_at": None,
            }

        # Nếu operation đã lưu binding khác với binding đang yêu cầu -> từ chối
        if op.telegram_binding_id and op.telegram_binding_id != telegram_binding_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "BINDING_MISMATCH", "message": "Operation belongs to another binding"},
            )

        # Xác minh tài khoản Odoo và quyền action hiện tại giống logic của POST
        settings = getattr(request.app.state, "settings", None)
        if not settings:
            from app.config import get_settings
            settings = get_settings()

        secret = ""
        if user.credential and user.credential.encrypted_secret and settings.ENCRYPTION_KEY:
            try:
                secret = decrypt_secret(user.credential.encrypted_secret, settings.ENCRYPTION_KEY)
            except Exception:
                secret = ""

        odoo_svc = _get_odoo_service(request, user)
        # _verify_odoo_account kiểm tra secret và trạng thái Odoo; ném 403 hoặc 503
        _verify_odoo_account(odoo_svc, user.odoo_uid, secret)

        if op.action:
            action_perm_map = {
                "calendar.create": "calendar.write",
                "calendar.update": "calendar.write",
                "gmail.send": "gmail.send",
                "crm.create": "crm.create",
                "crm.update": "crm.write",
            }
            if op.action in action_perm_map:
                needed_perm = action_perm_map[op.action]
                g_conn = user.google_connection
                g_status = g_conn.status if g_conn else "disconnected"
                g_scopes = g_conn.scopes.split() if g_conn and g_conn.scopes else []

                permissions = set(
                    odoo_svc.compute_permissions(
                        uid=user.odoo_uid,
                        secret=secret,
                        is_internal_user=bool(user.active),
                        google_status=g_status,
                        google_scopes=g_scopes,
                    )
                )
                if needed_perm not in permissions:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail={
                            "code": "PERMISSION_DENIED",
                            "message": f"Tài khoản hiện không có quyền cho action '{op.action}' (yêu cầu {needed_perm}).",
                        },
                    )

        # Trích xuất result nếu có
        res_data = None
        if op.status == "completed" and op.result_json:
            # Nếu là operation cũ không có telegram_binding_id -> không tự động trả nội dung kết quả
            if not op.telegram_binding_id:
                res_data = None
            else:
                try:
                    parsed = json.loads(op.result_json)
                    res_data = parsed.get("result") if isinstance(parsed, dict) and "result" in parsed else parsed
                except Exception:
                    res_data = None

        up_at = op.updated_at or op.created_at
        return {
            "operation_id": op.operation_id,
            "action": op.action,
            "status": op.status,
            "result": res_data,
            "updated_at": up_at.isoformat() if up_at else None,
        }


@router.get("/users/{user_id}/calendar/events/{event_id}")
def get_calendar_event_detail(
    user_id: int,
    event_id: str,
    request: Request,
):
    settings = request.app.state.settings
    session_factory = request.app.state.session_factory
    with session_factory() as db:
        user = db.query(User).filter_by(id=user_id).first()
        if not user or not user.active:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "USER_NOT_FOUND", "message": "User not found or inactive"},
            )

        secret = ""
        if user.credential and user.credential.encrypted_secret and settings.ENCRYPTION_KEY:
            try:
                secret = decrypt_secret(user.credential.encrypted_secret, settings.ENCRYPTION_KEY)
            except Exception:
                secret = ""

        odoo_svc = _get_odoo_service(request, user)
        _verify_odoo_account(odoo_svc, user.odoo_uid, secret)

        g_conn = user.google_connection
        g_status = g_conn.status if g_conn else "disconnected"
        g_scopes = g_conn.scopes.split() if g_conn and g_conn.scopes else []

        permissions = odoo_svc.compute_permissions(
            uid=user.odoo_uid,
            secret=secret,
            is_internal_user=bool(user.active),
            google_status=g_status,
            google_scopes=g_scopes,
        )
        if "calendar.read" not in permissions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "PERMISSION_DENIED", "message": "Chưa được cấp quyền đọc lịch Google."},
            )

    google_client = _get_google_client(request)
    try:
        ev = google_client.get_calendar_event(user_id=user_id, calendar_id="primary", event_id=event_id)
        return {"status": "OK", "event": ev}
    except Exception as e:
        import httpx

        from app.google.oauth import GoogleAuthRevokedError

        if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "EVENT_NOT_FOUND", "message": f"Không tìm thấy sự kiện lịch {event_id}"},
            ) from e
        if isinstance(e, GoogleAuthRevokedError):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "RECONNECT_REQUIRED", "message": "Google token bị thu hồi hoặc cần xác thực lại."},
            ) from e
        if isinstance(e, httpx.TimeoutException):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "SERVICE_UNAVAILABLE", "message": "Quá thời gian chờ kết nối Google Calendar."},
            ) from e
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "SERVICE_ERROR", "message": str(e)},
        ) from e


@router.get("/users/{user_id}/crm/leads/{lead_id}")
def get_crm_lead_detail(
    user_id: int,
    lead_id: int,
    request: Request,
):
    settings = request.app.state.settings
    session_factory = request.app.state.session_factory
    with session_factory() as db:
        user = db.query(User).filter_by(id=user_id).first()
        if not user or not user.active:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "USER_NOT_FOUND", "message": "User not found or inactive"},
            )

        secret = ""
        if user.credential and user.credential.encrypted_secret and settings.ENCRYPTION_KEY:
            try:
                secret = decrypt_secret(user.credential.encrypted_secret, settings.ENCRYPTION_KEY)
            except Exception:
                secret = ""

        odoo_svc = _get_odoo_service(request, user)
        _verify_odoo_account(odoo_svc, user.odoo_uid, secret)

        permissions = odoo_svc.compute_permissions(
            uid=user.odoo_uid,
            secret=secret,
            is_internal_user=bool(user.active),
            google_status="disconnected",
            google_scopes=[],
        )
        if "crm.read" not in permissions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "PERMISSION_DENIED", "message": "Chưa được cấp quyền đọc CRM (crm.read)."},
            )

        try:
            lead = odoo_svc.get_lead_detail(uid=user.odoo_uid, secret=secret, lead_id=lead_id)
        except Exception as e:
            if isinstance(e, OdooNetworkError):
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={"code": "ODOO_UNAVAILABLE", "message": f"Không thể kết nối Odoo: {e}"},
                ) from e
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"code": "SERVICE_ERROR", "message": str(e)},
            ) from e

        if not lead:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "LEAD_NOT_FOUND", "message": f"Không tìm thấy cơ hội CRM {lead_id}"},
            )
        return {"status": "OK", "lead": lead}
