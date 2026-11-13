import pytest

from app.workspace.adapters import (
    normalize_workspace_result,
    encode_cursor,
    decode_cursor,
    ProtocolCursorMismatchError,
)


def test_cursor_encode_decode():
    cursor = encode_cursor(protocol="rest", page_token="token_abc_123")
    assert isinstance(cursor, str)
    
    proto, token = decode_cursor(cursor)
    assert proto == "rest"
    assert token == "token_abc_123"


def test_cursor_protocol_mismatch_detection():
    # If a cursor is created with MCP, decoding it with expected protocol 'rest' raises mismatch error
    cursor = encode_cursor(protocol="mcp", page_token="mcp_cursor_xyz")
    
    with pytest.raises(ProtocolCursorMismatchError):
        decode_cursor(cursor, expected_protocol="rest")


def test_normalize_gmail_messages_rest_and_mcp():
    raw_rest = {
        "messages": [
            {
                "id": "msg_001",
                "threadId": "th_001",
                "snippet": "Xin chào...",
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Báo cáo tiến độ"},
                        {"name": "From", "value": "boss@company.com"},
                        {"name": "To", "value": "me@company.com"},
                        {"name": "Date", "value": "Fri, 13 Nov 2026 09:00:00 +0700"},
                    ]
                },
            }
        ],
        "nextPageToken": "page_tok_1",
    }
    
    normalized_rest = normalize_workspace_result(
        protocol="rest",
        service="gmail",
        operation="messages.list",
        raw_data=raw_rest,
    )
    
    assert len(normalized_rest["items"]) == 1
    item = normalized_rest["items"][0]
    assert item["id"] == "msg_001"
    assert item["thread_id"] == "th_001"
    assert item["subject"] == "Báo cáo tiến độ"
    assert item["from"] == "boss@company.com"
    assert item["to"] == "me@company.com"
    assert normalized_rest["next_cursor"] is not None
    proto, tok = decode_cursor(normalized_rest["next_cursor"])
    assert proto == "rest"
    assert tok == "page_tok_1"


def test_normalize_calendar_events_rest_and_mcp():
    raw_rest = {
        "items": [
            {
                "id": "evt_123",
                "summary": "Họp chiến lược Q4",
                "start": {"dateTime": "2026-11-14T09:00:00+07:00"},
                "end": {"dateTime": "2026-11-14T10:00:00+07:00"},
                "htmlLink": "https://calendar.google.com/event/123",
            }
        ],
        "nextPageToken": "cal_page_2",
    }
    
    normalized = normalize_workspace_result(
        protocol="rest",
        service="calendar",
        operation="events.list",
        raw_data=raw_rest,
    )
    
    assert len(normalized["items"]) == 1
    evt = normalized["items"][0]
    assert evt["id"] == "evt_123"
    assert evt["summary"] == "Họp chiến lược Q4"
    assert evt["start"]["dateTime"] == "2026-11-14T09:00:00+07:00"
    assert evt["html_link"] == "https://calendar.google.com/event/123"


def test_normalize_drive_files_rest_and_mcp():
    raw_rest = {
        "files": [
            {
                "id": "file_999",
                "name": "Bảng dự toán.xlsx",
                "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "size": "1048576",
                "modifiedTime": "2026-11-13T10:00:00Z",
            }
        ],
        "nextPageToken": "drive_page_next",
    }
    
    normalized = normalize_workspace_result(
        protocol="rest",
        service="drive",
        operation="files.list",
        raw_data=raw_rest,
    )
    
    assert len(normalized["items"]) == 1
    f = normalized["items"][0]
    assert f["id"] == "file_999"
    assert f["name"] == "Bảng dự toán.xlsx"
    assert f["mime_type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert f["size_bytes"] == 1048576

