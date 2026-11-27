"""Shared outbound clients and Odoo account verification for Gateway routes."""

from typing import Any

from fastapi import HTTPException, Request, status

from app.google.client import GoogleClient
from app.google.oauth import GoogleOAuthService
from app.models import User
from app.odoo.client import (
    OdooAccessError,
    OdooAccountInactiveError,
    OdooAuthError,
    OdooClient,
    OdooExternalUserError,
    OdooNetworkError,
)
from app.odoo.service import OdooService


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


def _get_google_client(request: Request) -> GoogleClient:
    gc = getattr(request.app.state, "google_client", None)
    if gc is not None:
        return gc
    settings = request.app.state.settings
    session_factory = request.app.state.session_factory
    redirect_uri = settings.google_redirect_uri
    oauth_service = GoogleOAuthService(
        session_factory=session_factory,
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        redirect_uri=redirect_uri,
        encryption_key=settings.ENCRYPTION_KEY or "",
        allowed_domains=settings.GOOGLE_WORKSPACE_DOMAINS,
    )
    gc = GoogleClient(oauth_service)
    request.app.state.google_client = gc
    return gc


def _get_workspace_service(request: Request):
    ws = getattr(request.app.state, "workspace_service", None)
    if ws is not None:
        return ws
    from app.workspace.service import WorkspaceService
    settings = request.app.state.settings
    google_client = _get_google_client(request)
    ws = WorkspaceService(
        google_client,
        settings.ENCRYPTION_KEY or "default_key_32bytes_min_12345678",
        use_mcp=getattr(settings, "GOOGLE_WORKSPACE_MCP_ENABLED", True),
    )
    request.app.state.workspace_service = ws
    return ws


def _get_odoo_service(request: Request, user: User) -> OdooService | None:
    import threading

    settings = request.app.state.settings
    odoo_url = user.tenant or settings.ODOO_URL
    odoo_db = user.database or settings.ODOO_DB
    if not odoo_url or not odoo_db:
        return None
    state = request.app.state
    lock = getattr(state, "outbound_clients_lock", None)
    if lock is None:
        lock = threading.Lock()
        state.outbound_clients_lock = lock
    clients = getattr(state, "odoo_clients", None)
    if clients is None:
        clients = {}
        state.odoo_clients = clients
    with lock:
        key = (odoo_url, odoo_db)
        client = clients.get(key)
        if client is None:
            client = OdooClient(url=odoo_url, db=odoo_db)
            clients[key] = client
    return OdooService(client)
