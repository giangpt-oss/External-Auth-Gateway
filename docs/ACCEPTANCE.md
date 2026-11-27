# Kiểm tra trước khi bàn giao

Tài liệu là danh sách kiểm tra để chạy trên phiên bản code và môi trường cụ thể. Ghi kết quả thực tế sau mỗi lần chạy, kèm ngày, phiên bản code và bằng chứng.

## 1. Chuẩn bị

Ghi lại database sử dụng, tenant Odoo, tài khoản thử, cờ chức năng đang bật và cách chạy dịch vụ. Các ca ghi dữ liệu cần dùng môi trường và dữ liệu đã được cho phép thử nghiệm.

## 2. Kiểm tra tự động

Chạy tại thư mục gốc Gateway.

```powershell
.\.venv\Scripts\python.exe -m ruff check app tests
./scripts/dev.ps1 test unit
./scripts/dev.ps1 test integration
./scripts/dev.ps1 test contract
```

Script dev.ps1 tạo môi trường chạy test riêng. Xem nội dung script khi cần kiểm tra danh sách file được chọn hoặc loại trừ.

Các bài thử cần repo Agent nằm cạnh Gateway chạy riêng.

Nếu repo Agent nằm ở vị trí khác, đặt biến môi trường AI_AGENT_REPO thành đường dẫn repo đó trước khi chạy.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_full_lifecycle_cross_app.py tests/integration/test_live_cross_app_e2e.py -v
```

Các bài PostgreSQL và test peer có điều kiện môi trường riêng. Xem [tests/contract/README.md](../tests/contract/README.md). Ghi số passed, failed, skipped và lý do bỏ qua để người nhận bàn giao biết phạm vi đã kiểm tra.

## 3. Kiểm tra chức năng thực tế

| Nhóm | Thao tác | Kết quả cần đối chiếu |
|---|---|---|
| Khởi động | Chạy service, mở /healthz | Dịch vụ phản hồi và log khởi động rõ ràng |
| Odoo | Đăng nhập Portal bằng tài khoản thử | Đúng người dùng và quyền truy cập |
| Telegram | Liên kết, mở bot, bấm Start | Đúng user, private chat và binding |
| Google | Liên kết và chọn tài khoản | Đúng tài khoản, scope và trạng thái kết nối |
| Đọc dữ liệu | Hỏi dữ liệu Calendar, Gmail hoặc Odoo | Kết quả khớp nguồn và phạm vi được xem |
| Ghi dữ liệu | Xem trước rồi xác nhận một thao tác thử | Bản ghi đích khớp dữ liệu đã duyệt |
| Hủy xác nhận | Từ chối trên bản xem trước | Yêu cầu được hủy đúng phạm vi |
| Xác nhận lặp | Gửi lại cùng xác nhận | Đối chiếu kết quả đã lưu và số lần ghi ở nguồn |
| Mất quyền | Thu hồi quyền rồi thử lại | Yêu cầu bị từ chối đúng bước |
| Đổi liên kết | Ngắt rồi ghép nối Telegram lại | Yêu cầu gắn binding cũ bị kiểm tra lại |
| Job nền | Chạy báo cáo hoặc Workspace job | Theo dõi được job, callback và nội dung giao |
| Phục hồi | Khôi phục bản sao vào môi trường thử | Đọc được dữ liệu và giải mã bằng khóa tương ứng |

## 4. Kiểm tra mua hàng

1. Chọn nhà cung cấp và sản phẩm đang có trên Odoo.
2. Nhập số lượng, kiểm tra giá, thuế, đơn vị và ngày nhận trong bản xem trước.
3. Xác nhận tạo RFQ, ghi lại số RFQ và đối chiếu dòng hàng trên Odoo.
4. Yêu cầu xác nhận đúng RFQ đó, kiểm tra bản xem trước của bước xác nhận.
5. Xác nhận và đọc lại trạng thái Odoo.
6. Nếu trạng thái là to approve, thử bước duyệt bằng tài khoản có quyền rồi đối chiếu trạng thái cuối.
7. Kiểm tra trường hợp thiếu sản phẩm, trùng tên, thiếu quyền, sửa nháp và dữ liệu nguồn thay đổi sau bản xem trước.

RFQ ở draft là nháp. to approve là chờ duyệt. purchase là đơn mua đã xác nhận. Chỉ ghi nhận hoàn thành theo trạng thái thực tế và yêu cầu nghiệp vụ của ca thử.

Luồng duyệt tùy chỉnh có thể bị chặn nếu Gateway chưa xác minh được điều kiện. Ghi rõ thông báo và bước còn thiếu trong biên bản.

## 5. Ghi kết quả

| Thông tin | Nội dung cần điền |
|---|---|
| Thời điểm và phiên bản | Ngày chạy, commit hoặc phiên bản triển khai |
| Môi trường | Local hoặc Docker, SQLite hoặc PostgreSQL |
| Ca kiểm tra | Yêu cầu, tài khoản và cờ chức năng |
| Kết quả | Đạt, lỗi hoặc chưa kiểm tra |
| Bằng chứng | Lệnh test, log, request_id, operation_id, số chứng từ |
| Phần còn lại | Bước chưa chạy và nguyên nhân cụ thể |

Các số liệu kiểm thử của phiên bản cũ cần được thay bằng kết quả vừa chạy khi bàn giao phiên bản mới.

## 6. Kết quả rà soát ngày 27/11/2026

Môi trường kiểm tra là Windows, Python 3.11 và database SQLite tạm do script test tạo.

| Kiểm tra | Kết quả |
|---|---|
| Bộ chính qua scripts/dev.ps1 test all | 436 passed, 9 skipped |
| Hai file test_full_lifecycle_cross_app.py và test_live_cross_app_e2e.py | 5 passed |
| Ruff cho app, scripts, alembic và tests | Đạt |
| Cú pháp Python | 123 file đạt |
| Cú pháp Jinja | 22 template đạt |
| JavaScript app.js và stitch.js | Đạt |
| pip check | Đạt |
| git diff --check | Đạt |

Chín ca bỏ qua cần PostgreSQL dùng riêng, pgvector hoặc PostgreSQL cùng Agent peer. Các bài HTTP xuyên hai ứng dụng dùng dịch vụ ngoài mô phỏng và database tạm. Kết quả trên cần được bổ sung bằng nghiệm thu tenant và hạ tầng triển khai thực tế.

Môi trường Python đã được cài pypdf 7.2.0 theo khoảng phiên bản khai báo trong pyproject.toml. Ca đọc PDF đã chạy thành công. Bộ chính còn một cảnh báo deprecation từ Starlette/AnyIO.
