# Vận hành Auth Gateway

Tài liệu dùng khi khởi động, xem log, sao lưu hoặc xử lý sự cố. Cấu hình ban đầu nằm trong [SETUP.md](SETUP.md).

## Kiểm tra dịch vụ

Local chạy mặc định ở cổng 8080. Compose cũng công bố cổng 8080 trên host, còn ứng dụng trong container nghe ở 8000.

```powershell
Invoke-WebRequest -UseBasicParsing http://localhost:8080/healthz
docker compose ps
docker compose logs --tail 100 gateway
```

Healthcheck cho biết ứng dụng và kiểm tra sức khỏe của nó phản hồi. Tiếp tục thử đăng nhập và gọi dịch vụ để kiểm tra kết nối Odoo hoặc Google của từng tài khoản.

## Theo dõi lỗi từ một tin nhắn

1. Lấy update_id hoặc request_id của lượt Telegram.
2. Tìm request_id trong log Gateway và log Agent.
3. Nếu xử lý nền, tìm tiếp job_id trong log worker.
4. Nếu ghi dữ liệu, tìm operation_id và trạng thái trong API Actions.
5. Đối chiếu bản ghi thực tế trên Odoo hoặc Google khi kết quả còn chưa xác định.

```powershell
docker compose logs --since 10m gateway | Select-String -SimpleMatch 'tg-12345'
```

Các file cần đọc theo tình huống nằm trong bảng dưới.

| Tình huống | Vị trí |
|---|---|
| Update Telegram chưa chuyển sang Agent | app/telegram/routes.py, app/telegram/worker.py |
| Danh tính hoặc quyền bị từ chối | app/internal/identity.py, app/internal/dependencies.py |
| Action lỗi hoặc cần đối soát | app/internal/actions.py |
| Business prepare hoặc thực thi lỗi | app/business/routes.py, app/business/service.py |
| Workspace job hoặc callback chậm | app/workspace/worker.py, app/workspace/routes.py |
| Đọc Drive hoặc chỉ mục lỗi | app/workspace/drive_retriever.py, app/workspace/drive_runtime.py |
| Báo cáo hoặc báo giá chậm | app/reporting/worker.py, app/quotations/jobs.py |

## Sao lưu SQLite

Kiểm tra DATABASE_URL trước khi chọn đường dẫn nguồn. Local thường dùng ./data/gateway.db, Compose dùng /data/gateway.db.

Script [backup.py](../scripts/backup.py) dùng SQLite Online Backup API để tạo bản sao nhất quán khi database đang hoạt động.

```powershell
.\.venv\Scripts\python.exe scripts/backup.py --source ./data/gateway.db --dest ./backups
docker compose exec gateway python scripts/backup.py --source /data/gateway.db --dest /data/backups
```

Bản sao trong /data/backups vẫn nằm cùng volume dữ liệu. Chuyển bản sao sang nơi lưu trữ độc lập theo quy trình vận hành. Lưu ENCRYPTION_KEY ở nơi bảo mật và bảo đảm có thể tìm lại đúng khóa tương ứng khi phục hồi.

## Phục hồi SQLite

1. Dừng Gateway, poller và mọi worker đang dùng database đó.
2. Sao lưu lại dữ liệu hiện tại trước khi thay thế.
3. Kiểm tra bản sao bằng SQLite integrity_check.
4. Khôi phục vào database mới, giữ riêng bộ database cũ cùng các file WAL/SHM liên quan.
5. Trỏ DATABASE_URL đến bản phục hồi hoặc thay bộ dữ liệu tại vị trí đã xác minh.
6. Khởi động với ENCRYPTION_KEY tương ứng.
7. Kiểm tra healthcheck, đăng nhập, liên kết và trạng thái tác vụ dở dang.

Đối với PostgreSQL, dùng công cụ sao lưu và phục hồi PostgreSQL theo môi trường triển khai. scripts/backup.py phục vụ SQLite.

## Khởi động PostgreSQL và worker riêng

```powershell
docker compose -f compose.yaml -f compose.postgres.yaml up -d --build gateway workspace-worker drive-indexer
docker compose -f compose.yaml -f compose.postgres.yaml logs --tail 100 workspace-worker drive-indexer
```

Migration chạy qua service migrate. WORKSPACE_EMBEDDED_WORKER=false được đặt trong cấu hình này. Kiểm tra log từng worker và database URL thực tế khi job đã tạo nhưng chưa được nhận.

Lease là thời hạn một worker giữ quyền xử lý job. Khi worker mất kết nối, khả năng nhận lại phụ thuộc trạng thái job và việc lệnh ghi đã được gửi đi hay chưa. Đọc trạng thái và dấu vết thực thi trước khi can thiệp.

## Xử lý sự cố thường gặp

| Hiện tượng | Kiểm tra và cách xử lý |
|---|---|
| Đăng nhập Odoo thất bại | Kiểm tra tenant, database, tài khoản và lỗi trả từ Odoo. Đăng nhập lại khi credential hết hiệu lực |
| Google yêu cầu kết nối lại | Kiểm tra OAuth client, scope, trạng thái token và tài khoản đang chọn. Cấp quyền lại trên Portal |
| Bot chưa nhận tin | Kiểm tra liên kết private chat, token bot, poller hoặc webhook, callback_query và log hàng đợi |
| Agent trả 401 hoặc 403 | Đối chiếu secret theo từng chiều, binding và phạm vi tài khoản |
| Gateway gọi Agent không được | Kiểm tra AI_AGENT_BASE_URL từ môi trường Gateway đang chạy |
| UNKNOWN_RESULT | Giữ operation_id, tra cứu trạng thái và đối chiếu dịch vụ đích |
| delivery_unknown | Kiểm tra tin thực tế trong Telegram và log giao nhận trước khi gửi lại |
| Business prepare bị chặn | Kiểm tra cờ phân hệ, quyền Odoo, trạng thái chứng từ, trường bắt buộc và quy trình duyệt |
| Job ở trạng thái chờ lâu | Kiểm tra worker, lease, deadline và callback |

## Dừng hoặc cập nhật dịch vụ

Có thể tắt chuyển hội thoại bằng ENABLE_AI_AGENT_FORWARDING=false rồi khởi động lại Gateway. Trước khi cập nhật phiên bản, sao lưu dữ liệu và ghi lại cấu hình, revision migration, phiên bản code đang chạy.

```powershell
docker compose stop gateway
docker compose up -d --build gateway
```

Dữ liệu cần giữ để phục hồi gồm database, file xuất cần sử dụng, khóa mã hóa và cấu hình triển khai. Khi chia sẻ log, chỉ gửi thông tin cần đối soát và che credential, token cùng dữ liệu cá nhân.
