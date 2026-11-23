"""Internal google endpoints."""


from fastapi import APIRouter, HTTPException, Query, Request, status

from app.google.account_resolver import GoogleAccountResolver
from app.models import User
from app.security import decrypt_secret
from app.workspace.selection_manager import SelectionManager, SwitchLockError

router = APIRouter()


@router.get("/users/{user_id}/calendar/events")
def get_calendar_events(
    user_id: int,
    request: Request,
    time_min: str = Query(..., description="Start ISO datetime"),
    time_max: str = Query(..., description="End ISO datetime"),
):
    from app.internal import routes as dependencies

    session_factory = request.app.state.session_factory
    with session_factory() as db:
        user = db.query(User).filter_by(id=user_id).first()
        if not user or not user.active:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "USER_NOT_FOUND", "message": "User not found or inactive"},
            )
        g_conn = GoogleAccountResolver.get_active_account(db, user_id)

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

    client = dependencies._get_google_client(request)
    try:
        with SelectionManager.admit_activity(session_factory, user_id, account_id=g_conn.id):
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
    except SwitchLockError as exc:
        return {"status": exc.code, "events": [], "message": exc.message}
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
    from app.internal import routes as dependencies

    session_factory = request.app.state.session_factory
    with session_factory() as db:
        user = db.query(User).filter_by(id=user_id).first()
        if not user or not user.active:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "USER_NOT_FOUND", "message": "User not found or inactive"},
            )

    client = dependencies._get_google_client(request)
    try:
        with SelectionManager.admit_activity(session_factory, user_id):
            return client.list_unread_emails(user_id=user_id, max_results=5)
    except SwitchLockError as exc:
        return {"status": exc.code, "total_unread": 0, "messages": []}


@router.get("/users/{user_id}/calendar/events/{event_id}")
def get_calendar_event_detail(
    user_id: int,
    event_id: str,
    request: Request,
):
    from app.internal import routes as dependencies

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

        odoo_svc = dependencies._get_odoo_service(request, user)
        dependencies._verify_odoo_account(odoo_svc, user.odoo_uid, secret)

        g_conn = GoogleAccountResolver.get_active_account(db, user_id)
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

    google_client = dependencies._get_google_client(request)
    try:
        with SelectionManager.admit_activity(
            session_factory, user_id, account_id=g_conn.id if g_conn else None,
        ):
            ev = google_client.get_calendar_event(
                user_id=user_id, calendar_id="primary", event_id=event_id,
            )
        return {"status": "OK", "event": ev}
    except SwitchLockError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": exc.message},
        ) from exc
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
