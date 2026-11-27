import re
import secrets
from hashlib import sha256
from pathlib import Path
from typing import Any

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth.routes import get_auth_service
from app.auth.service import AuthenticatedUser, AuthService
from app.google.account_resolver import GoogleAccountResolver
from app.models import TelegramConnection, User
from app.odoo.client import OdooClient, OdooError
from app.odoo.service import OdooService
from app.portal.workspace_status import workspace_app_statuses

router = APIRouter(tags=["Portal"])

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["portal_assets"] = {
    name: sha256((TEMPLATES_DIR.parent / "static" / name).read_bytes()).hexdigest()[:16]
    for name in ("style.css", "app.js", "stitch.js")
}


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
    anon_csrf = request.cookies.get("anon_csrf") or secrets.token_urlsafe(16)
    response = templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "csrf_token": anon_csrf,
            "user": None,
            "error": None,
        },
    )
    if not request.cookies.get("anon_csrf"):
        response.set_cookie("anon_csrf", anon_csrf, httponly=True, samesite="lax")
    return response


def _telegram_portal_state(
    request: Request, user: AuthenticatedUser, connection: TelegramConnection | None, auth: AuthService,
) -> str:
    if not connection or connection.status != "connected" or not connection.verified_on_web:
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


def _telegram_context(request: Request, user: AuthenticatedUser, connection: TelegramConnection | None, auth: AuthService) -> dict:
    state = _telegram_portal_state(request, user, connection, auth)
    linked = connection is not None and connection.status == "connected"
    if state == "verified":
        label, tone = "Đã xác minh", "success"
    elif not linked:
        label, tone = "Chưa liên kết", ""
    elif request.app.state.settings.ODOO_EMPLOYEE_TELEGRAM_ID_FIELD:
        label, tone = "Cần xác minh", "warning"
    else:
        label, tone = "Đã liên kết", "success"
    return {
        "telegram_conn": connection,
        "telegram_state": state,
        "telegram_label": label,
        "telegram_tone": tone,
        "telegram_linked": linked,
        "telegram_revision": connection.updated_at.isoformat() if connection and connection.updated_at else "",
    }


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
        google_connections = GoogleAccountResolver.get_user_connections(db, user.id)
        google_conn, selection = GoogleAccountResolver.get_active_with_selection(db, user.id)

        from app.workspace.activity_tracker import WorkspaceActivityTracker
        active_blockers = []
        if selection and selection.active_account_id:
            active_blockers = WorkspaceActivityTracker.get_active_blockers(
                db, user.id, account_id=selection.active_account_id
            )
        can_switch = (
            selection is not None
            and selection.state == "draining"
            and len(active_blockers) == 0
        )

        target_conn = None
        if selection and selection.target_account_id:
            target_conn = GoogleAccountResolver.get_account(db, user.id, selection.target_account_id)

        telegram_conn = db.query(TelegramConnection).filter_by(user_id=user.id).first()

        workspace_apps = workspace_app_statuses(google_conn)

    telegram = _telegram_context(request, user, telegram_conn, auth)
    settings = request.app.state.settings
    bot_username = settings.TELEGRAM_BOT_USERNAME.strip().lstrip("@")
    telegram_bot_url = None
    if (telegram["telegram_state"] == "verified" or not settings.ODOO_EMPLOYEE_TELEGRAM_ID_FIELD and telegram["telegram_linked"]) and re.fullmatch(r"[A-Za-z0-9_]+", bot_username):
        telegram_bot_url = f"https://t.me/{bot_username}"

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "user": user_db,
            "google_conn": google_conn,
            "google_connections": google_connections,
            "google_selection": selection,
            "active_blockers": active_blockers,
            "can_switch": can_switch,
            "target_conn": target_conn,
            **telegram,
            "telegram_bot_url": telegram_bot_url,
            "telegram_web_enabled": bool(request.app.state.settings.ODOO_EMPLOYEE_TELEGRAM_ID_FIELD),
            "telegram_web_configured": settings.telegram_web_configured or bool(
                not settings.ODOO_EMPLOYEE_TELEGRAM_ID_FIELD
                and settings.TELEGRAM_BOT_TOKEN and bot_username
            ),
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
    telegram = _telegram_context(request, user, connection, auth)
    return JSONResponse(
        {"verified": telegram["telegram_state"] == "verified", "state": telegram["telegram_state"],
         "linked": telegram["telegram_linked"], "revision": telegram["telegram_revision"]},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/connections/telegram/verification-options", response_class=HTMLResponse)
def telegram_verification_options(
    request: Request,
    user_data: tuple[Any, str] | None = Depends(get_current_user_portal),
    auth: AuthService = Depends(get_auth_service),
):
    if not user_data:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    settings = request.app.state.settings
    if not settings.ODOO_EMPLOYEE_TELEGRAM_ID_FIELD:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    user, csrf_token = user_data
    with request.app.state.session_factory() as db:
        connection = db.query(TelegramConnection).filter_by(user_id=user.id).first()
    return templates.TemplateResponse(
        request=request,
        name="telegram_verification_page.html",
        context={
            "user": user,
            "csrf_token": csrf_token,
            "telegram_web_configured": settings.telegram_web_configured or bool(
                not settings.ODOO_EMPLOYEE_TELEGRAM_ID_FIELD
                and settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_BOT_USERNAME
            ),
            **_telegram_context(request, user, connection, auth),
        },
        headers={"Cache-Control": "no-store"},
    )
