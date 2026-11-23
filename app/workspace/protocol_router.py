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
        user_id: Optional[int | tuple[int, int]] = None,
    ) -> ProtocolChoice:
        """Select protocol based on service capability, media, mapping, and circuit breaker."""
        if not self.mcp_enabled:
            return ProtocolChoice.REST

        if media is not None:
            return ProtocolChoice.REST

        # Check circuit breaker per (user_id, service) if user_id is provided, else per service
        if self.circuit_breaker is not None:
            if hasattr(self.circuit_breaker, "_key") and user_id is not None:
                if not self.circuit_breaker.is_available(user_id, service):
                    return ProtocolChoice.REST
            elif hasattr(self.circuit_breaker, "is_available"):
                try:
                    if not self.circuit_breaker.is_available(service):
                        return ProtocolChoice.REST
                except TypeError:
                    pass

        # Check if operation is supported by MCP via google_mcp.translate
        mcp_target = translate(service, method, params or {}, body or {}, media)
        if mcp_target is None:
            return ProtocolChoice.REST

        return ProtocolChoice.MCP

