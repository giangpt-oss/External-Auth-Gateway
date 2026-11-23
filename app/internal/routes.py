"""Aggregate internal router and compatibility exports for existing callers."""

from fastapi import APIRouter, Depends

from app.internal import actions, google, identity, odoo
from app.internal.actions import (
    UserActionRequest,
    execute_user_action,
    get_user_action_status,
)
from app.internal.dependencies import (
    _get_google_client,
    _get_odoo_service,
    _get_workspace_service,
    _verify_odoo_account,
)
from app.internal.google import (
    get_calendar_event_detail,
    get_calendar_events,
    get_unread_emails,
)
from app.internal.identity import (
    IdentityResolveRequest,
    get_user_connections,
    resolve_identity,
)
from app.internal.odoo import (
    OdooQueryRequest,
    get_crm_lead_detail,
    get_crm_leads,
    get_odoo_catalog,
    query_odoo_records,
)
from app.internal.security import verify_agent_secret

__all__ = [
    "IdentityResolveRequest",
    "OdooQueryRequest",
    "UserActionRequest",
    "_get_google_client",
    "_get_odoo_service",
    "_get_workspace_service",
    "_verify_odoo_account",
    "execute_user_action",
    "get_calendar_event_detail",
    "get_calendar_events",
    "get_crm_lead_detail",
    "get_crm_leads",
    "get_odoo_catalog",
    "get_unread_emails",
    "get_user_action_status",
    "get_user_connections",
    "query_odoo_records",
    "resolve_identity",
    "router",
]

router = APIRouter(
    prefix="/internal/v1",
    tags=["Internal Integration"],
    dependencies=[Depends(verify_agent_secret)],
)
router.include_router(identity.router)
router.include_router(google.router)
router.include_router(odoo.router)
router.include_router(actions.router)
