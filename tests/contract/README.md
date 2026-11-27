# Kiểm thử hợp đồng Workspace

Hợp đồng là cấu trúc HTTP mà Gateway và Agent cùng sử dụng, gồm endpoint, phương thức, payload và phản hồi. Nhóm test này giúp phát hiện hai repo hiểu khác nhau về cùng lời gọi.

## Các file liên quan

| File | Vai trò |
|---|---|
| workspace-v1.json | Fixture hợp đồng do Gateway sở hữu |
| test_workspace_peer.py | Kiểm thử ghép Gateway với Agent qua HTTP |

Agent có bản sao workspace-v1.json và file workspace_peer.py trong tests/contract. Khi đổi hợp đồng, cập nhật phiên bản fixture và đồng bộ bản sao giống từng byte sang Agent.

## Chạy kiểm tra trong từng repo

Chạy từ thư mục gốc repo có scripts/dev.ps1.

```powershell
./scripts/dev.ps1 test contract
```

Test peer cần thêm dịch vụ và database riêng theo phần dưới.

## Chuẩn bị test xuyên hai dịch vụ

Cần PostgreSQL dùng riêng cho kiểm thử, có khả năng đáp ứng các extension mà schema yêu cầu. Test tạo dữ liệu thử trong database được cấu hình. Hai repo cần cài dependencies kiểm thử và truy cập được nhau qua HTTP.

Terminal Agent chạy router thật với bộ phân loại ý định cố định của fixture.

```powershell
$env:GATEWAY_BASE_URL = 'http://127.0.0.1:8765'
$env:GATEWAY_TO_AGENT_SECRET = 'workspace-peer-gateway-to-agent-secret'
$env:AGENT_TO_GATEWAY_SECRET = 'workspace-peer-agent-to-gateway-secret'
$env:WORKSPACE_BACKGROUND_READS = 'true'
$env:PYTHONPATH = ".;$((Resolve-Path tests/contract).Path)"
python -m uvicorn workspace_peer:app --port 8766
```

Terminal Gateway chạy bài thử với database riêng. Thay URL PostgreSQL bên dưới bằng URL môi trường kiểm thử.

```powershell
$env:APP_ENV = 'development'
$env:DATABASE_URL = 'sqlite:///:memory:'
$env:WORKSPACE_PEER_POSTGRES_URL = 'postgresql://TEST_USER:TEST_PASSWORD@127.0.0.1:5432/TEST_DATABASE'
$env:WORKSPACE_PEER_AGENT_URL = 'http://127.0.0.1:8766'
python -m pytest -q tests/contract/test_workspace_peer.py
```

Nếu thiếu WORKSPACE_PEER_POSTGRES_URL hoặc WORKSPACE_PEER_AGENT_URL, fixture bỏ qua ca peer. Các biến này chỉ dùng trong terminal kiểm thử.

## Test kiểm tra điều gì

Bài thử theo dõi update Telegram trùng, request_id, job, tài khoản Google được chọn và callback. Luồng ghi kiểm tra bản xem trước, xác nhận lặp và số lần adapter gọi dịch vụ.

Odoo, Google, Telegram và LLM trong ca peer dùng dữ liệu mô phỏng. Khi nghiệm thu dịch vụ thực tế, thực hiện thêm các ca trong [ACCEPTANCE.md](../../docs/ACCEPTANCE.md).
