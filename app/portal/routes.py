import re
import secrets
from pathlib import Path
from typing import Any

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth.routes import get_auth_service
from app.auth.service import AuthenticatedUser, AuthService
from app.models import CalendarPair, GoogleConnection, SyncIntent, TelegramConnection, User
from app.odoo.client import OdooClient, OdooError
from app.odoo.service import OdooService

router = APIRouter(tags=["Portal"])

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def get_current_user_portal(
    request: Request,
    session_id: str | None = Cookie(None),
    auth_service: AuthService = Depends(get_auth_service),
) -> tuple[Any, str] | None:
    if not session_id:
        return None
    res = auth_service.get_user_by_session(session_id)
    if not res:
        return None
    user, session_rec = res
    csrf_token = (
        session_rec.csrf_secret if hasattr(session_rec, "csrf_secret") else str(session_rec)
    )
    return user, csrf_token


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    user_data: tuple[Any, str] | None = Depends(get_current_user_portal),
):
    if user_data:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    # Issue a transient CSRF token for the login form if not existing
    anon_csrf = request.cookies.get("anon_csrf")
    response = templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "csrf_token": anon_csrf or secrets.token_urlsafe(16),
            "user": None,
            "error": None,
        },
    )
    if not anon_csrf:
        response.set_cookie("anon_csrf", secrets.token_urlsafe(16), httponly=True, samesite="lax")
    return response


def _telegram_portal_state(
    request: Request, user: AuthenticatedUser, connection: TelegramConnection | None, auth: AuthService,
) -> str:
    if not connection or not connection.verified_on_web:
        return "not_verified"
    field = request.app.state.settings.ODOO_EMPLOYEE_TELEGRAM_ID_FIELD
    if not field:
        return "unavailable"
    try:
        secret = auth.get_user_secret(user.id)
        if not secret:
            return "unavailable"
        odoo = OdooService(OdooClient(url=user.tenant, db=user.database))
        expected_id = odoo.get_employee_telegram_id(user.odoo_uid, secret, field)
    except (OdooError, InvalidToken):
        return "unavailable"
    return "verified" if expected_id == connection.telegram_user_id else "mismatch"


@router.get("/", response_class=HTMLResponse)
def dashboard_page(
    request: Request,
    user_data: tuple[Any, str] | None = Depends(get_current_user_portal),
    auth: AuthService = Depends(get_auth_service),
):
    if not user_data:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    user, csrf_token = user_data
    session_factory = request.app.state.session_factory

    with session_factory() as db:
        user_db = db.query(User).filter_by(id=user.id).first()
        google_conn = db.query(GoogleConnection).filter_by(user_id=user.id).first()
        telegram_conn = db.query(TelegramConnection).filter_by(user_id=user.id).first()
        conflicts = db.query(CalendarPair).filter_by(user_id=user.id, status="conflict").all()
        ambiguous_intents = (
            db.query(SyncIntent).filter_by(user_id=user.id, status="ambiguous").all()
        )

        calendar_active = (
            google_conn is not None
            and google_conn.status == "connected"
            and "calendar" in (google_conn.scopes or "")
        )

        scopes_str = (google_conn.scopes or "") if google_conn and google_conn.status == "connected" else ""
        is_conn = google_conn is not None and google_conn.status == "connected"

        workspace_apps = [
            {
                "id": "gmail",
                "name": "Gmail",
                "active": is_conn and any(s in scopes_str for s in ["gmail", "mail.google.com"]),
                "icon_svg": '<svg width="20" height="20" viewBox="0 0 24 24"><path fill="#EA4335" d="M20 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 4l-8 5-8-5V6l8 5 8-5v2z"/></svg>',
            },
            {
                "id": "calendar",
                "name": "Calendar",
                "active": is_conn and "calendar" in scopes_str,
                "icon_svg": '<svg width="20" height="20" viewBox="0 0 24 24"><path fill="#4285F4" d="M19 3h-1V1h-2v2H8V1H6v2H5c-1.11 0-1.99.9-1.99 2L3 19c0 1.1.89 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm0 16H5V8h14v11zM7 10h5v5H7z"/></svg>',
            },
            {
                "id": "drive",
                "name": "Drive",
                "active": is_conn and "drive" in scopes_str,
                "icon_svg": '<svg width="20" height="20" viewBox="0 0 24 24"><path fill="#34A853" d="M8.2 16.5l3.8-6.5h7.6l-3.8 6.5z"/><path fill="#FBBC05" d="M12 10l-3.8 6.5L4.4 10h7.6z"/><path fill="#4285F4" d="M15.8 3.5H8.2L4.4 10l3.8 6.5 7.6-13z"/></svg>',
            },
            {
                "id": "docs",
                "name": "Docs",
                "active": is_conn and any(s in scopes_str for s in ["documents", "docs", "drive"]),
                "icon_svg": '<svg width="20" height="20" viewBox="0 0 24 24"><path fill="#4285F4" d="M14 2H6c-1.1 0-1.99.9-1.99 2L4 20c0 1.1.89 2 1.99 2H18c1.1 0 2-.9 2-2V8l-6-6zm2 16H8v-2h8v2zm0-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z"/></svg>',
            },
            {
                "id": "sheets",
                "name": "Sheets",
                "active": is_conn and any(s in scopes_str for s in ["spreadsheets", "sheets", "drive"]),
                "icon_svg": '<svg width="20" height="20" viewBox="0 0 24 24"><path fill="#0F9D58" d="M14 2H6c-1.1 0-1.99.9-1.99 2L4 20c0 1.1.89 2 1.99 2H18c1.1 0 2-.9 2-2V8l-6-6zm-1 16H8v-2h5v2zm0-4H8v-2h5v2zm3 4h-2v-2h2v2zm0-4h-2v-2h2v2zm-3-5V3.5L18.5 9H13z"/></svg>',
            },
            {
                "id": "slides",
                "name": "Slides",
                "active": is_conn and any(s in scopes_str for s in ["presentations", "drive"]),
                "icon_svg": '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#F9AB00" stroke-width="2"><rect x="4" y="5" width="16" height="14" rx="2"/><path d="M8 10h8M8 14h5"/></svg>',
            },
        ]

    telegram_state = _telegram_portal_state(request, user, telegram_conn, auth)
    telegram_verified = telegram_state == "verified"
    settings = request.app.state.settings
    bot_username = settings.TELEGRAM_BOT_USERNAME.strip().lstrip("@")
    telegram_bot_url = None
    if telegram_verified and re.fullmatch(r"[A-Za-z0-9_]+", bot_username):
        telegram_bot_url = f"https://t.me/{bot_username}"

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "user": user_db,
            "google_conn": google_conn,
            "telegram_conn": telegram_conn,
            "telegram_verified": telegram_verified,
            "telegram_state": telegram_state,
            "telegram_bot_url": telegram_bot_url,
            "telegram_web_enabled": bool(request.app.state.settings.ODOO_EMPLOYEE_TELEGRAM_ID_FIELD),
            "telegram_web_configured": request.app.state.settings.telegram_web_configured,
            "conflicts": conflicts,
            "ambiguous_intents": ambiguous_intents,
            "calendar_active": calendar_active,
            "workspace_apps": workspace_apps,
            "csrf_token": csrf_token,
        },
    )


@router.get("/connections/telegram/verification-status")
def telegram_verification_status(
    request: Request,
    user_data: tuple[Any, str] | None = Depends(get_current_user_portal),
    auth: AuthService = Depends(get_auth_service),
):
    if not user_data:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    user, _ = user_data
    with request.app.state.session_factory() as db:
        connection = db.query(TelegramConnection).filter_by(user_id=user.id).first()
        revision = connection.updated_at.isoformat() if connection and connection.updated_at else ""
    state = _telegram_portal_state(request, user, connection, auth)
    return JSONResponse(
        {"verified": state == "verified", "state": state, "revision": revision},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/connections/telegram/verification-options", response_class=HTMLResponse)
def telegram_verification_options(
    request: Request,
    user_data: tuple[Any, str] | None = Depends(get_current_user_portal),
):
    if not user_data:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    settings = request.app.state.settings
    if not settings.ODOO_EMPLOYEE_TELEGRAM_ID_FIELD:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    user, csrf_token = user_data
    return templates.TemplateResponse(
        request=request,
        name="telegram_verification_page.html",
        context={
            "user": user,
            "csrf_token": csrf_token,
            "telegram_web_configured": settings.telegram_web_configured,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/calendar/enable")
def enable_calendar_sync(
    request: Request,
    user_data: tuple[Any, str] | None = Depends(get_current_user_portal),
    csrf_token: str = Form(""),
):
    if not user_data:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    user, actual_csrf = user_data
    if csrf_token != actual_csrf:
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    # Calendar sync enabled state is tied to Google connection scopes
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/calendar/sync")
def sync_calendar_now(
    request: Request,
    user_data: tuple[Any, str] | None = Depends(get_current_user_portal),
    csrf_token: str = Form(""),
):
    if not user_data:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    user, actual_csrf = user_data
    if csrf_token != actual_csrf:
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    settings = request.app.state.settings
    session_factory = request.app.state.session_factory
    from urllib.parse import quote

    from app.calendar.service import CalendarSyncService
    from app.google.client import GoogleClient
    from app.google.oauth import GoogleOAuthService
    from app.odoo.client import OdooClient

    odoo = OdooClient(settings.ODOO_URL, settings.ODOO_DB)
    oauth = GoogleOAuthService(
        session_factory,
        settings.GOOGLE_CLIENT_ID,
        settings.GOOGLE_CLIENT_SECRET,
        settings.google_redirect_uri,
        settings.ENCRYPTION_KEY or "",
        settings.GOOGLE_WORKSPACE_DOMAINS,
    )
    gclient = GoogleClient(oauth)
    cal_sync = CalendarSyncService(session_factory, odoo, gclient, settings.ENCRYPTION_KEY or "")

    try:
        res = cal_sync.sync_user(user.id)
        msg = f"Đã đồng bộ xong! (Tạo Odoo: {res.created_in_odoo}, Tạo Google: {res.created_in_google}, Xóa Odoo: {res.deleted_in_odoo}, Xóa Google: {res.deleted_in_google}, Cập nhật: {res.updated_in_odoo + res.updated_in_google}, Bỏ qua/Khớp: {res.skipped})"
        return RedirectResponse(
            url=f"/?status=sync_success&msg={quote(msg)}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    except Exception as e:
        return RedirectResponse(
            url=f"/?error={quote(f'Lỗi đồng bộ: {str(e)}')}",
            status_code=status.HTTP_303_SEE_OTHER,
        )


@router.post("/calendar/pause")
def pause_calendar_sync(
    request: Request,
    user_data: tuple[Any, str] | None = Depends(get_current_user_portal),
    csrf_token: str = Form(""),
):
    if not user_data:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    user, actual_csrf = user_data
    if csrf_token != actual_csrf:
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    # In MVP, pause can toggle connection status or flag
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/calendar/pairs/{pair_id}/resolve")
def resolve_conflict(
    pair_id: int,
    request: Request,
    source: str = Form(...),
    expected_version: int = Form(1),
    csrf_token: str = Form(""),
    user_data: tuple[Any, str] | None = Depends(get_current_user_portal),
):
    if not user_data:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    user, actual_csrf = user_data
    if csrf_token != actual_csrf:
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    # Call CalendarSyncService.resolve_conflict
    cal_sync = getattr(request.app.state, "calendar_sync", None)
    if cal_sync:
        try:
            cal_sync.resolve_conflict(
                user_id=user.id,
                pair_id=pair_id,
                source=source,
                expected_version=expected_version,
            )
        except Exception:
            pass
    else:
        # Fallback direct state update
        session_factory = request.app.state.session_factory
        with session_factory() as db:
            pair = db.query(CalendarPair).filter_by(id=pair_id, user_id=user.id).first()
            if pair:
                pair.status = "synced"
                db.commit()

    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/calendar/intents/{intent_id}/reconcile")
def reconcile_intent(
    intent_id: int,
    request: Request,
    action: str = Form("link"),
    odoo_event_id: int | None = Form(None),
    csrf_token: str = Form(""),
    user_data: tuple[Any, str] | None = Depends(get_current_user_portal),
):
    if not user_data:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    user, actual_csrf = user_data
    if csrf_token != actual_csrf:
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    session_factory = request.app.state.session_factory
    with session_factory() as db:
        intent = db.query(SyncIntent).filter_by(id=intent_id, user_id=user.id).first()
        if intent:
            if action == "link" and odoo_event_id:
                intent.status = "success"
                intent.target_id = str(odoo_event_id)
            elif action == "retry":
                intent.status = "pending"
            db.commit()

    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
