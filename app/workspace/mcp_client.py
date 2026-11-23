"""Streamable HTTP Client for Model Context Protocol (MCP) and Circuit Breaker."""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional
import httpx

ENDPOINTS: Dict[str, str] = {
    service: f"https://{service}mcp.googleapis.com/mcp/v1"
    for service in ("gmail", "calendar", "drive", "docs", "sheets", "slides")
}


class MCPError(Exception):
    """Raised when an MCP request fails or returns an error response."""
    def __init__(self, message: str = "MCP request failed", status_code: Optional[int] = None, data: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.data = data


class CircuitBreaker:
    """Circuit breaker per service to prevent cascading failures."""
    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 30.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._failures: Dict[str, int] = {}
        self._tripped_until: Dict[str, float] = {}

    def record_failure(self, service: str) -> None:
        self._failures[service] = self._failures.get(service, 0) + 1
        if self._failures[service] >= self.failure_threshold:
            self._tripped_until[service] = time.time() + self.recovery_timeout

    def record_success(self, service: str) -> None:
        self._failures[service] = 0
        self._tripped_until.pop(service, None)

    def is_available(self, service: str) -> bool:
        tripped_time = self._tripped_until.get(service)
        if tripped_time is None:
            return True
        now = time.time()
        if now >= tripped_time:
            # Half-open: allow probe
            return True
        return False


class MCPStreamableClient:
    """Streamable HTTP client for Google Workspace MCP services."""
    def __init__(self, service: str, base_url: Optional[str] = None):
        self.service = service
        self.base_url = base_url or ENDPOINTS.get(service, f"https://{service}mcp.googleapis.com/mcp/v1")
        self.session_id: Optional[str] = None
        self._http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=5.0),
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )

    async def initialize(self, token: str, user_id: int, connection_version: int) -> Dict[str, Any]:
        """Perform MCP lifecycle initialize handshake and capture Mcp-Session-Id."""
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-User-ID": str(user_id),
            "X-Connection-Version": str(connection_version),
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {
                    "tools": {"listChanged": True},
                },
                "clientInfo": {
                    "name": "odoo-auth-gateway",
                    "version": "1.0",
                },
            },
        }
        resp = await self._http_client.send(
            httpx.Request("POST", self.base_url, headers=headers, json=payload)
        )
        if resp.status_code >= 400:
            raise MCPError(f"Initialize failed with status {resp.status_code}", status_code=resp.status_code)
        
        # Save Mcp-Session-Id if returned by server
        sess_hdr = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
        if sess_hdr:
            self.session_id = sess_hdr

        data = resp.json()
        if "error" in data:
            raise MCPError(f"MCP Initialize Error: {data['error']}", data=data["error"])
        
        result = data.get("result", {})
        return result.get("serverInfo", result)

    async def list_tools(
        self,
        token: str,
        user_id: int,
        connection_version: int,
    ) -> list[Dict[str, Any]]:
        """List available tools exposed by this MCP server."""
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-User-ID": str(user_id),
            "X-Connection-Version": str(connection_version),
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        payload = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        }
        resp = await self._http_client.send(
            httpx.Request("POST", self.base_url, headers=headers, json=payload)
        )
        if resp.status_code >= 400:
            raise MCPError(f"tools/list failed with status {resp.status_code}", status_code=resp.status_code)
        
        data = resp.json()
        if "error" in data:
            raise MCPError(f"MCP tools/list Error: {data['error']}", data=data["error"])
        
        result = data.get("result", {})
        return result.get("tools", [])

    async def call_tool(
        self,
        token: str,
        user_id: int,
        connection_version: int,
        name: str,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Call an MCP tool with JSON-RPC 2.0."""
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-User-ID": str(user_id),
            "X-Connection-Version": str(connection_version),
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        payload = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": arguments,
            },
        }
        resp = await self._http_client.send(
            httpx.Request("POST", self.base_url, headers=headers, json=payload)
        )
        if resp.status_code >= 400:
            raise MCPError(f"Tool call failed with status {resp.status_code}", status_code=resp.status_code)

        content_type = resp.headers.get("Content-Type", "")
        if "text/event-stream" in content_type:
            # Parse SSE lines
            lines = resp.text.splitlines()
            result_data = None
            for line in lines:
                if line.startswith("data: "):
                    data_str = line[6:].strip()
                    if data_str:
                        try:
                            msg = json.loads(data_str)
                            if "result" in msg:
                                result_data = msg["result"]
                        except Exception:
                            continue
            if result_data is None:
                raise MCPError("No result received in SSE stream")
            if result_data.get("isError"):
                raise MCPError("Tool returned error status in SSE stream", data=result_data)
            return result_data.get("structuredContent", result_data)
        else:
            data = resp.json()
            if "error" in data:
                raise MCPError(f"MCP Error: {data['error']}", data=data["error"])
            result = data.get("result", {})
            if result.get("isError"):
                raise MCPError("Tool returned error status", data=result)
            return result.get("structuredContent", result)

    async def close(self) -> None:
        await self._http_client.aclose()


class MCPSyncClient:
    """Synchronous Streamable HTTP client for Google Workspace MCP services with session persistence."""

    def __init__(
        self,
        service: str,
        base_url: Optional[str] = None,
        http_client: Optional[httpx.Client] = None,
        transport: Optional[Any] = None,
    ):
        self.service = service
        self.base_url = base_url or ENDPOINTS.get(service, f"https://{service}mcp.googleapis.com/mcp/v1")
        self.session_id: Optional[str] = None
        self._shared_client = http_client or httpx.Client(
            http2=True,
            timeout=httpx.Timeout(9.0, connect=3.0, pool=2.0),
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
            transport=transport,
        )
        self._owns_client = http_client is None
        from threading import Lock

        self._initialize_lock = Lock()
        self.protocol_version = None
        self._tools = None

    def _headers(self, token):
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    @staticmethod
    def _timeout():
        from app.workspace.network_slots import request_deadline

        deadline = request_deadline.get()
        remaining = min(9.0, deadline - time.monotonic()) if deadline is not None else 9.0
        if remaining <= 0:
            raise MCPError("MCP request deadline expired")
        return httpx.Timeout(remaining, connect=min(3, remaining), pool=min(2, remaining))

    def _rpc(self, token, payload):
        with self._shared_client.stream("POST", self.base_url, headers=self._headers(token), json=payload,
                                        timeout=self._timeout()) as response:
            if response.status_code >= 400:
                raise MCPError("MCP lifecycle request rejected", status_code=response.status_code)
            if "id" not in payload:
                return {}
            session = response.headers.get("Mcp-Session-Id")
            if session:
                self.session_id = session
            if "text/event-stream" in response.headers.get("Content-Type", ""):
                data = self._parse_sse_response(response.iter_text())
            else:
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 8 * 1024 * 1024:
                        raise MCPError("Response exceeds 8 MiB")
                data = json.loads(raw)
            if data.get("id") != payload["id"] or "error" in data:
                error = data.get("error") or {}
                code = error.get("code") if isinstance(error, dict) else None
                raise MCPError("Invalid MCP lifecycle response", status_code=code if code in {401, 403, 429} else None)
            return data.get("result", {})

    def _ensure_ready(self, token):
        with self._initialize_lock:
            if self._tools is not None:
                return
            try:
                result = self._rpc(token, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "odoo-auth-gateway", "version": "1.0"},
                }})
                version = result.get("protocolVersion")
                if version not in {"2025-11-25", "2025-06-18", "2025-03-26"}:
                    raise MCPError("Unsupported MCP protocol version")
                self.protocol_version = version
                self._rpc(token, {"jsonrpc": "2.0", "method": "notifications/initialized"})
                tools = {}
                params = {}
                for index in range(20):
                    page = self._rpc(token, {"jsonrpc": "2.0", "id": 2 + index, "method": "tools/list", "params": params})
                    for tool in page.get("tools", []):
                        if isinstance(tool.get("inputSchema"), dict):
                            tools[tool["name"]] = tool["inputSchema"]
                    cursor = page.get("nextCursor")
                    if not cursor:
                        self._tools = tools
                        return
                    params = {"cursor": cursor}
                raise MCPError("MCP tool listing exceeded page limit")
            except Exception:
                self.session_id = None
                self.protocol_version = None
                raise

    def initialize(self, token: str, user_id: int, connection_version: int = 1) -> Dict[str, Any]:
        """Perform MCP initialize handshake and capture Mcp-Session-Id."""
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-User-ID": str(user_id),
            "X-Connection-Version": str(connection_version),
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {"listChanged": True}},
                "clientInfo": {"name": "odoo-auth-gateway", "version": "1.0"},
            },
        }
        resp = self._shared_client.post(self.base_url, headers=headers, json=payload)
        if resp.status_code >= 400:
            raise MCPError(f"Initialize failed with status {resp.status_code}", status_code=resp.status_code)
        sess_hdr = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
        if sess_hdr:
            self.session_id = sess_hdr
        data = resp.json()
        if "error" in data:
            raise MCPError(f"MCP Initialize Error: {data['error']}", data=data["error"])
        result = data.get("result", {})
        return result.get("serverInfo", result)

    def list_tools(self, token: str, user_id: int, connection_version: int = 1) -> list[Dict[str, Any]]:
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-User-ID": str(user_id),
            "X-Connection-Version": str(connection_version),
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        payload = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        resp = self._shared_client.post(self.base_url, headers=headers, json=payload)
        if resp.status_code >= 400:
            raise MCPError(f"tools/list failed with status {resp.status_code}", status_code=resp.status_code)
        data = resp.json()
        if "error" in data:
            raise MCPError(f"MCP tools/list Error: {data['error']}", data=data["error"])
        result = data.get("result", {})
        return result.get("tools", [])

    def call_tool(
        self,
        token: str,
        name: str,
        arguments: Dict[str, Any],
        user_id: Optional[int] = None,
        connection_version: int = 1,
    ) -> Dict[str, Any]:
        from jsonschema import ValidationError, validate

        self._ensure_ready(token)
        if name not in self._tools:
            raise MCPError("MCP tool unavailable", status_code=501)
        try:
            validate(arguments, self._tools[name])
        except ValidationError as exc:
            raise MCPError("Invalid MCP arguments", status_code=400) from exc
        headers = self._headers(token)
        if user_id is not None:
            headers["X-User-ID"] = str(user_id)
            headers["X-Connection-Version"] = str(connection_version)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        try:
            with self._shared_client.stream("POST", self.base_url, headers=headers, json=payload,
                                            timeout=self._timeout()) as response:
                if response.status_code != 200:
                    if response.status_code == 404:
                        with self._initialize_lock:
                            self.session_id = None
                            self.protocol_version = None
                            self._tools = None
                    raise MCPError(f"Tool call failed with status {response.status_code}", status_code=response.status_code)

                # Capture or refresh session id
                sess_hdr = response.headers.get("Mcp-Session-Id") or response.headers.get("mcp-session-id")
                if sess_hdr:
                    self.session_id = sess_hdr

                content_type = response.headers.get("content-type", "")
                if "text/event-stream" in content_type:
                    message = self._parse_sse_response(response.iter_text())
                else:
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 8 * 1024 * 1024:
                            raise MCPError("Response exceeds 8 MiB")
                    message = json.loads(raw)

            if message.get("id") != 1 or "error" in message:
                err_data = message.get("error")
                err_code = err_data.get("code") if isinstance(err_data, dict) else None
                status_code = err_code if err_code in {401, 403, 429} else None
                raise MCPError("MCP call returned error", status_code=status_code, data=err_data)
            result = message.get("result", {})
            if result.get("isError"):
                err_code = result.get("code") or result.get("status_code")
                status_code = err_code if err_code in {401, 403, 429} else 400
                raise MCPError("Tool returned error status", status_code=status_code, data=result)

            data = result.get("structuredContent")
            if data is None:
                blocks = result.get("content", [])
                if len(blocks) == 1 and blocks[0].get("type") == "text":
                    data = json.loads(blocks[0]["text"])
                else:
                    data = result

            if not isinstance(data, dict):
                return data

            # Format tool response matching catalog contracts
            if name in {"read_doc", "read_presentation"}:
                return data.get("content", data)
            if name == "get_file_metadata":
                return self._drive_file(data)
            if name == "search_files":
                return {**data, "files": [self._drive_file(item) for item in data.get("files", [])]}
            if name == "list_events":
                return {**data, "items": data.get("events", [])}
            return data
        except (httpx.RequestError, ValueError, KeyError, TypeError) as exc:
            if isinstance(exc, MCPError):
                raise
            raise MCPError("MCP request failed") from exc

    def _parse_sse_response(self, chunks):
        buffer = ""
        received = 0
        for chunk in chunks:
            received += len(chunk.encode())
            if received > 8 * 1024 * 1024:
                raise MCPError("Response exceeds 8 MiB")
            events = (buffer + chunk).replace("\r\n", "\n").split("\n\n")
            buffer = events.pop()
            for event in events:
                data = "\n".join(line[5:].lstrip(" ") for line in event.splitlines() if line.startswith("data:"))
                if data:
                    message = json.loads(data)
                    if isinstance(message, dict) and message.get("id") == 1:
                        return message
        raise MCPError("No valid SSE response received")

    @staticmethod
    def _drive_file(data):
        names = {"title": "name", "viewUrl": "webViewLink", "fileSize": "size"}
        return {names.get(key, key): value for key, value in data.items()}

    def close(self) -> None:
        if self._owns_client and not self._shared_client.is_closed:
            self._shared_client.close()

