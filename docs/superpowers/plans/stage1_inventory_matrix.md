# Bảng Kiểm Kê Kỹ Thuật: Agent → Gateway → Google / Odoo / MCP (Giai đoạn 1)

Ngày lập: 2026-11-13  
Trạng thái: Hoàn thành kiểm kê Giai đoạn 1  

---

## 1. Google Workspace Services

| Dịch vụ | Thao tác (Operation) | Giao thức khả dụng | Tham số đầu vào chính | Quyền/Scopes yêu cầu | Cơ chế phân trang | Schema kết quả chuẩn hóa |
|---|---|---|---|---|---|---|
| **Gmail** | `users.messages.list` | REST & MCP (`search_threads` / `get_message`) | `q`, `maxResults`, `pageToken`, `labelIds` | `https://www.googleapis.com/auth/gmail.readonly` | Cursor mã hóa protocol (`{"p":"rest\|mcp","t":token}`) | `{"messages": [{"id": str, "thread_id": str, "snippet": str, "date": str, "from": str, "subject": str}], "next_page_token": str\|None}` |
| **Gmail** | `users.messages.get` | REST & MCP (`get_message`) | `id`, `format` (`metadata` / `full`) | `https://www.googleapis.com/auth/gmail.readonly` | Không phân trang | `{"id": str, "thread_id": str, "snippet": str, "headers": dict, "body_text": str}` |
| **Gmail** | `users.drafts.create` / `update` | REST (Write) | `body` (MIME EmailMessage `to`, `subject`, `text`, `attachments`) | `https://www.googleapis.com/auth/gmail.compose` | Không phân trang | `{"id": str, "message": {"id": str, "thread_id": str}}` |
| **Calendar** | `events.list` | REST & MCP (`list_events`) | `calendarId`, `timeMin`, `timeMax`, `timeZone`, `maxResults`, `pageToken` | `https://www.googleapis.com/auth/calendar.readonly` / `calendar.events` | Cursor mã hóa protocol (`{"p":"rest\|mcp","t":token}`) | `{"events": [{"id": str, "summary": str, "start": str, "end": str, "location": str, "link": str}], "next_page_token": str\|None}` |
| **Calendar** | `events.get` | REST & MCP (`get_event`) | `calendarId`, `eventId` | `https://www.googleapis.com/auth/calendar.readonly` | Không phân trang | `{"id": str, "summary": str, "description": str, "start": str, "end": str, "attendees": list}` |
| **Calendar** | `events.insert` / `patch` / `delete` | REST (Write - Idempotent) | `calendarId`, `summary`, `start`, `end`, `attendees`, `operation_id` | `https://www.googleapis.com/auth/calendar.events` | Không phân trang | `{"id": str, "status": "confirmed", "htmlLink": str}` |
| **Drive** | `files.list` | REST & MCP (`search_files`) | `pageSize`, `pageToken`, `q` (`modifiedTime desc`, Shared Drive support) | `https://www.googleapis.com/auth/drive.readonly` | Cursor mã hóa protocol (`{"p":"rest\|mcp","t":token}`) | `{"files": [{"id": str, "name": str, "mime_type": str, "modified_time": str, "size": int}], "next_page_token": str\|None}` |
| **Drive** | `files.get` | REST & MCP (`get_file_metadata`) | `fileId`, `fields="id,version,trashed,capabilities(canDownload)"` | `https://www.googleapis.com/auth/drive.readonly` | Không phân trang | `{"id": str, "name": str, "version": str, "trashed": bool, "capabilities": {"canDownload": bool}}` |
| **Drive / Docs / Sheets / Slides** | File Export / Download (`alt=media`) | REST Only (bắt buộc) | `fileId`, `mimeType` export | `https://www.googleapis.com/auth/drive.readonly` | Không phân trang (Stream 8 MiB limit) | `{"content_base64": str, "mime_type": str, "size_bytes": int}` |
| **Docs** | `documents.get` | REST & MCP (`read_doc`) | `documentId` | `https://www.googleapis.com/auth/documents.readonly` | Không phân trang | `{"documentId": str, "title": str, "body": dict}` |
| **Sheets** | `spreadsheets.values.get` | REST & MCP (`get_values`) | `spreadsheetId`, `range` | `https://www.googleapis.com/auth/spreadsheets.readonly` | Không phân trang | `{"spreadsheetId": str, "range": str, "values": list[list]}` |
| **Slides** | `presentations.get` | REST & MCP (`read_presentation`) | `presentationId` | `https://www.googleapis.com/auth/presentations.readonly` | Không phân trang | `{"presentationId": str, "title": str, "slides": list}` |

---

## 2. ERP / CRM Odoo Services

| Dịch vụ | Thao tác (Operation) | Giao thức | Tham số đầu vào | Quyền/Xác thực yêu cầu | Cơ chế phân trang | Schema kết quả |
|---|---|---|---|---|---|
| **Identity** | `identity/resolve` | Internal HTTP REST | `telegram_user_id`, `chat_id`, `contract_version=2` | `GATEWAY_TO_AGENT_SECRET` | Không | `{"user_id": int, "tenant": str, "database": str, "odoo_uid": int, "name": str, "login": str, "contract_version": 2}` |
| **CRM Lead** | `crm.lead.search_read` | XML-RPC / JSON-RPC | `domain`, `fields`, `limit`, `offset` | Odoo Session / API Key (`res.users`) | `offset` + `limit` | `{"records": [{"id": int, "name": str, "partner_id": list, "expected_revenue": float, "stage_id": list}]}` |
| **Partner** | `res.partner.search_read` | XML-RPC / JSON-RPC | `domain`, `fields`, `limit` | Odoo Session / API Key | `offset` + `limit` | `{"records": [{"id": int, "name": str, "email": str, "phone": str, "company_name": str}]}` |
| **Sale Order** | `sale.order.search_read` | XML-RPC / JSON-RPC | `domain`, `fields`, `limit` | Odoo Session / API Key | `offset` + `limit` | `{"records": [{"id": int, "name": str, "amount_total": float, "state": str, "partner_id": list}]}` |

---

## 3. Quy Tắc Đo Lường 7 Giai Đoạn (Timing Telemetry)

1. `classification_ms`: Đo thời gian bộ phân loại ý định (NLP regex / Classifier) từ khi nhận text đến khi chọn tool.
2. `queue_wait_ms`: Thời gian tác vụ nằm trong hàng đợi `workspace_jobs` trước khi được một worker nhận việc (`now - created_at`).
3. `token_refresh_ms`: Thời gian kiểm tra và làm mới token OAuth qua `DistributedOAuthLeaseManager` (nếu cần).
4. `external_call_ms`: Thời gian thực hiện request HTTP mạng ngoài tới máy chủ Google API, Odoo RPC hoặc MCP Endpoint.
5. `internal_retrieval_ms`: Thời gian truy vấn cơ sở dữ liệu nội bộ (SQLAlchemy / PostgreSQL / FTS / Vector search).
6. `permission_check_ms`: Thời gian xác thực danh tính, lấy trạng thái kết nối và kiểm tra scopes/capabilities.
7. `llm_synthesis_ms`: Thời gian gửi prompt tới LLM (Groq / Gemini) và nhận phản hồi tổng hợp cuối cùng.

