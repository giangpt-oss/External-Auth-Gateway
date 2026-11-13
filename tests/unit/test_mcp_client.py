import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx

from app.workspace.mcp_client import (
    MCPStreamableClient,
    MCPError,
    CircuitBreaker,
)


@pytest.mark.asyncio
async def test_mcp_initialize_and_tool_call():
    client = MCPStreamableClient(service="drive")
    
    mock_init_resp = httpx.Response(
        status_code=200,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "google-drive-mcp", "version": "1.0"},
            },
        },
        request=httpx.Request("POST", "https://drivemcp.googleapis.com/mcp/v1"),
    )
    
    mock_call_resp = httpx.Response(
        status_code=200,
        headers={"Content-Type": "application/json"},
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "structuredContent": {"files": [{"id": "f1", "name": "Document.docx"}]},
                "isError": False,
            },
        },
        request=httpx.Request("POST", "https://drivemcp.googleapis.com/mcp/v1"),
    )
    
    with patch.object(client._http_client, "send", new_callable=AsyncMock) as mock_send:
        mock_send.side_effect = [mock_init_resp, mock_call_resp]
        
        # 1. Initialize
        server_info = await client.initialize(token="test_token", user_id=1, connection_version=1)
        assert server_info["name"] == "google-drive-mcp"
        
        # 2. Call tool
        res = await client.call_tool(
            token="test_token",
            user_id=1,
            connection_version=1,
            name="search_files",
            arguments={"pageSize": 10},
        )
        assert "files" in res
        assert res["files"][0]["name"] == "Document.docx"
    await client.close()


@pytest.mark.asyncio
async def test_mcp_circuit_breaker_tripping():
    cb = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)
    
    assert cb.is_available("gmail") is True
    
    # Record 4 failures -> still available
    for _ in range(4):
        cb.record_failure("gmail")
    assert cb.is_available("gmail") is True
    
    # 5th failure -> trips circuit breaker
    cb.record_failure("gmail")
    assert cb.is_available("gmail") is False
    
    # Other service is isolated and remains available
    assert cb.is_available("calendar") is True
    
    # Success resets failure count
    cb.record_success("calendar")
    assert cb.is_available("calendar") is True

