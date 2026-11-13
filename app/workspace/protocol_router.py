"""Protocol router deciding between Model Context Protocol (MCP) and REST API."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Optional

from app.workspace.google_mcp import translate
from app.workspace.mcp_client import CircuitBreaker


class ProtocolChoice(str, Enum):
    MCP = "mcp"
    REST = "rest"


class ProtocolRouter:
    """Routes workspace operations dynamically to MCP or REST."""

    def __init__(
        self,
        circuit_breaker: Optional[CircuitBreaker] = None,
        mcp_enabled: bool = True,
    ):
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self.mcp_enabled = mcp_enabled

    def select_protocol(
        self,
        service: str,
        method: str,
        params: Dict[str, Any],
        body: Dict[str, Any],
        media: Any = None,
    ) -> ProtocolChoice:
        """Select protocol based on service capability, media, mapping, and circuit breaker."""
        if not self.mcp_enabled:
            return ProtocolChoice.REST

        if media is not None:
            return ProtocolChoice.REST

        if not self.circuit_breaker.is_available(service):
            return ProtocolChoice.REST

        # Check if operation is supported by MCP via google_mcp.translate
        mcp_target = translate(service, method, params or {}, body or {}, media)
        if mcp_target is None:
            return ProtocolChoice.REST

        return ProtocolChoice.MCP

