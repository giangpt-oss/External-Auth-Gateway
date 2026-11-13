import ssl
import xmlrpc.client
from typing import Any
from urllib.parse import urlparse

import httpx
from app.workspace.network_slots import outbound_slot, outbound_user_id


class OdooError(Exception):
    """Base exception for Odoo errors."""

    pass


class OdooAuthError(OdooError):
    """Raised when authentication fails."""

    pass


class OdooAccessError(OdooError):
    """Raised when access is denied / permission error."""

    pass


class OdooNetworkError(OdooError):
    """Raised on connection timeout or network failure."""

    pass


class OdooAccountInactiveError(OdooAccessError):
    """Raised when Odoo account is inactive (active=False)."""

    pass


class OdooExternalUserError(OdooAccessError):
    """Raised when Odoo account is an external/portal user (share=True)."""

    pass


class TimeoutTransport(xmlrpc.client.SafeTransport):
    """XML-RPC transport with configurable timeout and TLS context."""

    def __init__(self, timeout: int = 20, context: ssl.SSLContext | None = None):
        super().__init__(context=context)
        self.timeout = timeout

    def make_connection(self, host: Any):
        conn = super().make_connection(host)
        conn.timeout = self.timeout
        return conn


class OdooClient:
    def __init__(self, url: str, db: str, timeout: int = 20):
        parsed = urlparse(url)
        if parsed.scheme and parsed.netloc:
            self.url = f"{parsed.scheme}://{parsed.netloc}"
        else:
            self.url = url.rstrip("/")
        self.db = db
        self.timeout = timeout

    def studio_approval_spec(self, uid, secret, record_id, method, company_id):
        """Studio returns integer map keys, unsupported by XML-RPC serialization."""
        if method not in ('action_confirm', 'action_quotation_send'):
            raise OdooError('Unsupported quotation approval method.')
        try:
            response = httpx.post(self.url + '/jsonrpc', json={
                'jsonrpc': '2.0', 'method': 'call', 'id': 1,
                'params': {'service': 'object', 'method': 'execute_kw', 'args': [
                    self.db, uid, secret, 'studio.approval.rule', 'get_approval_spec',
                    [[{'model': 'sale.order', 'method': method, 'action_id': False, 'res_id': record_id}]],
                    {'context': {'allowed_company_ids': [company_id], 'force_company': company_id}}]}},
                timeout=self.timeout)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OdooNetworkError('Không đọc được trạng thái phê duyệt từ Odoo.') from exc
        if body.get('error'):
            name = body['error'].get('data', {}).get('name', '')
            if name.endswith(('AccessError', 'AccessDenied')):
                raise OdooAccessError('Không có quyền đọc quy tắc phê duyệt Odoo.')
            raise OdooError('Odoo không cung cấp cấu trúc phê duyệt Studio được hỗ trợ.')
        if not isinstance(body.get('result'), dict):
            raise OdooError('Dữ liệu phê duyệt Studio không hợp lệ.')
        return body['result']

    def _get_proxy(self, service: str) -> xmlrpc.client.ServerProxy:
        endpoint = f"{self.url}/xmlrpc/2/{service}"
        if endpoint.startswith("https://"):
            context = ssl.create_default_context()
            transport = TimeoutTransport(timeout=self.timeout, context=context)
            return xmlrpc.client.ServerProxy(endpoint, transport=transport, allow_none=True)
        else:
            transport = xmlrpc.client.Transport()
            # Set default socket timeout for http
            return xmlrpc.client.ServerProxy(endpoint, transport=transport, allow_none=True)

    def authenticate(self, login: str, secret: str) -> int:
        try:
            common = self._get_proxy("common")
            uid = common.authenticate(self.db, login, secret, {})
            if not uid or not isinstance(uid, int):
                raise OdooAuthError("Invalid Odoo credentials or user not found.")
            return uid
        except (TimeoutError, ConnectionError, OSError) as e:
            raise OdooNetworkError(f"Failed to connect to Odoo server: {e}") from e
        except xmlrpc.client.Fault as e:
            if "AccessDenied" in e.faultString:
                raise OdooAuthError(f"Access Denied: {e.faultString}") from e
            raise OdooError(f"Odoo RPC Fault: {e.faultString}") from e

    def authenticate_password(self, login: str, password: str) -> int:
        """Use Odoo's interactive login, which validates the account password."""
        try:
            response = httpx.post(
                f"{self.url}/web/session/authenticate",
                json={
                    "jsonrpc": "2.0",
                    "method": "call",
                    "params": {"db": self.db, "login": login, "password": password},
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OdooNetworkError("Failed to connect to Odoo web login.") from exc

        if not isinstance(body, dict):
            raise OdooNetworkError("Invalid response from Odoo web login.")
        result = body.get("result")
        uid = result.get("uid") if isinstance(result, dict) else None
        if "error" in body or type(uid) is not int or uid <= 0:
            raise OdooAuthError("Odoo did not complete password authentication.")
        return uid

    def execute(
        self,
        uid: int,
        secret: str,
        model: str,
        method: str,
        args: list[Any],
        kwargs: dict[str, Any] | None = None,
    ) -> Any:
        kwargs = kwargs or {}
        try:
            models = self._get_proxy("object")
            read_methods = {"read", "search", "search_read", "search_count", "fields_get",
                            "read_group", "name_search", "name_get", "check_access_rights",
                            "check_access_rule", "default_get", "has_group"}
            with outbound_slot(outbound_user_id.get(), write=method not in read_methods):
                return models.execute_kw(self.db, uid, secret, model, method, args, kwargs)
        except (TimeoutError, ConnectionError, OSError) as e:
            raise OdooNetworkError(f"Network error during Odoo call: {e}") from e
        except xmlrpc.client.Fault as e:
            fault_str = f"{getattr(e, 'faultCode', '')} {getattr(e, 'faultString', '')}"
            if "AccessError" in fault_str or "AccessDenied" in fault_str:
                raise OdooAccessError(f"Odoo Access Denied: {fault_str}") from e
            raise OdooError(f"Odoo RPC Fault: {fault_str}") from e
