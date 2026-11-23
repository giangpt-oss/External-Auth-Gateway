# Kiểm thử hợp đồng Workspace

`workspace-v1.json` là mẫu HTTP do Gateway sở hữu. Gateway kiểm tra trường bắt buộc của phản hồi thực. Client Agent kiểm tra đường dẫn, phương thức, payload và cách nhận phản hồi từ cùng mẫu. Khi đổi hợp đồng, cập nhật phiên bản mẫu và chuyển bản sao giống từng byte sang thư mục `tests/contract` của Agent trong PR tương ứng.

Chạy `./scripts/dev.ps1 test contract` ở mỗi repo để kiểm tra hợp đồng riêng của dịch vụ.

## Kiểm thử Telegram xuyên hai dịch vụ

Chuẩn bị một PostgreSQL thử nghiệm hỗ trợ extension `vector` và một database dùng riêng cho ca kiểm thử. PostgreSQL, Agent và Gateway phải truy cập được nhau. Cài dependencies test của từng repo và chạy mỗi lệnh từ thư mục gốc repo tương ứng với môi trường cấu hình thử.

Ở terminal của Agent, khởi chạy router thực với bộ phân loại trả về ý định đọc, chuẩn bị ghi và xác nhận cố định. Các giá trị bên dưới chỉ dùng cho ca thử.

```powershell
$env:GATEWAY_BASE_URL = 'http://127.0.0.1:8765'
$env:GATEWAY_TO_AGENT_SECRET = 'workspace-peer-gateway-to-agent-secret'
$env:AGENT_TO_GATEWAY_SECRET = 'workspace-peer-agent-to-gateway-secret'
$env:WORKSPACE_BACKGROUND_READS = 'true'
$env:PYTHONPATH = ".;$((Resolve-Path tests/contract).Path)"
python -m uvicorn workspace_peer:app --port 8766
```

Ở terminal của Gateway, chạy bài thử và truyền URL database PostgreSQL riêng. Ca thử tạo người dùng và dữ liệu giả trong database này.

```powershell
$env:APP_ENV = 'development'
$env:DATABASE_URL = 'sqlite:///:memory:'
$env:WORKSPACE_PEER_POSTGRES_URL = 'postgresql://TEST_USER:TEST_PASSWORD@127.0.0.1:5432/TEST_DATABASE'
$env:WORKSPACE_PEER_AGENT_URL = 'http://127.0.0.1:8766'
python -m pytest -q tests/contract/test_workspace_peer.py
```

Bài thử gửi hai lần cùng một Telegram update, theo dõi `request_id` tới job đã chốt tài khoản, xác thực token ở adapter Google và kiểm tra callback chỉ giao kết quả một lần. Lượt ghi tạo bản xem trước, gửi hai xác nhận cho cùng một thao tác và kiểm tra adapter chỉ gọi Google một lần. Odoo, Google, Telegram và LLM dùng dữ liệu mô phỏng.
