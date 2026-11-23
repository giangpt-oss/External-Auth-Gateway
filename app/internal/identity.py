"""Internal identity endpoints."""

import hashlib
import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from app.google.account_resolver import GoogleAccountResolver
from app.internal.cache import identity_cache
from app.models import TelegramConnection, User
from app.security import decrypt_secret

router = APIRouter()


class IdentityResolveRequest(BaseModel):
    telegram_user_id: int
    chat_id: int
    contract_version: int = 1


@router.post("/identity/resolve")
def resolve_identity(
    req: IdentityResolveRequest,
    request: Request,
):
    from app.internal import routes as dependencies

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

            g_conn, selection = GoogleAccountResolver.get_active_with_selection(db, user.id)
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
                odoo_svc = dependencies._get_odoo_service(request, user)
                # Xác minh Odoo account trước khi cấp quyền
                dependencies._verify_odoo_account(odoo_svc, user.odoo_uid, secret)

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

            selection_ver = selection.selection_version if selection else 1
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
                "telegram_user_id": conn.telegram_user_id,
                "chat_id": conn.private_chat_id,
                "telegram_binding_id": conn.binding_id,
                "google_account_id": g_conn.id if g_conn else None,
                "google_account_email": g_conn.email if g_conn else None,
                "google_selection_version": selection_ver,
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
        google_conn = GoogleAccountResolver.get_active_account(db, user.id)

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
