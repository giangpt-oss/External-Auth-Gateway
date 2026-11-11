from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException

from app.models import User
from app.reporting.schemas import TelegramScope
from app.security import decrypt_secret


@dataclass
class VerifiedContext:
    user: Any
    service: Any
    secret: str
    company_ids: list[int]
    default_company_id: int
    metadata: dict = field(default_factory=dict)
    active_company_ids: list[int] | None = None
    session_factory: Any = None

    def call(self, model, method, args=None, kwargs=None):
        kwargs = dict(kwargs or {})
        companies = self.active_company_ids or [self.default_company_id]
        rpc_context = dict(kwargs.get("context") or {})
        rpc_context["allowed_company_ids"] = companies
        if len(companies) == 1:
            rpc_context["force_company"] = companies[0]
        else:
            rpc_context.pop("force_company", None)
        kwargs["context"] = rpc_context
        return self.service.client.execute(
            uid=self.user.odoo_uid, secret=self.secret, model=model, method=method,
            args=args or [], kwargs=kwargs or {},
        )

    def read_fields(self, model):
        if model not in self.metadata:
            self.metadata[model] = self.call(model, "fields_get", [], {
                "attributes": ["string", "type", "required", "readonly", "selection", "relation"],
            })
        return self.metadata[model]

    def selected_companies(self, requested):
        chosen = requested or [self.default_company_id]
        if not set(chosen).issubset(self.company_ids):
            raise HTTPException(403, "Công ty không thuộc phạm vi tài khoản Odoo.")
        self.active_company_ids = chosen
        return chosen


@contextmanager
def authenticated_context(request, user_id: int, binding: TelegramScope):
    # Import lazily to avoid a cycle when the internal router includes report routes.
    from app.internal.routes import _get_odoo_service, _verify_odoo_account

    settings = request.app.state.settings
    with request.app.state.session_factory() as db:
        user = db.query(User).filter_by(id=user_id, active=True).first()
        if not user:
            raise HTTPException(404, "Không tìm thấy tài khoản đang hoạt động.")
        conn = user.telegram_connection
        if not conn or conn.status != "connected" or (
            conn.telegram_user_id != binding.telegram_user_id
            or conn.private_chat_id != binding.chat_id
            or conn.binding_id != binding.telegram_binding_id
        ):
            raise HTTPException(403, "RECONNECT_REQUIRED: Liên kết Telegram đã thay đổi.")
        if not user.credential or not settings.ENCRYPTION_KEY:
            raise HTTPException(403, "Thiếu thông tin xác thực Odoo.")
        try:
            secret = decrypt_secret(user.credential.encrypted_secret, settings.ENCRYPTION_KEY)
        except Exception as exc:
            raise HTTPException(403, "Không thể giải mã thông tin xác thực Odoo.") from exc
        svc = _get_odoo_service(request, user)
        _verify_odoo_account(svc, user.odoo_uid, secret)
        profiles = svc.client.execute(user.odoo_uid, secret, "res.users", "read", [[user.odoo_uid]], {
            "fields": ["company_ids", "company_id"],
        })
        if not profiles or not profiles[0].get("company_id"):
            raise HTTPException(403, "Không xác định được công ty mặc định.")
        profile = profiles[0]
        yield VerifiedContext(user, svc, secret, profile.get("company_ids", []), profile["company_id"][0],
                              session_factory=request.app.state.session_factory)
