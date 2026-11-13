from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse

from app.auth.service import (
    AuthenticatedUser,
    AuthError,
    AuthService,
    InactiveUserError,
    PortalUserError,
    RateLimitExceeded,
)
from app.config import Settings
from app.odoo.client import OdooAuthError, OdooClient, OdooNetworkError

router = APIRouter(tags=["Authentication"])


def get_auth_service(request: Request) -> AuthService:
    settings: Settings = request.app.state.settings
    session_factory = request.app.state.session_factory
    odoo_client = getattr(request.app.state, "odoo_client", None)
    if not odoo_client:
        odoo_client = OdooClient(url=settings.ODOO_URL, db=settings.ODOO_DB)

    return AuthService(
        session_factory=session_factory,
        odoo_client=odoo_client,
        tenant=settings.ODOO_URL,
        database=settings.ODOO_DB,
        encryption_key=settings.ENCRYPTION_KEY or "dev_dummy_key_000000000000000000000=",
        session_lifetime_hours=settings.SESSION_LIFETIME_HOURS,
    )


def get_current_user(
    request: Request,
    session_id: str | None = Cookie(None),
    auth_service: AuthService = Depends(get_auth_service),
) -> AuthenticatedUser:
    token = session_id
    if not token and "Authorization" in request.headers:
        auth_header = request.headers["Authorization"]
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "UNAUTHORIZED", "message": "Authentication required"},
        )

    result = auth_service.get_user_by_session(token)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "SESSION_EXPIRED", "message": "Session expired or invalid"},
        )

    auth_user, _ = result
    return auth_user


@router.post("/auth/login")
def login(
    request: Request,
    response: Response,
    login: str = Form(...),
    secret: str = Form(...),
    credential_kind: str = Form("password"),
    auth_service: AuthService = Depends(get_auth_service),
):
    client_ip = request.client.host if request.client else "127.0.0.1"
    settings: Settings = request.app.state.settings

    try:
        auth_user, session_token, csrf_secret = auth_service.login(
            login=login,
            secret=secret,
            credential_kind=credential_kind,
            client_ip=client_ip,
        )

        is_production = settings.APP_ENV == "production"
        is_html = "text/html" in request.headers.get("Accept", "")
        if is_html:
            resp = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
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

        response.set_cookie(
            key="session_id",
            value=session_token,
            max_age=settings.SESSION_LIFETIME_HOURS * 3600,
            httponly=True,
            samesite="lax",
            secure=is_production,
            path="/",
        )

        return {
            "status": "success",
            "user": {
                "id": auth_user.id,
                "odoo_uid": auth_user.odoo_uid,
                "name": auth_user.name,
                "login": auth_user.login,
                "tz": auth_user.tz,
            },
            "csrf_token": csrf_secret,
        }

    except RateLimitExceeded as e:
        is_html = "text/html" in request.headers.get("Accept", "")
        if is_html:
            from app.portal.routes import templates

            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={
                    "csrf_token": request.cookies.get("anon_csrf", ""),
                    "user": None,
                    "error": f"Quá nhiều lần thử: {e}",
                },
                status_code=429,
            )
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"error": {"code": "RATE_LIMITED", "message": str(e)}},
        )
    except (OdooAuthError, AuthError) as e:
        is_html = "text/html" in request.headers.get("Accept", "")
        if is_html:
            from app.portal.routes import templates

            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={
                    "csrf_token": request.cookies.get("anon_csrf", ""),
                    "user": None,
                    "error": "Thông tin đăng nhập không chính xác hoặc Odoo từ chối xác thực.",
                },
                status_code=401,
            )
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"error": {"code": "INVALID_CREDENTIALS", "message": str(e)}},
        )
    except (InactiveUserError, PortalUserError) as e:
        is_html = "text/html" in request.headers.get("Accept", "")
        if is_html:
            from app.portal.routes import templates

            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={
                    "csrf_token": request.cookies.get("anon_csrf", ""),
                    "user": None,
                    "error": "Tài khoản của bạn đã bị vô hiệu hóa hoặc là tài khoản Portal không được phép truy cập.",
                },
                status_code=403,
            )
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"error": {"code": "ACCESS_DENIED", "message": str(e)}},
        )
    except OdooNetworkError as e:
        is_html = "text/html" in request.headers.get("Accept", "")
        if is_html:
            from app.portal.routes import templates

            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={
                    "csrf_token": request.cookies.get("anon_csrf", ""),
                    "user": None,
                    "error": "Không thể kết nối đến máy chủ Odoo. Vui lòng thử lại sau.",
                },
                status_code=502,
            )
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"error": {"code": "ODDO_UNAVAILABLE", "message": str(e)}},
        )


@router.post("/auth/logout")
def logout(
    request: Request,
    response: Response,
    session_id: str | None = Cookie(None),
    auth_service: AuthService = Depends(get_auth_service),
):
    if session_id:
        auth_service.logout(session_id)

    is_html = "text/html" in request.headers.get("Accept", "")
    if is_html:
        resp = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
        resp.delete_cookie(key="session_id", path="/")
        return resp

    response.delete_cookie(key="session_id", path="/")
    return {"status": "success", "message": "Logged out successfully"}


@router.get("/api/me")
def me(current_user: AuthenticatedUser = Depends(get_current_user)):
    return {
        "id": current_user.id,
        "odoo_uid": current_user.odoo_uid,
        "login": current_user.login,
        "name": current_user.name,
        "partner_id": current_user.partner_id,
        "tz": current_user.tz,
        "active": current_user.active,
    }
