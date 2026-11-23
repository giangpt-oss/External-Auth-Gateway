import base64
import json
from unittest.mock import Mock

import httpx
import pytest

from app.workspace.catalog import SERVICES, catalog


def make_service(handler, scopes=None):
    from app.workspace.service import WorkspaceService
    google = Mock()
    google.get_user_google_connection.return_value = Mock(status="connected", scopes=" ".join(scopes or ["https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/documents"]))
    google.oauth_service.get_valid_access_token.return_value = "private-test-token"
    def dispatch(request):
        if request.url.path == "/mcp/v1":
            call = json.loads(request.content)
            if call["method"] == "notifications/initialized":
                return httpx.Response(202)
            if call["method"] == "initialize":
                result = {"protocolVersion": "2025-11-25"}
            elif call["method"] == "tools/list":
                result = {"tools": [{"name": name, "inputSchema": {"type": "object"}} for name in (
                    "read_doc", "update_doc", "search_threads", "list_events", "search_files", "get_values", "read_presentation",
                )]}
            else:
                return handler(request)
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": call["id"], "result": result})
        return handler(request)

    return WorkspaceService(google, "test-signing-key", transport=httpx.MockTransport(dispatch))


def test_service_module_exists():
    from pathlib import Path
    assert Path("app/workspace/service.py").exists(), "Workspace service not implemented"


def test_google_read_uses_fixed_host_and_oauth_header():
    def handler(request):
        assert request.url.host == "www.googleapis.com"
        assert request.url.path == "/drive/v3/files"
        assert request.headers["Authorization"] == "Bearer private-test-token"
        assert request.url.params["pageSize"] == "20"
        return httpx.Response(200, json={"files": [{"id": "file1", "name": "Test"}], "nextPageToken": "next"})
    result = make_service(handler).read(1, "drive", {"method": "files.list"})
    assert result["status"] == "OK"
    from app.workspace.adapters import decode_cursor
    assert decode_cursor(result["data"]["nextPageToken"].removeprefix("ws:")) == ("rest", "next")


def test_missing_scope_stops_before_network():
    from app.workspace.service import WorkspaceError
    calls = []
    service = make_service(lambda r: calls.append(r), ["openid"])
    with pytest.raises(WorkspaceError) as exc:
        service.read(1, "drive", {"method": "files.list"})
    assert exc.value.code == "PERMISSION_DENIED"
    assert calls == []


def test_preview_does_not_write_and_ticket_binds_content_and_user():
    from app.workspace.service import WorkspaceError
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"id": "new"})
    service = make_service(handler)
    preview = service.prepare(1, "drive", {"method": "files.create", "body": {"name": "Report"}})
    assert calls == []
    arguments = preview["arguments"]
    arguments["body"]["name"] = "Tampered"
    with pytest.raises(WorkspaceError):
        service.execute(1, "drive", arguments)
    arguments["body"]["name"] = "Report"
    with pytest.raises(WorkspaceError):
        service.execute(2, "drive", arguments)
    assert calls == []
    assert service.execute(1, "drive", arguments)["data"]["id"] == "new"
    assert len(calls) == 1


def test_changed_target_refuses_mutation():
    from app.workspace.service import WorkspaceError
    revision = ["old"]
    calls = []
    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json={"id": "f1", "name": revision[0]})
    service = make_service(handler)
    preview = service.prepare(1, "drive", {"method": "files.update", "parameters": {"fileId": "f1"}, "body": {"name": "new"}})
    revision[0] = "changed"
    with pytest.raises(WorkspaceError) as exc:
        service.execute(1, "drive", preview["arguments"])
    assert exc.value.code == "CONFLICT"
    assert calls == ["GET", "GET"]


def test_write_timeout_is_unknown_and_never_retried():
    from app.workspace.service import WorkspaceError
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("test timeout", request=request)
    service = make_service(handler)
    preview = service.prepare(1, "docs", {"method": "documents.create", "body": {"title": "Test"}})
    with pytest.raises(WorkspaceError) as exc:
        service.execute(1, "docs", preview["arguments"])
    assert exc.value.code == "UNKNOWN_RESULT"
    assert len(calls) == 1


def test_binary_export_is_bounded_and_base64_encoded():
    service = make_service(lambda r: httpx.Response(200, content=b"PDF", headers={"Content-Type": "application/pdf"}))
    result = service.read(1, "drive", {"method": "files.export", "parameters": {"fileId": "abc", "mimeType": "application/pdf"}})
    assert base64.b64decode(result["data"]["content_base64"]) == b"PDF"


def test_json_file_download_preserves_bytes_instead_of_parsing_api_response():
    raw = b'{"name":"my file"}'
    service = make_service(lambda r: httpx.Response(200, content=raw, headers={"Content-Type": "application/json"}))
    result = service.read(1, "drive", {"method": "files.get", "parameters": {"fileId": "abc", "alt": "media"}})
    assert base64.b64decode(result["data"]["content_base64"]) == raw


def test_gmail_attachment_under_mime_limit_can_prepare():
    service = make_service(lambda r: httpx.Response(200, json={}), ["https://mail.google.com/"])
    attachment = {"filename": "report.txt", "mime_type": "text/plain", "data_base64": base64.b64encode(b"a" * 800000).decode()}
    preview = service.prepare(1, "gmail", {"method": "users.messages.send", "mail": {"to": "test@example.com", "subject": "Test", "text": "See attachment", "attachments": [attachment]}})
    assert preview["status"] == "OK"


def test_sheets_snapshot_includes_cells_and_blocks_changed_cell():
    from app.workspace.service import WorkspaceError
    current = ["old"]
    calls = []
    def handler(request):
        calls.append(request)
        # values.update snapshots values.get for the exact range, rather than
        # fetching unrelated grid data for the whole spreadsheet.
        assert request.url.path == '/v4/spreadsheets/abc/values/A1:B2'
        return httpx.Response(200, json={"range": "A1:B2", "values": [[current[0]]]})
    service = make_service(handler, ["https://www.googleapis.com/auth/spreadsheets"])
    preview = service.prepare(1, "sheets", {"method": "spreadsheets.values.update", "parameters": {"spreadsheetId": "abc", "range": "A1:B2", "valueInputOption": "RAW"}, "body": {"values": [["new"]]}})
    current[0] = "somebody else edited"
    with pytest.raises(WorkspaceError) as exc:
        service.execute(1, "sheets", preview["arguments"])
    assert exc.value.code == "CONFLICT"
    assert len(calls) == 2 and all(request.method == "GET" for request in calls)


def test_all_discovery_schemas_are_valid_jsonschema():
    from jsonschema import Draft7Validator

    from app.workspace.catalog import request_schema
    for service in SERVICES:
        spec = catalog()[service]
        for method in spec["methods"]:
            Draft7Validator.check_schema(request_schema(service, method))

def test_docs_read_routes_through_google_mcp():
    def handler(request):
        assert request.url.host == "docsmcp.googleapis.com"
        assert request.url.path == "/mcp/v1"
        assert request.headers["Authorization"] == "Bearer private-test-token"
        call = json.loads(request.content)
        assert call["method"] == "tools/call"
        assert call["params"] == {
            "name": "read_doc", "arguments": {"documentId": "doc1"}
        }
        return httpx.Response(200, json={
            "jsonrpc": "2.0",
            "id": call["id"],
            "result": {"structuredContent": {"content": {"title": "Report"}}},
        })

    service = make_service(handler)
    service.use_mcp = True
    assert service.read(1, "docs", {
        "method": "documents.get", "parameters": {"documentId": "doc1"}
    })["data"] == {"title": "Report"}


def test_docs_mcp_mutation_requires_confirmation_and_keeps_revision():
    calls = []

    def handler(request):
        call = json.loads(request.content)
        calls.append(call["params"])
        if call["params"]["name"] == "read_doc":
            data = {"content": {"documentId": "doc1", "revisionId": "rev1"}}
        else:
            assert call["params"]["arguments"]["writeControl"] == {"requiredRevisionId": "rev1"}
            data = {"documentId": "doc1"}
        message = {"jsonrpc": "2.0", "id": call["id"], "result": {"structuredContent": data}}
        return httpx.Response(
            200, text=f"event: message\ndata: {json.dumps(message)}\n\n",
            headers={"Content-Type": "text/event-stream"},
        )

    service = make_service(handler)
    service.use_mcp = True
    preview = service.prepare(1, "docs", {
        "method": "documents.batchUpdate",
        "parameters": {"documentId": "doc1"},
        "body": {"requests": [{"insertText": {"endOfSegmentLocation": {}, "text": "Hello"}}]},
    })
    assert [call["name"] for call in calls] == ["read_doc"]
    assert service.execute(1, "docs", preview["arguments"])["data"] == {"documentId": "doc1"}
    assert [call["name"] for call in calls] == ["read_doc", "read_doc", "update_doc"]


@pytest.mark.parametrize(("service_name", "method", "parameters", "scope", "tool"), [
    ("gmail", "users.threads.list", {"q": "is:unread"}, "gmail.readonly", "search_threads"),
    ("calendar", "events.list", {"calendarId": "primary"}, "calendar.events", "list_events"),
    ("drive", "files.list", {"q": "trashed=false"}, "drive.readonly", "search_files"),
    ("sheets", "spreadsheets.values.get", {"spreadsheetId": "id", "range": "A1"}, "spreadsheets.readonly", "get_values"),
    ("slides", "presentations.get", {"presentationId": "id"}, "presentations.readonly", "read_presentation"),
])
def test_supported_workspace_reads_call_google_mcp(service_name, method, parameters, scope, tool):
    def handler(request):
        call = json.loads(request.content)
        assert request.url.host == f"{service_name}mcp.googleapis.com"
        assert call["params"]["name"] == tool
        data = {"content": {"title": "Report"}} if service_name == "slides" else {}
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": call["id"],
            "result": {"content": [{"type": "text", "text": json.dumps(data)}]},
        })

    service = make_service(handler, [f"https://www.googleapis.com/auth/{scope}"])
    service.use_mcp = True
    assert service.read(1, service_name, {"method": method, "parameters": parameters})["status"] == "OK"


def test_workspace_rest_refresh_and_retry_preserve_protocol(monkeypatch):
    requests = []
    sleeps = []
    monkeypatch.setattr("app.workspace.service.time.sleep", sleeps.append)

    def handler(request):
        requests.append(request.headers["Authorization"])
        if len(requests) == 1:
            return httpx.Response(401)
        if len(requests) == 2:
            return httpx.Response(429, headers={"Retry-After": "0.7"})
        return httpx.Response(200, json={"files": []})

    service = make_service(handler)
    service.google.oauth_service.get_valid_access_token.side_effect = ["expired", "renewed"]
    result = service.read(1, "drive", {"method": "files.list"})
    assert result["status"] == "OK"
    assert requests == ["Bearer expired", "Bearer renewed", "Bearer renewed"]
    assert sleeps == [0.7]
    assert service.google.oauth_service.get_valid_access_token.call_args.kwargs == {"force_refresh": True}


def test_drive_knowledge_rest_does_not_disable_later_mcp():
    from app.workspace.drive_knowledge import drive_documents

    hosts = []
    def handler(request):
        hosts.append(request.url.host)
        if request.url.host == "www.googleapis.com":
            return httpx.Response(200, json={"files": []})
        call = json.loads(request.content)
        return httpx.Response(200, json={"id": call["id"], "result": {"structuredContent": {"files": []}}})

    service = make_service(handler)
    service.use_mcp = True
    assert drive_documents(service, 1, ["policy"])["files"] == []
    assert service.read(1, "drive", {"method": "files.list"})["status"] == "OK"
    assert hosts == ["www.googleapis.com", "www.googleapis.com", "drivemcp.googleapis.com"]


def test_workspace_cursor_cannot_cross_protocol_after_mcp_failure(monkeypatch):
    from app.workspace.adapters import encode_cursor
    from app.workspace.service import WorkspaceError

    requests = []
    monkeypatch.setattr("app.workspace.service.time.sleep", lambda _: None)
    def handler(request):
        requests.append(request.url.host)
        return httpx.Response(503)

    service = make_service(handler)
    service.use_mcp = True
    payload = {"method": "files.list", "parameters": {"pageToken": "ws:" + encode_cursor("rest", "next")}}
    with pytest.raises(WorkspaceError, match="phân trang") as error:
        service.read(1, "drive", payload)
    assert error.value.code == "INVALID_CURSOR"
    assert requests == []
