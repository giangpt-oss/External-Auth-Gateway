"""Adapters from Workspace operations to Google's documented MCP tools."""

import json

import httpx

MAX_RESPONSE_BYTES = 8 * 1024 * 1024

ENDPOINTS = {
    service: f"https://{service}mcp.googleapis.com/mcp/v1"
    for service in ("gmail", "calendar", "drive", "docs", "sheets", "slides")
}

# An operation is eligible only when all of its parameters have a known mapping.
READ_TOOLS = {
    ("gmail", "users.messages.get"): ("get_message", {"id": "messageId"}),
    ("gmail", "users.threads.get"): ("get_thread", {"id": "threadId"}),
    ("gmail", "users.threads.list"): ("search_threads", {"q": "query", "maxResults": "pageSize", "pageToken": "pageToken"}),
    ("calendar", "events.get"): ("get_event", {"calendarId": "calendarId", "eventId": "eventId"}),
    ("calendar", "events.list"): ("list_events", {
        "calendarId": "calendarId", "maxResults": "pageSize", "pageToken": "pageToken",
        "timeMin": "startTime", "timeMax": "endTime", "timeZone": "timeZone",
    }),
    ("drive", "files.get"): ("get_file_metadata", {"fileId": "fileId"}),
    ("drive", "files.list"): ("search_files", {"pageSize": "pageSize", "pageToken": "pageToken"}),
    ("docs", "documents.get"): ("read_doc", {"documentId": "documentId"}),
    ("sheets", "spreadsheets.values.get"): ("get_values", {"spreadsheetId": "spreadsheetId", "range": "range"}),
    ("sheets", "spreadsheets.get"): ("get_spreadsheet", {"spreadsheetId": "spreadsheetId", "includeGridData": "includeGridData", "ranges": "ranges"}),
    ("slides", "presentations.get"): ("read_presentation", {"presentationId": "presentationId"}),
}
UPDATE_TOOLS = {
    ("docs", "documents.batchUpdate"): ("update_doc", "documentId"),
    ("slides", "presentations.batchUpdate"): ("update_presentation", "presentationId"),
}


def translate(service, method, params, body, media):
    if media is not None:
        return None
    params = {key: value for key, value in params.items() if key != "userId" or service != "gmail"}
    read = READ_TOOLS.get((service, method))
    if read and not body:
        name, fields = read
        if name == "search_files" and params.get("q") == "trashed=false":
            # The MCP search tool only searches eligible, non-trashed files.
            params.pop("q")
        if params.keys() <= fields.keys() and not (name == "search_threads" and params.get("maxResults", 20) > 50):
            args = {fields[key]: value for key, value in params.items()}
            if service == "drive":
                args["excludeContentSnippets"] = True
            return name, args
    update = UPDATE_TOOLS.get((service, method))
    if update:
        name, id_field = update
        if params.keys() == {id_field} and body.keys() <= {"requests", "writeControl"} and body.get("requests"):
            return name, {**params, **body}
    if service == "sheets" and method == "spreadsheets.values.update":
        if (
            params.keys() == {"spreadsheetId", "range", "valueInputOption"}
            and params["valueInputOption"] == "RAW"
            and body.keys() == {"values"}
        ):
            return "update_values", {
                "spreadsheetId": params["spreadsheetId"], "range": params["range"],
                "values": body["values"],
            }
    return None


class GoogleMcpError(Exception):
    def __init__(self, status_code=None):
        self.status_code = status_code
        super().__init__("Google Workspace MCP request failed")


def call_tool(service, token, name, arguments, *, transport=None):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": name, "arguments": arguments,
    }}
    try:
        with httpx.Client(timeout=9.0, transport=transport) as client:
            with client.stream("POST", ENDPOINTS[service], headers=headers, json=payload) as response:
                if response.status_code != 200:
                    raise GoogleMcpError(response.status_code)
                if "text/event-stream" in response.headers.get("content-type", ""):
                    message = _sse_response(response.iter_text())
                else:
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > MAX_RESPONSE_BYTES:
                            raise GoogleMcpError()
                    message = json.loads(raw)
        if message.get("id") != 1 or "error" in message:
            raise GoogleMcpError()
        result = message["result"]
        if result.get("isError"):
            raise GoogleMcpError()
        data = result.get("structuredContent")
        if data is None:
            blocks = result["content"]
            if len(blocks) != 1 or blocks[0]["type"] != "text":
                raise GoogleMcpError()
            data = json.loads(blocks[0]["text"])
        if not isinstance(data, dict):
            raise GoogleMcpError()
        if name in {"read_doc", "read_presentation"}:
            return data["content"]
        if name == "get_file_metadata":
            return _drive_file(data)
        if name == "search_files":
            return {**data, "files": [_drive_file(item) for item in data.get("files", [])]}
        if name == "list_events":
            return {**data, "items": data.get("events", [])}
        return data
    except (httpx.RequestError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise GoogleMcpError() from exc


def _sse_response(chunks):
    buffer = ""
    received = 0
    for chunk in chunks:
        received += len(chunk.encode())
        if received > MAX_RESPONSE_BYTES:
            raise GoogleMcpError()
        events = (buffer + chunk).replace("\r\n", "\n").split("\n\n")
        buffer = events.pop()
        for event in events:
            data = "\n".join(line[5:].lstrip(" ") for line in event.splitlines() if line.startswith("data:"))
            if data:
                message = json.loads(data)
                if isinstance(message, dict) and message.get("id") == 1:
                    return message
    raise GoogleMcpError()


def _drive_file(data):
    names = {"title": "name", "viewUrl": "webViewLink", "fileSize": "size"}
    return {names.get(key, key): value for key, value in data.items()}
