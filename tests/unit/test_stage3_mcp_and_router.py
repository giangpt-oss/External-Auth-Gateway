"""Tests for Stage 3 Phase 3: MCP Client Session Lifecycle, tools/list, and Protocol Routing."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import json
import httpx

from app.workspace.mcp_client import MCPStreamableClient, MCPError
from app.workspace.protocol_router import ProtocolRouter, ProtocolChoice
from app.workspace.circuit_breaker import AccountServiceCircuitBreaker
from app.workspace.adapters import encode_cursor, decode_cursor, ProtocolCursorMismatchError


# ============================================================================
# 1. MCP Client Session Lifecycle & tools/list
# ============================================================================

@pytest.mark.asyncio
async def test_mcp_client_tools_list_lifecycle():
    """MCPStreamableClient must support tools/list method adhering to JSON-RPC 2.0,
    passing user_id and connection_version in session headers.
    """
    client = MCPStreamableClient("calendar")
    
    mock_tools_resp = httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "tools": [
                    {
                        "name": "list_events",
                        "description": "List calendar events",
                        "inputSchema": {"type": "object", "properties": {"calendarId": {"type": "string"}}},
                    }
                ]
            },
        },
        request=httpx.Request("POST", "https://calendarmcp.googleapis.com/mcp/v1"),
    )
    
    with patch.object(client._http_client, "send", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = mock_tools_resp
        
        tools = await client.list_tools(token="test_tok", user_id=1, connection_version=2)
        assert len(tools) == 1
        assert tools[0]["name"] == "list_events"
        
        # Verify request payload and session headers
        sent_req = mock_send.call_args[0][0]
        assert sent_req.headers["X-User-ID"] == "1"
        assert sent_req.headers["X-Connection-Version"] == "2"
        assert sent_req.headers["Authorization"] == "Bearer test_tok"
        
    await client.close()


@pytest.mark.asyncio
async def test_mcp_client_sse_streaming_response():
    """Verify MCPStreamableClient properly parses SSE (text/event-stream) messages."""
    client = MCPStreamableClient("docs")
    
    sse_body = "event: message\ndata: {\"jsonrpc\": \"2.0\", \"id\": 2, \"result\": {\"structuredContent\": {\"title\": \"Report\"}}}\n\n"
    mock_resp = httpx.Response(
        200,
        headers={"Content-Type": "text/event-stream"},
        text=sse_body,
        request=httpx.Request("POST", "https://docsmcp.googleapis.com/mcp/v1"),
    )
    
    with patch.object(client._http_client, "send", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = mock_resp
        
        res = await client.call_tool(
            token="tok",
            user_id=1,
            connection_version=1,
            name="read_doc",
            arguments={"documentId": "doc123"},
        )
        assert res == {"title": "Report"}
        
    await client.close()


# ============================================================================
# 2. Protocol Router Fallback & Cursor Integrity (No Cross-Protocol Leaks)
# ============================================================================

def test_protocol_router_falls_back_to_rest_when_mcp_unsupported():
    """Operations without verified MCP schema mapping or operations with media
    must route to REST fallback.
    """
    router = ProtocolRouter(mcp_enabled=True)
    
    # 1. Supported read -> MCP
    choice = router.select_protocol("calendar", "events.list", {"calendarId": "primary"}, {})
    assert choice == ProtocolChoice.MCP
    
    # 2. Media present -> REST
    choice_media = router.select_protocol("drive", "files.create", {}, {"name": "test.pdf"}, media={"bytes": "..."})
    assert choice_media == ProtocolChoice.REST
    
    # 3. Unmapped operation -> REST
    choice_unmapped = router.select_protocol("gmail", "unknown.custom.op", {}, {})
    assert choice_unmapped == ProtocolChoice.REST


def test_protocol_router_falls_back_when_circuit_breaker_tripped():
    """If circuit breaker is tripped for this (user_id, service), fallback to REST."""
    cb = AccountServiceCircuitBreaker(failure_threshold=1)
    cb.record_failure(user_id=1, service="calendar")
    
    router = ProtocolRouter(circuit_breaker=cb, mcp_enabled=True)
    choice = router.select_protocol("calendar", "events.list", {"calendarId": "primary"}, {}, user_id=1)
    assert choice == ProtocolChoice.REST
    
    # Other user still gets MCP if available
    choice_other = router.select_protocol("calendar", "events.list", {"calendarId": "primary"}, {}, user_id=2)
    assert choice_other == ProtocolChoice.MCP


def test_cursor_protocol_integrity_cannot_mix():
    """Pagination cursors must encapsulate protocol and prevent mixing MCP with REST tokens."""
    mcp_cursor = encode_cursor("mcp", "page_token_abc")
    assert isinstance(mcp_cursor, str)
    
    # Valid decode
    proto, token = decode_cursor(mcp_cursor, expected_protocol="mcp")
    assert proto == "mcp"
    assert token == "page_token_abc"
    
    # Attempting to decode MCP cursor expecting REST must raise ProtocolCursorMismatchError
    with pytest.raises(ProtocolCursorMismatchError):
        decode_cursor(mcp_cursor, expected_protocol="rest")


# ============================================================================
# 3. Circuit Breaker Filtering & MCP Session Persistence Across Requests
# ============================================================================

def test_circuit_breaker_ignores_401_403_429_errors(monkeypatch):
    """Lỗi MCP 401, 403 và 429 không được tính vào circuit breaker để tránh lỗi quyền
    làm các yêu cầu sau bị chuyển nhầm sang REST.
    Chỉ lỗi hạ tầng (5xx, rớt mạng) mới ghi nhận failure và ngắt circuit.
    """
    from app.workspace.service import WorkspaceService, WorkspaceError
    from app.workspace.mcp_client import MCPError
    monkeypatch.setattr("app.workspace.service.time.sleep", lambda _: None)

    cb = AccountServiceCircuitBreaker(failure_threshold=3)
    mock_google = MagicMock()
    mock_conn = MagicMock()
    mock_conn.status = "connected"
    mock_conn.scopes = "https://www.googleapis.com/auth/calendar.readonly"
    mock_conn.connection_version = 1
    mock_google.get_user_google_connection.return_value = mock_conn
    mock_google.oauth_service.get_valid_access_token.return_value = "valid_tok"

    ws = WorkspaceService(
        google_client=mock_google,
        signing_key="default_key_32bytes_min_12345678",
        use_mcp=True,
        circuit_breaker=cb,
        transport=httpx.MockTransport(lambda _: httpx.Response(503)),
    )

    mcp_client = ws.get_mcp_client("calendar", 1, 1)

    # 1. Simulate 403 Permission Denied (e.g. missing granular permission)
    def raise_403(*args, **kwargs):
        raise MCPError("Forbidden: insufficient scope", status_code=403)

    mcp_client.call_tool = raise_403

    # Call 5 times with 403 error
    for _ in range(5):
        with pytest.raises(WorkspaceError) as exc_info:
            ws.read(user_id=1, service="calendar", payload={"method": "events.list", "parameters": {"calendarId": "primary"}})
        assert exc_info.value.code == "PERMISSION_DENIED"

    # Circuit breaker must STILL be available (failure count is 0, not tripped!)
    assert cb.is_available(user_id=1, service="calendar") is True
    assert cb._failures.get((1, "calendar"), 0) == 0

    # 2. Simulate 401 Reconnect Required
    def raise_401(*args, **kwargs):
        raise MCPError("Unauthorized", status_code=401)

    mcp_client.call_tool = raise_401
    for _ in range(5):
        with pytest.raises(WorkspaceError) as exc_info:
            ws.read(user_id=1, service="calendar", payload={"method": "events.list", "parameters": {"calendarId": "primary"}})
        assert exc_info.value.code == "RECONNECT_REQUIRED"

    assert cb.is_available(user_id=1, service="calendar") is True
    assert cb._failures.get((1, "calendar"), 0) == 0

    # 3. Simulate 429 Rate Limit
    def raise_429(*args, **kwargs):
        raise MCPError("Rate limited", status_code=429)

    mcp_client.call_tool = raise_429
    for _ in range(5):
        with pytest.raises(WorkspaceError) as exc_info:
            ws.read(user_id=1, service="calendar", payload={"method": "events.list", "parameters": {"calendarId": "primary"}})
        assert exc_info.value.code == "RATE_LIMIT"

    assert cb.is_available(user_id=1, service="calendar") is True
    assert cb._failures.get((1, "calendar"), 0) == 0

    # 4. Now simulate 500 Infrastructure Server Error
    def raise_500(*args, **kwargs):
        raise MCPError("Internal Server Error", status_code=500)

    mcp_client.call_tool = raise_500
    for _ in range(3):
        with pytest.raises(WorkspaceError):
            ws.read(user_id=1, service="calendar", payload={"method": "events.list", "parameters": {"calendarId": "primary"}})

    # After 3 server errors (failure_threshold=3), circuit breaker MUST TRIP!
    assert cb.is_available(user_id=1, service="calendar") is False


def test_workspace_service_preserves_mcp_session_across_multiple_calls():
    """Verify that WorkspaceService preserves the same MCPSyncClient instance
    and Mcp-Session-Id across multiple operations and calls.
    """
    from app.workspace.service import WorkspaceService

    mock_google = MagicMock()
    mock_conn = MagicMock()
    mock_conn.status = "connected"
    mock_conn.scopes = "https://www.googleapis.com/auth/calendar.readonly https://www.googleapis.com/auth/drive.readonly"
    mock_conn.connection_version = 1
    mock_google.get_user_google_connection.return_value = mock_conn
    mock_google.oauth_service.get_valid_access_token.return_value = "valid_tok"

    # Mock HTTP client responses for MCP
    sessions_seen = []

    def mock_transport_handler(request: httpx.Request):
        sess = request.headers.get("Mcp-Session-Id")
        call = json.loads(request.content)
        if call["method"] == "initialize":
            return httpx.Response(200, headers={"Mcp-Session-Id": "mcp_sess_persisted_999"},
                                  json={"id": call["id"], "result": {"protocolVersion": "2025-11-25"}})
        if call["method"] == "notifications/initialized":
            return httpx.Response(202)
        if call["method"] == "tools/list":
            return httpx.Response(200, json={"id": call["id"], "result": {"tools": [
                {"name": "list_events", "inputSchema": {"type": "object"}},
            ]}})
        sessions_seen.append(sess)
        # Server returns a new or refreshed Mcp-Session-Id header
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json", "Mcp-Session-Id": "mcp_sess_persisted_999"},
            json={"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": {"events": [{"id": "ev1"}]}}},
        )

    transport = httpx.MockTransport(mock_transport_handler)
    mock_http_client = httpx.Client(transport=transport)

    ws = WorkspaceService(
        google_client=mock_google,
        signing_key="default_key_32bytes_min_12345678",
        use_mcp=True,
        http_client=mock_http_client,
    )

    # Call 1: Initially no session id
    res1 = ws.read(user_id=1, service="calendar", payload={"method": "events.list", "parameters": {"calendarId": "primary"}})
    assert res1["data"]["items"] == [{"id": "ev1"}]
    assert sessions_seen[0] == "mcp_sess_persisted_999"

    # Call 2: Second call MUST reuse the session ID returned by call 1!
    res2 = ws.read(user_id=1, service="calendar", payload={"method": "events.list", "parameters": {"calendarId": "primary"}})
    assert res2["data"]["items"] == [{"id": "ev1"}]
    assert sessions_seen[1] == "mcp_sess_persisted_999"

    # Verify the same MCPSyncClient instance is preserved
    client1 = ws.get_mcp_client("calendar", 1, 1)
    client2 = ws.get_mcp_client("calendar", 1, 1)
    assert client1 is client2
    assert client1.session_id == "mcp_sess_persisted_999"
    assert ws.get_mcp_client("calendar", 2, 1).session_id is None
    assert ws.get_mcp_client("calendar", 1, 2).session_id is None
