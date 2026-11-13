# Kế hoạch tối ưu Agent, kết nối ngoài và tri thức Drive

Cập nhật: 2026-11-13. Bản này thay thế kế hoạch cũ và các phiên bản trong hội thoại. Đây là đặc tả cần triển khai/kiểm chứng, không phải báo cáo đã hoàn thành. Không dùng dấu tích của checklist cũ thay bằng chứng nghiệm thu.

## 1. Mục tiêu và quyết định đã chốt

- Phạm vi: Gmail, Calendar, Drive, Docs, Sheets, Slides, ERP/CRM Odoo hiện có và lớp kết nối MCP dùng chung. Tasks, Meet, Chat, Contacts ngoài phạm vi.
- Cơ chế chạy ngầm: chào hỏi không đọc Drive; câu hỏi thông tin tự tra cứu tài liệu; dữ liệu nghiệp vụ hiện tại dùng công cụ tương ứng; câu hỏi kết hợp dùng cả hai nguồn.
- Câu trả lời tài liệu phải có nguồn. Phân biệt thiếu bằng chứng, phạm vi chưa lập chỉ mục và lỗi kết nối; không suy đoán khi thiếu dữ liệu.
- Tìm kiếm toàn bộ Drive được cấp quyền, gồm Shared Drive. Chỉ mục thường trực tối đa **3.000 tệp hỗ trợ gần nhất/tài khoản theo modifiedTime desc**, thay mức 5.000 trước đây. Không áp thêm điều kiện 24 tháng.
- Đồng bộ thay đổi mỗi 5 phút; cache quyền tối đa 60 giây, chấp nhận cửa sổ quyền cũ tương ứng. Thu hồi/ngắt kết nối đã phát hiện có hiệu lực ngay.
- Nghiệm thu 25 người dùng đồng thời, tăng lên 50; bảo toàn thay đổi Git ngoài phạm vi.

## 2. Các giai đoạn triển khai

### Giai đoạn 1 — Đường thực thi và baseline

- Kiểm kê Agent→Gateway→Google/Odoo/MCP, gồm đồng bộ lịch, đọc Drive và callback; lập bảng giao thức, tham số, quyền, phân trang, schema kết quả.
- Nối worker vào adapter thật, loại bỏ nhánh trả thành công bằng dữ liệu mẫu. Không coi file/migration đã tồn tại là bằng chứng tích hợp hoàn tất.
- Gắn request_id, job_id, operation_id; đo riêng phân loại, hàng đợi, refresh, request ngoài, truy xuất nội bộ, kiểm tra quyền và tổng hợp LLM.
- Tạo fixture và bộ câu hỏi để so sánh trước/sau trên cùng dữ liệu, model, môi trường; giữ schema thành công hiện có.

### Giai đoạn 2 — PostgreSQL, lease và điều phối bền vững

- PostgreSQL 16, SQLAlchemy, Psycopg 3, Alembic cho production; SQLite chỉ cho kiểm thử và cấu hình cục bộ một tiến trình.
- Một Agent, một bộ nhận/gửi Telegram, một scheduler, hai worker tương tác độc lập và một drive-indexer riêng. Không tạo lặp worker trong từng tiến trình web.
- Nhận tác vụ bằng khóa hàng; lease 60 giây, heartbeat 15 giây. Đóng transaction trước khi gọi dịch vụ ngoài.
- **Giới hạn mỗi người dùng: 4 đọc / 1 ghi trên toàn bộ tiến trình**, chỉ áp dụng cho request mạng Google/Odoo/MCP. Không còn giới hạn 2 đọc trong đặc tả này.
- Truy vấn PostgreSQL/pgvector/FTS không tiêu thụ lease mạng; có pool database và deadline riêng.
- Mỗi worker tương tác tối đa 8 request ngoài; toàn hệ thống tối đa 16. Indexer tối đa 2 request ngoài, tối đa 1/tài khoản, dùng chung trần toàn hệ thống/người dùng; ưu tiên tương tác trước đồng bộ nền.
- Cấp lease ở request ngoài thực tế. Nhánh cha không giữ lease khi chờ nhánh con; nhả lease khi backoff để tránh khóa lồng và chiếm slot vô ích.
- Lưu tác vụ trước thực thi. Sau tối đa 2 giây chờ trả mã tác vụ; tiến độ tiếp chỉ sau 30 giây nếu còn chạy.
- Deadline tổng 120 giây cho thao tác thường, 300 giây cho đọc nhiều tài liệu. Đồng bộ toàn Drive chia thành tác vụ có checkpoint, không áp deadline này cho cả lần quét.
- Callback/outbox bền vững tiếp tục tổng hợp ở Agent, lưu lịch sử rồi giao kết quả; job_id chống xử lý trùng. Xử lý riêng mất phản hồi sau gửi Telegram, không hứa giao đúng một lần tuyệt đối.
- Đọc mất worker được nhận lại trong deadline gốc. Ghi đã gửi phải đối soát; hủy chỉ dừng bước chưa gửi.

### Giai đoạn 3 — Pool kết nối, OAuth và MCP/REST

- HTTP client theo vòng đời tiến trình, đóng khi shutdown; Authorization riêng từng request. HTTP/2 khi endpoint hỗ trợ, giữ tương thích HTTP/1.1.
- Connect timeout 3 giây, pool timeout 2 giây; refresh, chờ và retry đều nằm trong ngân sách còn lại.
- Odoo JSON-RPC dùng pool HTTP; XML-RPC dùng executor giới hạn, transport riêng từng tác vụ.
- Refresh sớm trước hạn 5 phút cho tài khoản hoạt động trong 30 phút gần nhất; lease PostgreSQL và connection_version ngăn refresh trùng/ghi token cũ sau ngắt liên kết. 401 buộc refresh.
- Phân loại xác thực, thiếu scope, quyền tài nguyên, quota (kể cả 403), dịch vụ chưa bật, tham số sai, lỗi mạng, xung đột và kết quả chưa rõ.
- Hoàn thiện lifecycle MCP, tools/list, JSON-RPC, SSE và session theo tài khoản/connection_version. Chỉ chọn mapping đã xác minh schema/ngữ nghĩa; yêu cầu xác nhận MCP đi qua luồng xác nhận hiện có.
- REST cho tải/xuất tệp và chức năng chưa tương đương. Cursor chứa giao thức; không dùng token chéo. Fallback đọc chỉ khi tương đương quyền/ngữ nghĩa, không vượt lỗi chính sách.
- Đọc tối đa 3 lần gọi tổng cộng; backoff 0,5 rồi 1 giây kèm jitter và Retry-After. Circuit breaker theo tài khoản/dịch vụ: 5 lỗi hạ tầng liên tiếp, nghỉ 30 giây rồi thăm dò. Không hedging đợt đầu.
- Ghi dùng operation_id, payload hash, phiên bản và thời điểm gửi. Timeout không rõ kết quả chuyển unknown_result; không tự phát lại nếu chưa chứng minh chưa thực hiện hoặc có idempotency tương ứng.

### Giai đoạn 4 — Chỉ mục Drive và quyền truy cập

#### Quét đầu Shared Drive và giới hạn thường trực

- Liệt kê My Drive và từng Shared Drive có phân trang theo **modifiedTime desc**, hợp nhất ưu tiên và loại trùng ID. Chỉ tải/embedding **3.000 tệp hỗ trợ gần nhất/tài khoản**; đây là trần thường trực, không chỉ trần quét đầu.
- Lưu checkpoint và mốc thay đổi trước quét để xử lý biến động phát sinh. Đồng bộ tăng dần 5 phút; đối soát metadata hằng ngày để duy trì tập tệp và bổ sung vị trí trống.
- Dùng ngay phần đã xử lý; hiển thị phạm vi giới hạn, không nói đã đọc toàn bộ Drive. Không cam kết sẵn sàng sau 1–2 phút khi chưa đo.
- Tệp ngoài chỉ mục được tìm/tải trực tiếp tối đa 4 tệp/lượt; kết quả tạm tối đa 24 giờ, không tự mở rộng chỉ mục thường trực.

#### Cách ly CPU-bound

- Giải nén, parse PDF/DOCX/XLSX/PPTX, chunking và chuẩn hóa từ khóa chạy qua **một ProcessPoolExecutor tối đa 2 tiến trình con tại drive-indexer riêng**. Không tạo pool tại mỗi worker tương tác và không parse trên asyncio event loop.
- Đọc bổ sung theo câu hỏi cũng gửi parse vào pool này, ưu tiên trước việc nền.
- Queue parse tối đa 8 tệp; đầu vào 8 MiB/tệp, giải nén tối đa 64 MiB, parse tối đa 30 giây/tệp. Phải dừng xử lý quá hạn và thay process bị kẹt; timeout Future đơn thuần không đủ.
- Hỗ trợ Google Docs/Sheets/Slides, PDF có văn bản, DOCX/XLSX/PPTX/CSV/TXT/Markdown; OCR ngoài đợt đầu. Tệp lỗi/bỏ qua có lý do, không làm hỏng cả lượt đồng bộ.

#### Lưu trữ và truy xuất

- PostgreSQL lưu metadata, encrypted_content, tsvector riêng và vector pgvector. Chuẩn hóa tiếng Việt tại worker; INSERT/UPDATE tạo `to_tsvector('simple', :normalized_text)` từ tham số văn bản, không từ ciphertext và không dùng generated column trên ciphertext.
- Không lưu cột nội dung plaintext; không log tham số SQL/nội dung. FTS chứa từ vựng đọc được nên FTS/vector phải được bảo vệ bằng quyền database và mã hóa volume như dữ liệu nhạy cảm.
- Khóa nguồn gồm user_id, account_id, connection_version, file_id. Lọc chủ sở hữu trước chọn ứng viên; không chia sẻ dữ liệu giữa tài khoản.
- Lấy tối đa 20 ứng viên mỗi nhánh từ khóa/vector, hợp nhất 8 đoạn từ tối đa 4 tài liệu; giữ nguồn tới trang/sheet/slide khi có.

#### Kiểm tra quyền trước khi trả lời

- Cache theo **(user_id, account_id, connection_version, file_id)**; TTL tuyệt đối **60 giây** từ lần kiểm tra Google thành công; cache hit không kéo dài TTL.
- Cache chỉ hợp lệ khi đồng bộ thay đổi thành công gần nhất không quá 10 phút và không có lỗi quyền/xác thực chưa xử lý.
- Cache miss: tối đa **4 files.get song song** qua HTTP client dùng chung, HTTP/2 khi hỗ trợ, với `fields="id,version,trashed,capabilities(canDownload)"`. **Không dùng capabilities/canRead: trường này không tồn tại.**
- Metadata truy cập được chưa đủ chứng minh quyền tải nội dung; áp dụng capability phù hợp và xử lý lỗi download/export thực tế.
- Phiên bản mới phải tải/parse lại trước khi dùng. 403/404, ngắt kết nối hoặc sự kiện thu hồi vô hiệu hóa dữ liệu/cache liên quan ngay.
- Cache hết hạn hoặc đồng bộ không khỏe: kiểm tra trực tiếp; thất bại thì không dùng bản lưu trả lời.
- Thiếu bằng chứng được fallback tìm trực tiếp Drive một lần/lượt. Nêu giới hạn/kết quả chưa đầy đủ, không suy ra toàn Drive không có thông tin từ một lượt tìm thiếu.

### Giai đoạn 5 — DAG, timeout và cache công cụ

- Mở rộng phân loại sang danh sách lời gọi có phụ thuộc, giữ tương thích một công cụ; xác thực DAG không chu trình và đủ tham chiếu.
- Chạy tối đa 4 nhánh đọc độc lập/lượt, chịu lease Giai đoạn 2. Dùng parallel tool calling khi provider hỗ trợ; executor vẫn kiểm tra quyền/tham số/phụ thuộc.
- **step_timeout = 30 giây**, gồm chờ lease, refresh và retry, luôn nằm trong deadline tổng **120 giây** của tác vụ thường. Giữ 10 giây cuối cho tổng hợp/giao trạng thái; không bắt đầu bước khi hết ngân sách thực thi.
- Nhóm đọc tài liệu có deadline tổng 300 giây, chia tìm/tải/parse/tổng hợp thành các bước giới hạn riêng. Không reset deadline khi retry/đổi giao thức.
- Trả **partial response** cho nhánh độc lập đã hoàn thành. Nhánh phụ thuộc thiếu dữ liệu bị bỏ qua có lý do; không ghi khi thiếu đầu vào. Ghi đã gửi timeout chuyển unknown_result và tiếp tục đối soát.
- Nạp 3–6 schema theo ý định, tối đa 12 khi đa hệ thống; mở rộng một lần nếu thiếu công cụ.
- Công cụ nghiệp vụ đợt đầu: tìm–đọc tài liệu, tổng hợp lịch theo khoảng ngày, tra đối tác Odoo chuẩn bị nháp email. Trùng đối tác phải hỏi lại; giữ xác nhận ghi.
- Pruning theo schema công cụ, giữ nguồn/phiên bản/phân trang/cảnh báo thiếu dữ liệu. Không cache câu trả lời LLM đợt đầu.
- Cache đọc 30 giây theo người dùng, tài khoản/kết nối, phiên, công cụ và args hash; tách khỏi cache quyền 60 giây.
- Phiên bản invalidation theo **user_id + account/connection + resource_group**, không dùng biến toàn cục. Ghi thành công tăng phiên bản cùng transaction lưu kết quả; unknown_result cũng vô hiệu hóa nhóm liên quan.
- Ghi phải đọc lại trạng thái/phiên bản cần thiết trước gửi. Prefetch chỉ lấy dữ liệu nội bộ nhẹ như múi giờ, phiên và trạng thái kết nối.

## 3. API, trạng thái và lưu giữ

- Hoàn thiện tạo/xem/hủy tại `/internal/v1/users/{user_id}/workspace/jobs`; thêm API tương đương cho Odoo dùng chung bộ điều phối.
- Cùng request_id/payload trả cùng tác vụ; khác payload trả 409. Ghi tiếp tục cần operation_id/payload hash.
- Trạng thái queued, running, waiting_confirmation, completed, failed, unknown_result, cancelled, expired. Tác vụ nhiều nhánh có kết quả từng bước và partial.
- Thêm provider, request_id, job_id, error_code, retryable, next_action; giữ schema thành công tương thích.
- API đồng bộ hiển thị số tệp, trần 3.000, lỗi/bỏ qua, lần đồng bộ thành công và mức phủ giới hạn; hỗ trợ yêu cầu đồng bộ lại.
- Callback nội bộ xác thực, nhận lặp an toàn; kiểm tra lại chủ sở hữu, connection_version và liên kết Telegram trước giao kết quả.
- Nội dung tác vụ/kết quả tạm mã hóa giữ 24 giờ; metadata/chống lặp giữ 30 ngày. Ngắt kết nối chặn truy cập ngay, xóa nội dung/FTS/vector/cache liên quan trong 24 giờ.

## 4. Kiểm thử, nghiệm thu và chuyển đổi

### Kiểm thử bắt buộc

- Chào hỏi, câu hỏi không link, đa nguồn, thiếu bằng chứng, câu hỏi tiếp nối và chỉ dẫn độc hại trong tài liệu.
- Ít nhất 60 câu chuẩn tiếng Việt có/không dấu, mã nghiệp vụ, nhiều tài liệu/mâu thuẫn; tài liệu đúng trong top 8 >=90%; khẳng định dựa trên tài liệu có nguồn hỗ trợ.
- 4 nhánh mạng cùng chạy khi đủ slot, không vượt 4 đọc/1 ghi/người dùng hoặc 8/worker; truy vấn nội bộ không chiếm lease mạng; không khóa lồng/deadlock.
- Cache quyền sát mốc 60 giây, cache hit không gia hạn, thu hồi đã biết, đồng bộ trễ và cách ly tài khoản; không gửi field canRead.
- Hơn 3.000 tệp, Shared Drive lớn, phân trang, thay đổi lúc quét, fallback tệp cũ và không embedding lặp.
- PDF/Office nặng không chặn heartbeat/event loop; parse quá hạn dừng thật; không có cột plaintext và FTS tìm được với nội dung lưu mã hóa riêng.
- DAG bước 30 giây, deadline tổng, partial response, nhánh ghi phụ thuộc không chạy khi thiếu dữ liệu; invalidation đúng người/nhóm.
- Refresh đồng thời, 401/403/429/5xx, SSE đứt, cursor khác giao thức, worker chết trước/sau gửi, callback/xác nhận lặp, ID trùng khác payload, mất liên kết Telegram.
- Dữ liệu thử riêng cho Workspace và Odoo; Gmail đọc/tạo/sửa/xóa nháp, gửi thư mô phỏng. Đi qua Agent/Gateway thực tế, không chỉ adapter.

### Hiệu năng

- 25 người dùng trong 30 phút, đột biến 50 trong 5 phút, đồng thời đồng bộ nền.
- p95 nhận tác vụ <=3 giây, thành công >=99% khi dịch vụ mô phỏng bình thường; không ghi trùng trong kiểm thử, không rò dữ liệu/deadlock.
- p95 truy xuất chỉ mục nội bộ <=1 giây; đo riêng quyền ngoài và tổng trả lời. Mục tiêu giảm >=30% p95 trả lời so baseline cùng fixture/model.
- Báo cáo riêng Google/Odoo thật, quota, cache hit/miss, chi phí embedding, CPU/RAM, thời gian chỉ mục và tiêu chí chưa đạt; không coi ước lượng là kết quả nghiệm thu.

### Triển khai và quay lui

- Nghiệm thu từng giai đoạn trên môi trường thử, bật từng nhóm chức năng cho tài khoản thử.
- Di chuyển bản sao SQLite, đối chiếu ID/số dòng/quan hệ/sequence/giải mã. Chuyển thật sau khi dừng nhận việc mới, ghi nhận tác vụ đang chạy và sao lưu.
- Dựng lại chỉ mục Drive có kiểm soát sở hữu; không nhập nguyên chỉ mục cũ chưa xác minh quyền.
- Tắt riêng cache/song song/indexer hoặc chuyển ứng dụng về REST khi cần; giữ PostgreSQL và sổ chống lặp. Quay SQLite chỉ sau đồng bộ ngược và đối chiếu tác vụ đã gửi.
- Bàn giao báo cáo kiểm thử, bảng MCP/REST thực tế, cấu hình và hướng dẫn chẩn đoán. Không đánh dấu hoàn thành khi chưa có bằng chứng tích hợp.
