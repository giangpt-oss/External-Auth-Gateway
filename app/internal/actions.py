"""Internal actions endpoints."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel

from app.google.account_resolver import GoogleAccountResolver
from app.models import ActionOperation, User
from app.odoo.client import (
    OdooNetworkError,
)
from app.security import decrypt_secret
from app.workspace.catalog import SERVICES
from app.workspace.selection_manager import SelectionManager, SwitchLockError, active_account_pin
from app.workspace.service import WorkspaceError

router = APIRouter()


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
    from app.internal import routes as dependencies

    if req.action.startswith('business.'):
        from app.business.routes import execute_business_action
        return execute_business_action(user_id, req, request)
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

        odoo_svc = dependencies._get_odoo_service(request, user)
        # Xác minh tài khoản Odoo trước khi cấp quyền thực thi action
        dependencies._verify_odoo_account(odoo_svc, user.odoo_uid, secret)

        g_conn = GoogleAccountResolver.get_active_account(db, user_id)
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
            "sales.quotation.create": "sales.create",
            "crm.create": "crm.create",
            "crm.update": "crm.write",
        }
        action_perm_map.update({f"workspace.{name}.write": f"workspace.{name}.write" for name in SERVICES})
        if req.action not in action_perm_map:
            return {"status": "SERVICE_ERROR", "message": f"Action không được hỗ trợ: {req.action}"}

        if req.action == "sales.quotation.create" and not settings.ENABLE_QUOTATIONS:
            return {"status": "SERVICE_ERROR", "message": "Chức năng báo giá chưa được bật."}

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
            if op.telegram_binding_id != req.telegram_binding_id:
                raise HTTPException(403, "Thao tác thuộc phiên liên kết khác.")
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
                if req.action == 'sales.quotation.create':
                    from app.quotations.execution import QuoteExecutionJob
                    active_job = db.query(QuoteExecutionJob).filter_by(user_id=user_id, operation_id=req.operation_id).first()
                    if active_job and active_job.status in ('queued', 'executing'):
                        is_expired = False
                if is_expired:
                    message = "Thao tác trước đó quá hạn theo dõi chưa có kết quả, không thể tự động thực hiện lại."
                    db.query(ActionOperation).filter_by(id=op.id, status="in_progress").update(
                        {"status": "unknown_result", "error_message": message}, synchronize_session=False)
                    db.commit()
                    db.refresh(op)
                    if op.status == "completed":
                        return json.loads(op.result_json) if op.result_json else {"status": "completed"}
                    return {"status": "UNKNOWN_RESULT", "message": message}
                return {"status": "IN_PROGRESS", "message": "Thao tác đang được thực thi bởi một tiến trình khác."}
            if op.status == "unknown_result":
                return {"status": "UNKNOWN_RESULT", "message": "Thao tác trước đó chưa rõ kết quả do quá hạn chờ, không thể tự động thực hiện lại."}
            if op.status == "failed":
                return {"status": "SERVICE_ERROR", "message": op.error_message or "Thao tác trước đó đã thất bại."}

        # Tạo bản ghi atomic claim, xử lý race condition nếu hai request cùng operation_id insert đồng thời
        from sqlalchemy.exc import IntegrityError
        pin = None
        try:
            if req.action.startswith(("calendar.", "gmail.", "workspace.")):
                pin = SelectionManager.admit(db, user_id, account_id=g_conn.id if g_conn else None)
            op = ActionOperation(
                operation_id=req.operation_id,
                user_id=user_id,
                action=req.action,
                payload_hash=payload_hash,
                status="in_progress",
                telegram_binding_id=req.telegram_binding_id,
                google_account_id=pin.account_id if pin else None,
            )
            db.add(op)
            if req.action == 'sales.quotation.create':
                from app.quotations.execution import QuoteExecutionJob
                scope = {'telegram_user_id': req.telegram_user_id, 'chat_id': req.chat_id, 'telegram_binding_id': req.telegram_binding_id}
                db.add(QuoteExecutionJob(operation_id=req.operation_id, user_id=user_id, tenant=user.tenant,
                    database=user.database, scope_json=json.dumps(scope), payload_json=json.dumps(args, ensure_ascii=False)))
            db.commit()
        except SwitchLockError as exc:
            return {"status": exc.code, "message": exc.message}
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
        pin_token = active_account_pin.set(pin) if pin else None
        try:
            if req.action == "sales.quotation.create":
                return {'status': 'in_progress', 'message': 'Đã nhận xác nhận, đang tạo báo giá trong nền.', 'operation_id': req.operation_id}
            if req.action.startswith("workspace."):
                service_name = req.action.split(".")[1]
                result = dependencies._get_workspace_service(request).execute(user_id, service_name, args)
                final_res = {"status": "completed", "result": result["data"]}
                op.status = "completed"
                op.result_json = json.dumps(final_res, ensure_ascii=False)
                db.commit()
                return final_res
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
                google_client = dependencies._get_google_client(request)
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
                google_client = dependencies._get_google_client(request)
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
                google_client = dependencies._get_google_client(request)
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

        except WorkspaceError as e:
            op.status = "unknown_result" if e.code == "UNKNOWN_RESULT" else "failed"
            op.error_message = str(e)
            db.commit()
            return {"status": e.code, "message": str(e)}
        except Exception as e:
            import httpx

            from app.google.oauth import GoogleAuthRevokedError

            if req.action == "sales.quotation.create":
                db.rollback()
                unknown = isinstance(e, httpx.TimeoutException | OdooNetworkError)
                db.query(ActionOperation).filter(
                    ActionOperation.id == op.id,
                    ActionOperation.status.in_(["in_progress", "unknown_result"]),
                ).update({"status": "unknown_result" if unknown else "failed",
                          "error_message": str(e)}, synchronize_session=False)
                db.commit()
                db.refresh(op)
                if op.status == "completed":
                    return json.loads(op.result_json) if op.result_json else {"status": "completed"}
                return {"status": "UNKNOWN_RESULT" if unknown else "SERVICE_ERROR",
                        "message": "Kết quả tạo chưa rõ; cần đối soát, không tự tạo lại." if unknown else str(e)}

            if isinstance(e, GoogleAuthRevokedError):
                op.status = "failed"
                op.error_message = "Google token revoked"
                db.commit()
                return {
                    "status": "RECONNECT_REQUIRED",
                    "message": "Google token bị thu hồi hoặc cần xác thực lại.",
                }
            if isinstance(e, httpx.TimeoutException | OdooNetworkError):
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
        finally:
            if pin_token is not None:
                active_account_pin.reset(pin_token)


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
    from app.business.routes import business_action_status
    from app.internal import routes as dependencies
    from app.reporting.schemas import TelegramScope
    # Only intercept actions handled by the new ledger; old integrations retain their contracts.
    with request.app.state.session_factory() as probe_db:
        probe = probe_db.query(ActionOperation.action).filter_by(user_id=user_id, operation_id=operation_id).first()
    if probe and probe[0].startswith('business.'):
        binding = TelegramScope(telegram_user_id=telegram_user_id, chat_id=chat_id, telegram_binding_id=telegram_binding_id)
        return business_action_status(user_id, operation_id, binding, request)
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

        odoo_svc = dependencies._get_odoo_service(request, user)
        # _verify_odoo_account kiểm tra secret và trạng thái Odoo; ném 403 hoặc 503
        dependencies._verify_odoo_account(odoo_svc, user.odoo_uid, secret)

        if op.action:
            action_perm_map = {
                "calendar.create": "calendar.write",
                "calendar.update": "calendar.write",
                "gmail.send": "gmail.send",
                "sales.quotation.create": "sales.create",
            "crm.create": "crm.create",
                "crm.update": "crm.write",
            }
            action_perm_map.update({f"workspace.{name}.write": f"workspace.{name}.write" for name in SERVICES})
            if op.action in action_perm_map:
                needed_perm = action_perm_map[op.action]
                g_conn = GoogleAccountResolver.get_active_account(db, user_id)
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

        if op.action == "sales.quotation.create" and op.status in ("unknown_result", "in_progress"):
            from app.quotations.execution import QuoteExecutionJob
            from app.quotations.service import QuotationService
            execution_job = db.query(QuoteExecutionJob).filter_by(user_id=user_id, operation_id=operation_id).first()
            still_working = execution_job and execution_job.status in ('queued', 'executing')
            created = op.created_at
            age = (datetime.now(UTC) - (created if created.tzinfo else created.replace(tzinfo=UTC))).total_seconds() if created else 60
            if not still_working and (op.status == "unknown_result" or age > 30):
                from app.quotations.tracking import reconcile_tracked
                reconciled = reconcile_tracked(session_factory, user_id, operation_id, user.tenant, user.database,
                    QuotationService(odoo_svc.client, user.odoo_uid, secret))
                updates = {"status": "unknown_result"}
                if reconciled.get("status") == "completed":
                    updates = {"status": "completed", "result_json": json.dumps(reconciled, ensure_ascii=False)}
                db.query(ActionOperation).filter(
                    ActionOperation.id == op.id,
                    ActionOperation.status.in_(["unknown_result", "in_progress"]),
                ).update(updates, synchronize_session=False)
                db.commit()
                db.refresh(op)

        # Trích xuất result nếu có
        res_data = None
        if (op.status == "completed" or (op.status == "failed" and op.action == "sales.quotation.create")) and op.result_json:
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
        from app.quotations.execution import error_summary
        return {
            "operation_id": op.operation_id,
            "action": op.action,
            "status": op.status,
            "result": res_data,
            "updated_at": up_at.isoformat() if up_at else None,
            "message": error_summary(op.error_message) if op.error_message else None,
        }
