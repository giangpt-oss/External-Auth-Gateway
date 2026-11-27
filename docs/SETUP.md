# Cài đặt Auth Gateway

Chạy các lệnh tại thư mục gốc Gateway. Biến cấu hình đầy đủ nằm trong [.env.example](../.env.example), kiểu dữ liệu và mặc định nằm trong [app/config.py](../app/config.py).

## 1. Cài Python và dependencies

Dùng Python 3.11 trở lên.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

## 2. Điền cấu hình nền tảng

| Biến | Ý nghĩa |
|---|---|
| APP_ENV | development khi chạy local |
| PUBLIC_BASE_URL | URL Portal, local thường là http://localhost:8080 |
| ODOO_URL, ODOO_DB | URL tenant và tên database Odoo |
| DATABASE_URL | Local thường là sqlite:///./data/gateway.db, tính từ thư mục chạy |
| ENCRYPTION_KEY | Khóa Fernet mã hóa credential và token |
| ENABLE_AI_AGENT_FORWARDING | true để chuyển hội thoại sang Agent |

Tạo khóa một lần cho môi trường mới, lưu vào .env và sao lưu cùng kế hoạch phục hồi dữ liệu.

```powershell
.\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Database hiện có cần đúng khóa đã dùng mã hóa dữ liệu. Nhân viên đăng nhập bằng tài khoản Odoo đáp ứng kiểm tra trong app/auth/ và có quyền nghiệp vụ cần sử dụng.

## 3. Kết nối AI Agent

Đặt AI_AGENT_BASE_URL theo địa chỉ Agent mà Gateway truy cập được. Hai secret dưới đây phải khác nhau, mỗi chuỗi ít nhất 32 ký tự và khớp ở hai repo theo từng chiều.

| Biến | Chiều gọi |
|---|---|
| GATEWAY_TO_AGENT_SECRET | Gateway gọi Agent |
| AGENT_TO_GATEWAY_SECRET | Agent gọi Gateway |

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48)); print(secrets.token_urlsafe(48))"
```

Xem địa chỉ local và Docker trong [hướng dẫn tích hợp](INTEGRATION_AI_AGENT.md).

## 4. Kết nối Google

Điền GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET và đăng ký callback theo PUBLIC_BASE_URL.

```text
http://localhost:8080/oauth/google/callback
```

GOOGLE_WORKSPACE_DOMAINS để trống cho phép cả Gmail cá nhân. Điền danh sách miền khi cần giới hạn tài khoản.

Scope mặc định được khai báo trong [app/google/oauth.py](../app/google/oauth.py), gồm OpenID/email, Calendar, Gmail và quyền đọc Drive, Docs, Sheets. Khi người dùng chọn dịch vụ cụ thể, scope được xác định theo catalog Workspace. Sau khi bổ sung quyền hoặc đổi OAuth client, vào Portal cấp quyền lại.

GOOGLE_WORKSPACE_MCP_ENABLED điều khiển nhánh MCP. Khả năng sử dụng từng operation phụ thuộc provider và cấu hình Google của môi trường. Xem triển khai trong app/workspace/ khi bật nhánh này.

## 5. Kết nối Telegram

Điền TELEGRAM_BOT_TOKEN, TELEGRAM_BOT_USERNAME và TELEGRAM_WEBHOOK_SECRET.

Local có thể mở terminal thứ hai chạy poller.

```powershell
.\.venv\Scripts\python.exe scripts/telegram_poller.py
```

Nếu dùng webhook, cấu hình URL HTTPS công khai trỏ đến /webhooks/telegram và secret tương ứng. Đăng ký update message và callback_query để nhận cả tin nhắn và nút xác nhận. Chọn một cơ chế nhận update cho mỗi bot.

Nhân viên đăng nhập Portal, chọn liên kết Telegram, mở bot và bấm Start.

## 6. Bật chức năng theo phân hệ

| Nhóm | Cờ chính |
|---|---|
| Báo cáo Odoo | ENABLE_REPORTING |
| Báo giá bán | ENABLE_QUOTATIONS |
| Duyệt báo giá | ENABLE_QUOTATION_APPROVAL_FLOW |
| Nghiệp vụ Odoo | ENABLE_BUSINESS_ACTIONS cùng cờ phân hệ |
| Mua hàng | ENABLE_BUSINESS_ACTIONS=true và ENABLE_BUSINESS_PURCHASES=true |
| Workspace worker trong Gateway | WORKSPACE_EMBEDDED_WORKER |
| Chỉ mục Drive | DRIVE_INDEX_ENABLED |

Các cờ phân hệ còn lại nằm trong app/config.py. Xóa và gửi email có cờ kiểm tra thêm. Agent cần bật chức năng tương ứng.

## 7. Chạy local hoặc Docker

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Mở Portal và /healthz, sau đó thử đăng nhập và từng kết nối.

```powershell
docker compose up -d --build gateway
docker compose logs -f gateway
```

[compose.yaml](../compose.yaml) ánh xạ 8080 trên host vào 8000 trong container. SQLite được lưu ở volume auth_gateway_data.

Service tunnel dùng CLOUDFLARE_TUNNEL_TOKEN. Đích nội bộ trong Cloudflare là http://gateway:8000. PUBLIC_BASE_URL cần khớp domain công khai khi dùng OAuth bên ngoài local.

## 8. PostgreSQL và worker riêng

Đặt WORKSPACE_POSTGRES_PASSWORD và WORKSPACE_POSTGRES_URL rồi dùng cấu hình bổ sung.

```powershell
docker compose -f compose.yaml -f compose.postgres.yaml up -d --build gateway workspace-worker drive-indexer
```

[compose.postgres.yaml](../compose.postgres.yaml) bổ sung PostgreSQL, migration và worker riêng, đồng thời tắt embedded worker. Nếu cần giữ dữ liệu SQLite, chuẩn bị quy trình chuyển dữ liệu trước khi đổi database. Xem [OPERATIONS.md](OPERATIONS.md) về sao lưu và kiểm tra sau khởi động.
