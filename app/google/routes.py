from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from app.auth.routes import get_auth_service, get_current_user
from app.auth.service import AuthenticatedUser, AuthService
from app.config import Settings
from app.google.oauth import GoogleOAuthError, GoogleOAuthService

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
def start_google_oauth(
    request: Request,
    current_user: AuthenticatedUser = Depends(get_current_user),
    google_service: GoogleOAuthService = Depends(get_google_oauth_service),
    session_id: str | None = Cookie(None),
    auth_service: AuthService = Depends(get_auth_service),
    services: str | None = Query(None, description="Comma-separated Workspace service names; omitted requests all supported services"),
):
    session_rec_id = None
    if session_id:
        res = auth_service.get_user_by_session(session_id)
        if res:
            _, s_rec = res
            session_rec_id = s_rec.id

    try:
        if services is None:
            auth_url, _ = google_service.begin(user_id=current_user.id, session_id=session_rec_id)
        else:
            auth_url, _ = google_service.begin(user_id=current_user.id, session_id=session_rec_id, services=[s.strip() for s in services.split(",") if s.strip()])
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    is_html = "text/html" in request.headers.get("Accept", "") or "form" in request.headers.get(
        "Content-Type", ""
    )
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
        from datetime import UTC, datetime

        from app.models import LinkToken
        from app.security import hash_token
        with google_service.session_factory() as db:
            lt = (
                db.query(LinkToken)
                .filter(
                    LinkToken.token_hash == hash_token(state),
                    LinkToken.purpose == "google_oauth",
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
        return RedirectResponse(url="/?status=google_connected", status_code=303)
    except GoogleOAuthError as e:
        return RedirectResponse(url=f"/?error={str(e)}", status_code=303)


@router.post("/connections/google/disconnect")
def disconnect_google(
    request: Request,
    current_user: AuthenticatedUser = Depends(get_current_user),
    google_service: GoogleOAuthService = Depends(get_google_oauth_service),
):
    google_service.disconnect(user_id=current_user.id)
    is_html = "text/html" in request.headers.get("Accept", "") or "form" in request.headers.get(
        "Content-Type", ""
    )
    if is_html:
        return RedirectResponse(url="/", status_code=303)
    return {"status": "success", "message": "Google account disconnected successfully"}
