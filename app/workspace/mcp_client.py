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
        self._http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=5.0),
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )

    async def initialize(self, token: str, user_id: int, connection_version: int) -> Dict[str, Any]:
        """Perform MCP lifecycle initialize handshake."""
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-User-ID": str(user_id),
            "X-Connection-Version": str(connection_version),
        }
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
        
        data = resp.json()
        if "error" in data:
            raise MCPError(f"MCP Initialize Error: {data['error']}", data=data["error"])
        
        result = data.get("result", {})
        return result.get("serverInfo", result)

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

