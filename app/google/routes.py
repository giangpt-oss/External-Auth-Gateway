from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from app.auth.routes import get_auth_service, get_current_user
from app.auth.service import AuthenticatedUser, AuthService
from app.config import Settings
from app.google.account_resolver import GoogleAccountResolver
from app.google.account_selection import AccountSelectionManager, SwitchLockError
from app.google.oauth import GoogleOAuthError, GoogleOAuthService
from app.models import LinkToken, Session
from app.security import generate_secure_token, hash_token
from app.workspace.activity_tracker import WorkspaceActivityTracker

router = APIRouter(tags=["Google Workspace"])


def get_google_oauth_service(request: Request) -> GoogleOAuthService:
    settings: Settings = request.app.state.settings
    session_factory = request.app.state.session_factory
    redirect_uri = settings.google_redirect_uri

    return GoogleOAuthService(
        session_factory=session_factory,
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        redirect_uri=redirect_uri,
        encryption_key=settings.ENCRYPTION_KEY or "dev_dummy_key_000000000000000000000=",
        allowed_domains=settings.GOOGLE_WORKSPACE_DOMAINS,
    )


@router.post("/connections/google/start")
async def start_google_oauth(
    request: Request,
    current_user: AuthenticatedUser = Depends(get_current_user),
    google_service: GoogleOAuthService = Depends(get_google_oauth_service),
    session_id: str | None = Cookie(None),
    auth_service: AuthService = Depends(get_auth_service),
    services: str | None = Query(None, description="Comma-separated Workspace service names"),
    mode: str | None = Query(None, description="'add' or 'reconnect'"),
    account_id: int | None = Query(None, description="Target account id for reconnect"),
):
    form_data = {}
    content_type = request.headers.get("content-type", "")
    if "form" in content_type:
        form = await request.form()
        form_data = dict(form)

    req_mode = mode or form_data.get("mode") or "add"
    raw_account_id = account_id or form_data.get("account_id")
    req_account_id = int(raw_account_id) if raw_account_id and str(raw_account_id).isdigit() else None
    req_services = services or form_data.get("services")

    session_rec_id = None
    if session_id:
        res = auth_service.get_user_by_session(session_id)
        if res:
            _, s_rec = res
            session_rec_id = s_rec.id

    try:
        svc_list = [s.strip() for s in req_services.split(",") if s.strip()] if req_services else None
        auth_url, _ = google_service.begin(
            user_id=current_user.id,
            session_id=session_rec_id,
            services=svc_list,
            mode=req_mode,
            target_account_id=req_account_id,
        )
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc

    is_html = "text/html" in request.headers.get("Accept", "") or "form" in content_type
    if is_html:
        return RedirectResponse(url=auth_url, status_code=303)
    return {"status": "success", "auth_url": auth_url}


@router.get("/oauth/google/callback")
def google_oauth_callback(
    request: Request,
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
    google_service: GoogleOAuthService = Depends(get_google_oauth_service),
    session_id: str | None = Cookie(None),
    auth_service: AuthService = Depends(get_auth_service),
):
    if error:
        return RedirectResponse(url=f"/?error=google_{error}", status_code=303)

    if not code or not state:
        return RedirectResponse(url="/?error=missing_oauth_params", status_code=303)

    # Resolve user from current session or fallback to valid OAuth state token
    auth_res = auth_service.get_user_by_session(session_id) if session_id else None
    user_id = None
    if auth_res:
        current_user, _ = auth_res
        user_id = current_user.id
    else:
        with google_service.session_factory() as db:
            lt = (
                db.query(LinkToken)
                .filter(
                    LinkToken.token_hash == hash_token(state),
                    LinkToken.purpose.like("google_oauth%"),
                    LinkToken.used_at.is_(None),
                    LinkToken.expires_at > datetime.now(UTC),
                )
                .first()
            )
            if lt:
                user_id = lt.user_id

    if not user_id:
        return RedirectResponse(url="/login?error=session_required_for_oauth", status_code=303)

    try:
        google_service.complete(user_id=user_id, state=state, code=code)

        settings: Settings = request.app.state.settings
        resp = RedirectResponse(url="/?status=google_connected", status_code=303)

        # Nếu session cookie bị mất trong vòng OAuth redirect (cross-origin),
        # tạo session mới để người dùng không phải đăng nhập lại vào dashboard
        if not session_id:
            session_token = generate_secure_token(32)
            token_hash = hash_token(session_token)
            csrf_secret = generate_secure_token(32)
            now = datetime.now(UTC)
            expires_at = now + timedelta(hours=settings.SESSION_LIFETIME_HOURS)

            with google_service.session_factory() as db:
                db.query(Session).filter_by(user_id=user_id).delete()
                session_record = Session(
                    token_hash=token_hash,
                    user_id=user_id,
                    csrf_secret=csrf_secret,
                    expires_at=expires_at,
                )
                db.add(session_record)
                db.commit()

            is_production = settings.APP_ENV == "production"
            resp.set_cookie(
                key="session_id",
                value=session_token,
                max_age=settings.SESSION_LIFETIME_HOURS * 3600,
                httponly=True,
                samesite="lax",
                secure=is_production,
                path="/",
            )

        return resp
    except GoogleOAuthError as e:
        return RedirectResponse(url=f"/?error={str(e)}", status_code=303)


@router.get("/connections/google/accounts")
def list_google_accounts(
    current_user: AuthenticatedUser = Depends(get_current_user),
    google_service: GoogleOAuthService = Depends(get_google_oauth_service),
):
    with google_service.session_factory() as db:
        connections = GoogleAccountResolver.get_user_connections(db, current_user.id)
        selection = GoogleAccountResolver.get_selection(db, current_user.id)

        blockers = []
        if selection and selection.active_account_id:
            blockers = WorkspaceActivityTracker.get_active_blockers(
                db, current_user.id, account_id=selection.active_account_id
            )

        can_switch = (
            selection is not None
            and selection.state == "draining"
            and len(blockers) == 0
        )

        return {
            "accounts": [
                {
                    "id": c.id,
                    "email": c.email,
                    "status": c.status,
                    "scopes": c.scopes.split() if c.scopes else [],
                    "is_active": (selection is not None and selection.active_account_id == c.id),
                    "created_at": c.created_at.isoformat() if getattr(c, "created_at", None) else None,
                }
                for c in connections
            ],
            "active_account_id": selection.active_account_id if selection else None,
            "selection_version": selection.selection_version if selection else 1,
            "state": selection.state if selection else "accepting",
            "target_account_id": selection.target_account_id if selection else None,
            "can_switch": can_switch,
            "blockers": blockers,
        }


@router.post("/connections/google/accounts/{account_id}/activate")
def activate_google_account(
    account_id: int,
    request: Request,
    current_user: AuthenticatedUser = Depends(get_current_user),
    google_service: GoogleOAuthService = Depends(get_google_oauth_service),
    current_version: int | None = Query(None),
):
    """Switch atomically when idle. A pending switch requires explicit confirmation."""
    is_html = "text/html" in request.headers.get("Accept", "") or "form" in request.headers.get("Content-Type", "")
    with google_service.session_factory() as db:
        try:
            result = AccountSelectionManager.request_switch(
                db, current_user.id, account_id, activate_if_idle=True, current_version=current_version,
            )
        except SwitchLockError as exc:
            if is_html:
                return RedirectResponse("/?" + urlencode({"error": exc.message}), status_code=303)
            raise HTTPException(
                status_code=409, detail={"code": exc.code, "message": exc.message, "blockers": exc.blockers},
            ) from exc

    if is_html:
        redirect_status = {"switched": "account_switched", "already_active": "", "busy": "switch_busy",
                           "ready_to_switch": "switch_busy"}[result["status"]]
        return RedirectResponse(f"/?status={redirect_status}" if redirect_status else "/", status_code=303)
    return result


@router.post("/connections/google/accounts/{account_id}/confirm")
def confirm_google_account_switch(
    account_id: int,
    request: Request,
    current_user: AuthenticatedUser = Depends(get_current_user),
    google_service: GoogleOAuthService = Depends(get_google_oauth_service),
    current_version: int | None = Query(None, description="Phiên bản lựa chọn đang chờ xác nhận"),
):
    """Lần bấm thứ hai vào tài khoản B khi A đã xong việc: xác nhận chuyển (confirm_switch).

    Kiểm tra lại blocker dưới khóa. Nếu A vẫn bận -> từ chối, giữ draining.
    Nếu A đã xong -> chuyển sang B, đặt trạng thái accepting.
    """
    with google_service.session_factory() as db:
        try:
            result = AccountSelectionManager.confirm_switch(
                db, current_user.id, account_id, current_version=current_version
            )
        except SwitchLockError as exc:
            is_html = "text/html" in request.headers.get("Accept", "") or "form" in request.headers.get(
                "Content-Type", ""
            )
            if is_html:
                return RedirectResponse("/?" + urlencode({"error": exc.message}), status_code=303)
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code, "message": exc.message, "blockers": exc.blockers},
            ) from exc

        is_html = "text/html" in request.headers.get("Accept", "") or "form" in request.headers.get(
            "Content-Type", ""
        )
        if is_html:
            return RedirectResponse(url="/?status=account_switched", status_code=303)
        return {
            "status": "switched",
            "previous_account_id": result["previous_account_id"],
            "active_account_id": result["active_account_id"],
            "active_email": result["active_email"],
            "selection_version": result["selection_version"],
            "state": result["state"],
        }


@router.post("/connections/google/resume")
def resume_google_account(
    request: Request,
    current_user: AuthenticatedUser = Depends(get_current_user),
    google_service: GoogleOAuthService = Depends(get_google_oauth_service),
    current_version: int | None = Query(None),
):
    is_html = "text/html" in request.headers.get("Accept", "") or "form" in request.headers.get("Content-Type", "")
    with google_service.session_factory() as db:
        try:
            res = AccountSelectionManager.resume_current(db, current_user.id, current_version=current_version)
            if is_html:
                return RedirectResponse(url="/", status_code=303)
            return res
        except SwitchLockError as exc:
            if is_html:
                return RedirectResponse("/?" + urlencode({"error": exc.message}), status_code=303)
            raise HTTPException(status_code=400, detail={"code": exc.code, "message": exc.message}) from exc


@router.post("/connections/google/accounts/{account_id}/disconnect")
def disconnect_google_account(
    account_id: int,
    request: Request,
    current_user: AuthenticatedUser = Depends(get_current_user),
    google_service: GoogleOAuthService = Depends(get_google_oauth_service),
):
    try:
        google_service.disconnect(user_id=current_user.id, google_account_id=account_id)
    except GoogleOAuthError as exc:
        is_html = "text/html" in request.headers.get("Accept", "") or "form" in request.headers.get(
            "Content-Type", ""
        )
        if is_html:
            return RedirectResponse(url="/?error=disconnect_blocked", status_code=303)
        raise HTTPException(
            status_code=409,
            detail={"code": "ACCOUNT_BUSY", "message": str(exc)},
        ) from exc

    is_html = "text/html" in request.headers.get("Accept", "") or "form" in request.headers.get(
        "Content-Type", ""
    )
    if is_html:
        return RedirectResponse(url="/", status_code=303)
    return {"status": "success", "message": "Google account disconnected successfully"}


@router.post("/connections/google/accounts/{account_id}/delete")
def delete_google_account(
    account_id: int,
    request: Request,
    current_user: AuthenticatedUser = Depends(get_current_user),
    google_service: GoogleOAuthService = Depends(get_google_oauth_service),
):
    try:
        google_service.delete(user_id=current_user.id, google_account_id=account_id)
    except GoogleOAuthError as exc:
        is_html = "text/html" in request.headers.get("Accept", "") or "form" in request.headers.get(
            "Content-Type", ""
        )
        if is_html:
            return RedirectResponse(url="/?error=delete_blocked", status_code=303)
        raise HTTPException(
            status_code=409,
            detail={"code": "ACCOUNT_BUSY", "message": str(exc)},
        ) from exc

    is_html = "text/html" in request.headers.get("Accept", "") or "form" in request.headers.get(
        "Content-Type", ""
    )
    if is_html:
        return RedirectResponse(url="/", status_code=303)
    return {"status": "success", "message": "Google account deleted successfully"}


@router.post("/connections/google/disconnect")
def disconnect_google(
    request: Request,
    current_user: AuthenticatedUser = Depends(get_current_user),
    google_service: GoogleOAuthService = Depends(get_google_oauth_service),
):
    try:
        google_service.disconnect(user_id=current_user.id)
    except GoogleOAuthError as exc:
        is_html = "text/html" in request.headers.get("Accept", "") or "form" in request.headers.get(
            "Content-Type", ""
        )
        if is_html:
            return RedirectResponse(url="/?error=disconnect_blocked", status_code=303)
        raise HTTPException(
            status_code=409,
            detail={"code": "ACCOUNT_BUSY", "message": str(exc)},
        ) from exc

    is_html = "text/html" in request.headers.get("Accept", "") or "form" in request.headers.get(
        "Content-Type", ""
    )
    if is_html:
        return RedirectResponse(url="/", status_code=303)
    return {"status": "success", "message": "Google account disconnected successfully"}
