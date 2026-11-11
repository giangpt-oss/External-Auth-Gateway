"""Internal Workspace API; credentials never leave the Gateway."""
import base64
import mimetypes
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from app.auth.routes import get_current_user
from app.internal.security import verify_agent_secret
from app.models import User
from app.security import decrypt_secret
from app.workspace.catalog import overview
from app.workspace.service import WorkspaceError, WorkspaceService

router = APIRouter(prefix="/internal/v1", tags=["Workspace"], dependencies=[Depends(verify_agent_secret)])
portal_router = APIRouter(tags=["Workspace downloads"])


class WorkspaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service: str
    method: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    body: dict[str, Any] = Field(default_factory=dict)
    media: dict[str, Any] | None = None
    mail: dict[str, Any] | None = None


def service_for(request, user_id):
    from app.internal.routes import _get_google_client, _get_odoo_service, _verify_odoo_account
    settings = request.app.state.settings
    with request.app.state.session_factory() as db:
        user = db.query(User).filter_by(id=user_id, active=True).first()
        if not user:
            raise HTTPException(404, detail="User not found or inactive")
        secret = ""
        if user.credential and settings.ENCRYPTION_KEY:
            try:
                secret = decrypt_secret(user.credential.encrypted_secret, settings.ENCRYPTION_KEY)
            except Exception:
                secret = ""
        _verify_odoo_account(_get_odoo_service(request, user), user.odoo_uid, secret)
    return WorkspaceService(_get_google_client(request), settings.ENCRYPTION_KEY)


@router.get("/workspace/catalog")
def workspace_catalog(service: str | None = None, method: str | None = None):
    try:
        return {"status": "OK", "catalog": overview(service, method)}
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc


def run_request(user_id, payload, request, kind):
    instance = service_for(request, user_id)
    values = payload.model_dump(exclude={"service"}, exclude_none=True)
    try:
        if kind == "read":
            result = instance.read(user_id, payload.service, values)
            data = result.get("data")
            if isinstance(data, dict) and "content_base64" in data:
                ticket = instance.download_ticket(user_id, payload.service, values)
                result["data"] = {k: v for k, v in data.items() if k != "content_base64"}
                result["data"]["download_url"] = request.app.state.settings.PUBLIC_BASE_URL.rstrip("/") + "/workspace/download/" + ticket
            return result
        return instance.prepare(user_id, payload.service, values)
    except WorkspaceError as exc:
        return {"status": exc.code, "message": str(exc)}
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc


@router.post("/users/{user_id}/workspace/query")
def workspace_query(user_id: int, payload: WorkspaceRequest, request: Request):
    return run_request(user_id, payload, request, "read")


@router.post("/users/{user_id}/workspace/preview")
def workspace_preview(user_id: int, payload: WorkspaceRequest, request: Request):
    return run_request(user_id, payload, request, "write")


@portal_router.get("/workspace/download/{ticket}")
def download_workspace_file(ticket: str, request: Request, user=Depends(get_current_user)):
    try:
        data = service_for(request, user.id).download(user.id, ticket)["data"]
        if not isinstance(data, dict) or "content_base64" not in data:
            raise HTTPException(400, detail="This request is not a binary download")
        mime = data.get("mime_type", "application/octet-stream").split(";")[0]
        extension = mimetypes.guess_extension(mime) or ".bin"
        return Response(base64.b64decode(data["content_base64"]), media_type=mime, headers={"Content-Disposition": f'attachment; filename="workspace{extension}"', "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
    except WorkspaceError as exc:
        raise HTTPException(403, detail=str(exc)) from exc
