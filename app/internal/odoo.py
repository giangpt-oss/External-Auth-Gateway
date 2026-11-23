"""Internal odoo endpoints."""

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel

from app.models import User
from app.odoo.catalog import get_catalog_overview
from app.odoo.client import (
    OdooAccessError,
    OdooNetworkError,
)
from app.security import decrypt_secret

router = APIRouter()


@router.get("/users/{user_id}/crm/leads")
def get_crm_leads(
    user_id: int,
    request: Request,
    search_term: str | None = Query(None, description="Search term for lead name"),
    limit: int = Query(5, ge=1, le=50, description="Max leads to return"),
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
        # Xác minh tài khoản Odoo trước khi đọc CRM
        dependencies._verify_odoo_account(odoo_svc, user.odoo_uid, secret)

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


@router.get("/users/{user_id}/crm/leads/{lead_id}")
def get_crm_lead_detail(
    user_id: int,
    lead_id: int,
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


class OdooQueryRequest(BaseModel):
    model: str
    fields: list[str] | None = None
    domain: list[Any] | None = None
    limit: int | None = 10
    offset: int | None = 0
    order: str | None = None
    count_only: bool | None = False


@router.get("/odoo/catalog")
def get_odoo_catalog():
    """Trả về danh mục models và fields Odoo an toàn được hỗ trợ."""
    return {"status": "OK", "catalog": get_catalog_overview()}


@router.post("/users/{user_id}/odoo/query")
def query_odoo_records(
    user_id: int,
    req: OdooQueryRequest,
    request: Request,
):
    """Truy vấn dữ liệu Odoo an toàn theo Whitelist Catalog dưới quyền của chính user."""
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

        try:
            res = odoo_svc.execute_catalog_query(
                uid=user.odoo_uid,
                secret=secret,
                model=req.model,
                fields=req.fields,
                domain=req.domain,
                limit=req.limit,
                offset=req.offset,
                order=req.order,
                count_only=bool(req.count_only),
            )
            return {
                "status": "OK",
                **res,
            }
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "INVALID_QUERY", "message": str(e)},
            ) from e
        except OdooAccessError as e:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "PERMISSION_DENIED", "message": f"Tài khoản Odoo không có quyền truy cập {req.model}: {e}"},
            ) from e
        except OdooNetworkError as e:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "ODOO_UNAVAILABLE", "message": f"Không thể kết nối Odoo: {e}"},
            ) from e
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"code": "SERVICE_ERROR", "message": str(e)},
            ) from e
