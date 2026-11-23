# Kế hoạch Triển khai Giai đoạn 4: Chỉ mục Drive và Quyền Truy cập

> **Tài liệu đặc tả gốc:** `docs/superpowers/plans/2026-11-13-google-workspace-multiuser-optimization.md` (Mục 2 – Giai đoạn 4)
> **A1 đã hoàn tất theo trạng thái bàn giao. Điểm bắt đầu tiếp theo là A2.**
> **Revision A1 trong mã nguồn:** `d1e2f3a4b5c6`, kế tiếp `c7d8e9f0a1b2`.
> **Docker image hiện tại:** `postgres:16` (cần đổi)

---

## 0. Nguyên tắc & Ràng buộc

1. **Trần thường trực 3.000 tệp hỗ trợ/tài khoản** theo `modifiedTime desc`, không cắt theo tuổi tệp. Chọn tập tệp trước khi tải hoặc embedding.
2. **Cách ly CPU-bound**: Parse chạy trong tiến trình con `multiprocessing` do `drive-indexer` giám sát, tối đa 2 tiến trình. Tuyệt đối không parse trên asyncio event loop.
3. **Mã hóa nội dung đoạn bằng Fernet.** Tạo `tsvector` từ tham số đã chuẩn hóa. Bảo vệ FTS/vector bằng quyền database và mã hóa volume. Tắt log nội dung và tham số SQL nhạy cảm.
4. **Khóa liên kết Drive** là `(user_id, google_sub, link_version)` cho chỉ mục, checkpoint, cache quyền, tác vụ và kết quả tạm. `google_sub` định danh tài khoản Google. `link_version` tăng khi relink/disconnect. Refresh thông thường chỉ tăng `connection_version`, giữ nguyên cơ chế bảo vệ OAuth/MCP của Giai đoạn 3.
5. **Cache quyền 60 giây** tính từ lần Google xác nhận thành công. Chỉ dùng khi đồng bộ thành công trong 10 phút gần nhất và phạm vi tương ứng không có lỗi quyền/xác thực chưa xử lý. Thu hồi đã phát hiện phải có hiệu lực trên mọi worker.
6. **Tải tệp qua Gateway**, dùng lại OAuth, HTTP client và giới hạn mạng. Tiến trình con chỉ giải nén, parse, chia đoạn và chuẩn hóa từ khóa.
7. **Checkpoint theo phạm vi**: Mỗi Drive scope (My Drive, từng Shared Drive) có checkpoint riêng. Lấy mốc thay đổi trước khi quét, chỉ tiến checkpoint sau khi đã lưu kết quả.

---

## Thứ tự thực hiện

```
Phase A: Hạ tầng & Khóa liên kết
  A1  Tách link_version khỏi connection_version [đã hoàn tất]
  A2  Image pgvector + chốt embedding model + migration schema

Phase B: Luồng một tệp (vertical slice)
  B1  Tải một tệp qua Gateway
  B2  Parse trong tiến trình con (multiprocessing, kill/replace)
  B3  Chuẩn hóa tiếng Việt → mã hóa → tsvector + embedding → INSERT
  B4  Hybrid search FTS + pgvector → top 8 chunks từ ≤4 tài liệu
  B5  Permission gate: cache 60s + kiểm link_version + loại chunk lỗi
  B6  Endpoint Gateway + Agent gọi → trả lời có nguồn

Phase C: Quét & Đồng bộ Shared Drive
  C1  Quét đầu: My Drive + từng Shared Drive, phân trang, checkpoint
  C2  Đồng bộ tăng dần Changes API 5 phút
  C3  Giữ trần 3.000 tệp + đối soát metadata hằng ngày

Phase D: Fallback & Đánh giá
  D1  Fallback tìm trực tiếp khi thiếu bằng chứng
  D2  Bộ 60 câu tiếng Việt
  D3  Đổi liên kết khi đang chờ, đang xử lý hoặc chưa giao kết quả
  D4  Hồi quy toàn bộ
```

---

## Điều phối thực thi bằng `/teamwork`

Mở workspace `D:\ODOO` chứa cả `Auth_gateway` và `Module AI - Agent`. A1 đã hoàn tất theo bàn giao. Dùng mã và migration A1 đang có trong workspace làm đầu vào, kể cả các thay đổi chưa commit. A2 là task triển khai đầu tiên.

### Đội hình và quyền sở hữu

Đường dẫn trong bảng tính từ `Auth_gateway`, trừ các đường dẫn ghi rõ thuộc repo Agent. Tên file đứng sau một đường dẫn đầy đủ trong cùng danh sách dùng chung thư mục với đường dẫn đó. Tên file mới theo các task bên dưới.

| Vai trò | Task và file phụ trách | Đầu ra bàn giao |
|---|---|---|
| **Backend Tech Lead (Agent–Gateway)** | **B6, D1**. Gateway `app/workspace/routes.py`, `worker.py`, `worker_main.py`, `drive_fallback.py`. Repo Agent `app/services/gateway_client.py`, `unified_executor.py`, `app/api/v1/internal.py` và `app/knowledge/drive.py` | Trình thiết kế để người dùng duyệt, phân xử thay đổi ở file dùng chung, nối parser và scheduler vào điểm thực thi, tích hợp luồng `knowledge.search` và callback. Bàn giao câu trả lời có nguồn, trạng thái thiếu bằng chứng và fallback đã tích hợp |
| **Search Infrastructure Engineer (PostgreSQL/pgvector)** | **A2, B3, B4**. `compose.postgres.yaml`, migration A2, `app/workspace/models.py`, `vn_normalizer.py`, `drive_storage.py`, `drive_retriever.py` và kiểm thử tương ứng | Chuỗi migration từ A1, dữ liệu đoạn mã hóa và truy xuất FTS/vector. Bàn giao giao diện lưu theo khóa liên kết và phiên bản tệp, cùng danh sách ứng viên chưa giải mã cho bước kiểm quyền |
| **Backend Integration Engineer (Google Drive API)** | **B1, B2, C1–C3**. `app/workspace/drive_knowledge.py`, `drive_downloader.py`, `parsers/`, `drive_parse_pool.py`, `drive_scanner.py`, `drive_sync.py`, `drive_cap.py` và kiểm thử tương ứng | Tái sử dụng đường gọi REST qua WorkspaceService. Bàn giao tải kèm phiên bản tệp, parser có giới hạn, quét/Changes với checkpoint và chọn top 3.000 trước khi xếp hàng xử lý. Gửi phần nối worker cho vai trò tích hợp |
| **Application Security Engineer (OAuth và kiểm soát truy cập)** | **B5, D3**. `app/workspace/drive_permissions.py` và kiểm thử quyền, cache, phiên bản và đổi liên kết. Đối chiếu mã A1 trong `app/models.py` và `app/google/oauth.py` | Bàn giao hợp đồng kiểm quyền và vô hiệu hóa cho truy xuất, đồng bộ và fallback. Xác minh chặn liên kết cũ khi ghi chỉ mục, chuyển nội dung sang LLM hoặc giao kết quả, và dọn dữ liệu theo kế hoạch |
| **QA Automation Engineer (kiểm thử tích hợp và hiệu năng)** | **D2, D4**. Fixture và bài đánh giá trong `tests/evaluation/`, kịch bản tích hợp hai repo và kết quả đo. Review A2–D3 theo từng mốc | Bàn giao bộ 60 câu có nhãn và báo cáo D4. Gửi lỗi kèm cách tái hiện cho người phụ trách, chạy lại sau khi sửa và ghi rõ ca fail, skip hoặc chưa chạy |

### Duyệt thiết kế và bàn giao

Người điều phối dùng đội hình trên để trình phạm vi thay đổi, schema và migration, giao diện giữa các module, thứ tự tích hợp và tiêu chí nghiệm thu. Chờ người dùng duyệt rõ ràng trước khi sửa mã, cấu hình hoặc áp dụng migration. Trong mã hiện có, Gateway đọc Drive qua `drive_documents` và xử lý tác vụ `index.fetch`/`knowledge.search`. Agent đã có đường gọi Gateway và callback cho `knowledge.search`. Migration A2 dự kiến `e2f3a4b5c6d7_stage4_drive_knowledge_schema.py` hiện là file rỗng, cần hoàn thiện dưới A2 và nối tiếp revision A1.

Khi bàn giao mỗi task, ghi ID task, file đã sửa, giao diện liên quan, lệnh kiểm tra, kết quả thực tế và phần còn chờ. Người điều phối sở hữu các điểm nối dùng chung và nhận thay đổi đề xuất từ các vai trò khác. Trình người dùng các thay đổi thiết kế vượt phạm vi đã duyệt. Chuyển mốc khi các tiêu chí bắt buộc của mốc đó đạt.

### Thứ tự triển khai sau khi duyệt

| Mốc | Điều kiện bắt đầu | Phạm vi triển khai | Đầu ra bàn giao |
|---|---|---|---|
| **A2** | Thiết kế đã được người dùng duyệt và có revision A1 `d1e2f3a4b5c6` | Docker Compose, model và migration A2 của Gateway | PostgreSQL 16 + pgvector, schema và kết quả kiểm tra nâng/hạ migration |
| **B1 → B6** | A2 đạt nghiệm thu | Triển khai tuần tự từ tải tệp đến tích hợp `knowledge.search` ở hai repo | Luồng một tệp chạy qua Agent, Gateway và callback, trả lời có nguồn sau khi kiểm quyền và phiên bản |
| **C1–C3** | B6 đạt nghiệm thu và hợp đồng dùng chung đã được chốt | Quét, đồng bộ Changes, giữ trần và đối soát. Có thể chuẩn bị bộ dữ liệu có nhãn của D2 trong lúc làm C | Quét và đồng bộ dùng chung checkpoint, quy tắc chọn tệp và cơ chế vô hiệu hóa quyền |
| **D1–D3** | C1–C3 đã tích hợp và qua nghiệm thu | Tích hợp fallback, chạy bộ đánh giá D2, kiểm tra đổi liên kết ở D3 | Fallback dùng chung parser và kiểm quyền, kết quả bộ 60 câu và các ca đổi liên kết |
| **D4** | D1–D3 đã tích hợp | Nghiệm thu trên bản tích hợp của cả hai repo | Báo cáo hồi quy, PostgreSQL/pgvector, luồng thực tế, độ đúng, tải và số liệu độ trễ |

**Hợp đồng dùng chung cho C1–C3**

- Thống nhất metadata có khóa `(user_id, google_sub, link_version, file_id)`, phiên bản tệp, phạm vi và thời gian sửa. C1 sở hữu cursor quét cùng thao tác lưu mốc Changes ban đầu. C2 sở hữu việc tiến cursor Changes sau khi phạm vi quét sẵn sàng.
- C3 sở hữu quyết định tệp nào thuộc top 3.000. C1, C2 và đối soát dùng chung quyết định này trước khi xếp hàng tải hoặc embedding. C2 dùng hợp đồng vô hiệu hóa quyền của B5.
- Tích hợp quy tắc chọn tệp của C3 trước khi nối C1 và C2 vào worker. Kiểm tra C1 tiếp tục được từ checkpoint, rồi chạy C2 trên phạm vi đã quét để xác nhận thay đổi phát sinh trong lúc quét được xử lý.

D2 dùng fixture có nhãn để chuẩn bị trước và chạy đánh giá nghiệm thu sau khi tích hợp D1. Chốt ngưỡng fallback bằng dữ liệu D2. Kiểm tra migration trên PostgreSQL/pgvector thử nghiệm theo A2 và D4.

---

## Phase A: Hạ tầng & Khóa liên kết

### Task A1 — Tách `link_version` khỏi `connection_version`

**Trạng thái** — Đã hoàn tất theo bàn giao. Migration `d1e2f3a4b5c6_add_link_version_to_google_connections.py` và `tests/integration/test_link_version_separation.py` đã có trong workspace. Các điểm kiểm tra khóa trên luồng Drive được nối khi xây dựng luồng tương ứng và nghiệm thu đầu cuối ở D3.

**Vấn đề**: `connection_version` hiện tăng cả khi refresh thành công (`oauth_lease.py:214`) lẫn khi relink (`oauth.py:250`). Nếu dùng làm khóa chỉ mục, refresh định kỳ sẽ vô hiệu tài liệu vừa lập chỉ mục.

**Thay đổi**:

| File | Sửa đổi |
|---|---|
| `app/models.py` | Thêm cột `link_version = Column(Integer, nullable=False, default=1)` vào `GoogleConnection` |
| `app/google/oauth.py` | `handle_callback` khi relink (existing): tăng **cả** `link_version` và `connection_version`. Disconnect: tăng `link_version` |
| `app/auth/oauth_lease.py` | Refresh thành công (dòng 214): chỉ tăng `connection_version`, **KHÔNG** tăng `link_version` |
| `alembic/versions/d1e2f3a4b5c6_add_link_version_to_google_connections.py` | Migration thêm cột `link_version` sau revision `c7d8e9f0a1b2` |

Lưu khóa liên kết lúc nhận tác vụ. Xác minh liên kết còn hoạt động và khớp khóa trước khi xử lý, ghi chỉ mục/checkpoint, chuyển nội dung sang LLM và giao kết quả/callback. Kiểm tra và ghi trạng thái liên quan trong transaction ngắn để kết quả cũ không ghi đè liên kết mới. Khi relink hoặc disconnect, chặn truy cập ngay và dọn nội dung, FTS, vector, cache và kết quả tạm của liên kết cũ trong 24 giờ.

**Nghiệm thu**:
- [ ] Ca 1: Refresh token thường → `link_version` giữ nguyên, `connection_version` tăng
- [ ] Ca 2: Relink (gọi `handle_callback` với existing connection) → `link_version` tăng
- [ ] Ca 3: Disconnect → `link_version` tăng
- [ ] Hồi quy Gateway và kiểm tra refresh chạy đồng thời với relink/disconnect

---

### Task A2 — Image pgvector + Embedding model + Migration schema Drive

**Cấu hình khởi điểm**

| Quyết định | Giá trị đề xuất | Lý do |
|---|---|---|
| Docker image | `pgvector/pgvector:pg16` | Drop-in thay `postgres:16`, hỗ trợ `CREATE EXTENSION vector` |
| Extension | `CREATE EXTENSION IF NOT EXISTS vector;` trong migration | Bật pgvector |
| Embedding model | `text-embedding-3-small` (OpenAI) | Kiểm tra chất lượng tiếng Việt trong D2 |
| Số chiều vector | `1536` | Tương ứng `text-embedding-3-small` |

**Thay đổi**:

| File | Sửa đổi |
|---|---|
| `compose.postgres.yaml` | `image: pgvector/pgvector:pg16` thay `postgres:16` |
| `app/workspace/models.py` | Thêm model `DriveFileMetadata`, `DriveFileChunk`, `DriveSyncCheckpoint` |
| `alembic/versions/e2f3a4b5c6d7_stage4_drive_knowledge_schema.py` | Hoàn thiện file migration A2 hiện có, nối tiếp `d1e2f3a4b5c6`, tạo bảng, `CREATE EXTENSION vector` và index composite |

**Schema `drive_file_metadata`**:
```
user_id       INT FK → users.id
google_sub    VARCHAR(255)
link_version  INT               ← khóa chỉ mục ổn định
file_id       VARCHAR(255)
name          TEXT
mime_type     VARCHAR(255)
drive_id      VARCHAR(255)      ← NULL = phạm vi tệp ngoài Shared Drive
version       VARCHAR(64)      ← phiên bản Drive mới nhất đã quan sát
modified_time DATETIME
indexed_at    DATETIME
status        VARCHAR(32)      ← 'indexed', 'pending', 'skipped', 'error'
status_reason TEXT
PK: (user_id, google_sub, link_version, file_id)
```

**Schema `drive_file_chunks`**:
```
id                 SERIAL PK
user_id            INT FK
google_sub         VARCHAR(255)
file_id            VARCHAR(255)
link_version       INT
file_version       VARCHAR(64)   ← phiên bản nguồn thực sự được parse
chunk_index        INT
page_or_section    VARCHAR(128)
encrypted_content  TEXT          ← Fernet(plaintext)
tsv                TSVECTOR     ← to_tsvector('simple', :normalized)
embedding          VECTOR(1536)
created_at         DATETIME
FK: (user_id, google_sub, link_version, file_id) → drive_file_metadata ON DELETE CASCADE
UNIQUE: (user_id, google_sub, link_version, file_id, chunk_index)
```

**Schema `drive_sync_checkpoints`**:
```
user_id            INT FK
google_sub         VARCHAR(255)
link_version       INT
scope              VARCHAR(255)  ← 'my_drive' hoặc drive_id
scan_page_token    TEXT          ← cursor files.list, chỉ dùng khi đang quét
start_page_token   TEXT          ← mốc Changes lấy trước quét hoặc cursor đồng bộ
scan_state         VARCHAR(32)   ← tiến độ quét, gồm trạng thái đã hoàn tất
last_error_code    VARCHAR(64)   ← lỗi quyền/xác thực chưa giải quyết nếu có
last_full_scan_at  DATETIME
last_sync_at       DATETIME
last_success_at    DATETIME      ← dùng cho điều kiện cache 10 phút
PK: (user_id, google_sub, link_version, scope)
```

**Nghiệm thu**:
- [ ] `docker compose -f compose.yaml -f compose.postgres.yaml up -d postgres` khởi động thành công với image mới trong môi trường thử
- [ ] `SELECT * FROM pg_extension WHERE extname = 'vector'` trả kết quả
- [ ] Nâng cấp từ revision A1 `d1e2f3a4b5c6` lên A2 và kiểm tra cả chuỗi từ bản sao Giai đoạn 3 ở `c7d8e9f0a1b2`
- [ ] Hạ từ A2 về `d1e2f3a4b5c6` xóa bảng Drive và giữ dữ liệu Giai đoạn 3 cùng `link_version`
- [ ] SQLite chạy các bài kiểm tra logic. Kiểm tra FTS, pgvector, migration và cách ly liên kết chạy trên PostgreSQL 16 thật

---

## Phase B: Luồng một tệp (Vertical Slice)

### Task B1 — Tải một tệp qua Gateway

Tải file qua `WorkspaceService` hiện có với OAuth, pool kết nối, giới hạn mạng và circuit breaker. Dùng REST cho tải/xuất tệp, áp giới hạn 8 MiB trong lúc đọc stream và dùng deadline còn lại. Ghi nhận phiên bản nguồn và xác minh phiên bản vẫn khớp trước khi xuất bản chunks nếu tệp có thể đã đổi trong lúc tải/parse.

| File | Sửa đổi |
|---|---|
| `app/workspace/drive_downloader.py` | Tải theo khóa liên kết và file ID, trả bytes kèm phiên bản nguồn, qua `WorkspaceService.read()` với `force_rest=True` |
| `tests/unit/test_drive_downloader.py` | Mock `WorkspaceService`, test Google Docs export, file binary, lỗi 403/404 |

**Nghiệm thu**:
- [ ] Google Docs/Sheets/Slides export sang docx/xlsx/pptx
- [ ] File nhị phân (PDF, DOCX) trả raw bytes
- [ ] 403 → `PERMISSION_DENIED`, 404 → `NOT_FOUND`, không crash

---

### Task B2 — Parse trong tiến trình con (multiprocessing)

**Thiết kế**:
- `drive-indexer` dùng `multiprocessing.Process` trên Python 3.11, tối đa 2 tiến trình parse và hàng đợi tối đa 8 tệp. Đọc theo câu hỏi được ưu tiên trước việc nền trong cùng giới hạn.
- Mỗi tiến trình có kênh gửi/nhận riêng. Supervisor theo dõi bất đồng bộ để chờ I/O hoặc dọn tiến trình không chặn event loop.
- Đủ 30 giây thì hết hạn tác vụ parse và gửi `terminate()`. Cho tối đa 5 giây để dọn tiến trình, sau đó `kill()` nếu cần. Thu hồi tiến trình và bỏ kênh cũ trước khi tạo tiến trình và kênh mới.
- Tiến trình con giải nén, parse, chia đoạn và chuẩn hóa từ khóa. Gateway xử lý kết nối mạng.

| File | Sửa đổi |
|---|---|
| `app/workspace/parsers/__init__.py` | Registry: mime → parser function |
| `app/workspace/parsers/google_docs.py` | Parse DOCX (python-docx) |
| `app/workspace/parsers/google_sheets.py` | Parse XLSX (openpyxl) |
| `app/workspace/parsers/google_slides.py` | Parse PPTX, giữ số slide làm nguồn |
| `app/workspace/parsers/pdf_parser.py` | Parse PDF text (pdfplumber/PyMuPDF) |
| `app/workspace/parsers/plaintext.py` | Parse TXT/MD/CSV |
| `app/workspace/drive_parse_pool.py` | `ParsePool`: spawn/monitor/kill 2 processes, queue input/output, timeout 30s |
| `app/workspace/worker_main.py` | `--drive-indexer` khởi tạo `ParsePool` |
| `tests/unit/test_drive_parse_pool.py` | Ca bình thường + ca parser treo (sleep 60s) → verify kill + tệp tiếp theo xử lý được |

**Giới hạn đầu vào**: 8 MiB/tệp, 64 MiB giải nén

**Nghiệm thu**:
- [ ] Parser treo báo timeout ở 30 giây. Tiến trình chết khi đang trả kết quả được thay cùng kênh riêng, tệp tiếp theo chạy được và heartbeat vẫn đều
- [ ] Hàng đợi không vượt 8 tệp. Tệp vượt 8 MiB đầu vào hoặc 64 MiB giải nén được ghi lý do bỏ qua
- [ ] Không có IO mạng trong tiến trình con
- [ ] Chunking tối đa 800 token, chồng lấn tối đa 80 token trong cùng đơn vị nguồn, giữ trang, mục, sheet hoặc slide

---

### Task B3 — Chuẩn hóa → Mã hóa → tsvector + embedding → INSERT

| File | Sửa đổi |
|---|---|
| `app/workspace/vn_normalizer.py` | Chuẩn hóa tiếng Việt: unicode NFKC, bỏ dấu phụ cho tìm không dấu, lowercase |
| `app/workspace/drive_storage.py` | Lưu theo khóa liên kết, ID và phiên bản tệp. Ghi Fernet ciphertext, `to_tsvector('simple', :normalized)` và vector |
| `tests/unit/test_drive_storage.py` | Test mã hóa/giải mã, test tsvector tạo đúng (PostgreSQL only), test embedding INSERT |

**Embedding** dùng cấu hình riêng `EMBEDDING_MODEL` và `EMBEDDING_API_KEY`, tối đa 20 đoạn/lần gọi và cùng model cho tài liệu lẫn truy vấn. Dùng dữ liệu thử riêng hoặc embedding mô phỏng cho đến khi chốt việc gửi nội dung Drive thực sang nhà cung cấp. Giữ transaction DB đóng khi gọi API ngoài. Thay toàn bộ chunks của tệp và cập nhật trạng thái trong cùng transaction sau khi kiểm tra lại khóa liên kết, phiên bản nguồn và tư cách thuộc chỉ mục. Theo dõi phiên bản model/parser để đồng bộ lại có chủ đích. Bỏ qua embedding khi dữ liệu đầu vào và cấu hình chưa đổi.

**Nghiệm thu**:
- [ ] Cột `encrypted_content` giải mã Fernet trả đúng nội dung gốc
- [ ] Không có cột lưu nguyên văn nội dung đoạn. Tham số SQL và đoạn gốc không xuất hiện trong log
- [ ] Lượt đồng bộ lặp với cùng phiên bản nguồn và cấu hình không gọi embedding lại cho tệp đã lập chỉ mục
- [ ] INSERT + SELECT round-trip trên PostgreSQL thành công
- [ ] Tiếng Việt có dấu và không dấu đều tìm thấy

---

### Task B4 — Hybrid search FTS + pgvector

| File | Sửa đổi |
|---|---|
| `app/workspace/drive_retriever.py` | Tìm ứng viên theo khóa liên kết, trả ID đoạn, metadata và điểm |
| `tests/unit/test_drive_retriever.py` | Test FTS match, test vector similarity, test merge/dedup, test tối đa 8 chunks từ ≤4 docs |

**Thuật toán**:
1. FTS: `ts_rank(tsv, plainto_tsquery('simple', :query))` → top 20
2. Vector: `embedding <=> :query_embedding` (cosine distance) → top 20
3. Merge: RRF (Reciprocal Rank Fusion) hoặc score chuẩn hóa
4. Loại đoạn trùng theo ID đoạn. Giữ tối đa 8 đoạn từ tối đa 4 tài liệu, cho phép nhiều đoạn trong cùng tệp.
5. Chuyển các ứng viên sang B5 để xác minh quyền và phiên bản trước khi giải mã.

Lọc theo `(user_id, google_sub, link_version)` và phiên bản chunk khớp metadata hiện tại ngay trong từng nhánh, trước khi lấy top 20. Dùng cùng chuẩn hóa tiếng Việt cho truy vấn FTS và văn bản đã lập chỉ mục.

**Nghiệm thu**:
- [ ] Truy vấn trả ≤8 chunks từ ≤4 docs
- [ ] Chunks kèm metadata: `file_id`, `name`, `page_or_section`
- [ ] Dữ liệu khác `link_version` không bao giờ trả về

---

### Task B5 — Permission gate + cache 60s

| File | Sửa đổi |
|---|---|
| `app/workspace/drive_permissions.py` | Kiểm tra theo khóa liên kết, ID tệp và phiên bản kỳ vọng. Trả quyền, phiên bản quan sát và lý do loại |
| `tests/unit/test_drive_permissions.py` | Cache hit/miss, TTL 60s, invalidation on 403/404, điều kiện sync 10 phút |

**Thiết kế**:

```python
cache_key = (user_id, google_sub, link_version, file_id)
fields = "id,version,trashed,capabilities(canDownload)"
```

- Cache kết quả xác nhận thành công trong 60 giây, kèm phiên bản nguồn, phạm vi và thời điểm xác nhận. Cache hit không gia hạn TTL. Bỏ qua cache khi đồng bộ chưa thành công trong 10 phút gần nhất hoặc đang có lỗi quyền/xác thực chưa giải quyết.
- Cache miss gọi tối đa 4 `files.get` song song qua pool chung, HTTP/2 khi hỗ trợ. Dùng `supportsAllDrives=True` và tuân thủ lease mạng theo người dùng/worker. Lỗi quota hoặc lỗi mạng loại đoạn khỏi lượt hiện tại và được báo đúng nguyên nhân.
- Chỉ giải mã đoạn khi tài khoản còn hợp lệ, tệp chưa bị đưa vào thùng rác, quyền tải được xác nhận và phiên bản Google trùng phiên bản đã parse. Metadata truy cập được chưa đủ để xác nhận quyền tải. Xử lý lỗi download/export thực tế khi lập chỉ mục hoặc fallback.
- 403 do mất quyền, 404, thay đổi quyền qua Changes API hoặc mất quyền cả Shared Drive vô hiệu hóa cache tương ứng trên mọi worker. Dùng trạng thái vô hiệu hóa dùng chung để kết quả kiểm quyền đang chạy không nạp lại cache đã bị hủy. Khi phiên bản đổi, loại đoạn cũ và xếp hàng parse lại nếu tệp còn thuộc chỉ mục.

**Nghiệm thu**:
- [ ] Cache hit không gia hạn TTL → hết 60s phải gọi lại API
- [ ] Đồng bộ quá 10 phút hoặc có lỗi quyền/xác thực tồn đọng → bỏ qua cache
- [ ] Hai worker quan sát cùng thay đổi quyền. Ca tệp mất quyền, bị trash, đổi phiên bản hoặc check thất bại không đưa đoạn cũ vào LLM
- [ ] Không dùng `capabilities/canRead`

---

### Task B6 — Endpoint Gateway + Agent tích hợp

| File | Sửa đổi |
|---|---|
| `app/workspace/routes.py` | Thêm `/internal/v1/users/{user_id}/workspace/drive/search` |
| `app/workspace/routes.py` | Thêm `/internal/v1/users/{user_id}/workspace/drive/sync-status` |
| Agent và Gateway, luồng `knowledge.search` hiện có | Tái sử dụng xử lý tác vụ nền và callback để trả lời câu hỏi gốc. Dùng chung logic với endpoint nội bộ |
| `tests/integration/test_drive_search_endpoint.py` | Test endpoint round-trip, test nguồn trích dẫn |

**Endpoint `drive/search`**:
- Input: `{ "query": "..." }`. Xác thực caller nội bộ và lấy `user_id` từ path, kiểm tra quyền truy cập tác vụ và liên kết tương ứng.
- Luồng xử lý: chọn ứng viên → xác minh quyền và phiên bản → giải mã đoạn đạt điều kiện → trả đoạn kèm nguồn.
- Output: `{ "chunks": [{"file_id", "name", "page_or_section", "content", "score"}], "sources": [...] }`

**Phân biệt 3 tình huống**:
1. Có bằng chứng → trả chunks + sources
2. Đã tìm trong phạm vi hiện có và thiếu bằng chứng → `"status": "no_evidence"`, kèm giới hạn phạm vi
3. Chưa có chỉ mục khả dụng hoặc lỗi kết nối/quyền làm gián đoạn việc tìm → `"status": "not_indexed"` hoặc `"status": "connection_error"`

`sync-status` hiển thị số tệp, mức trần, lần đồng bộ thành công, trạng thái phạm vi và lý do lỗi/bỏ qua. Luồng vận hành có thể yêu cầu đồng bộ lại qua cơ chế tác vụ hiện có.

**Nghiệm thu**:
- [ ] Agent nhận chunks có nguồn trích dẫn rõ ràng
- [ ] Câu hỏi thông tin không kèm link tự tra cứu Drive. Câu trả lời có căn cứ trích nguồn tương ứng. Thiếu bằng chứng được báo rõ
- [ ] Câu chào hỏi → 0 lần đọc Drive (Agent phân loại intent trước)
- [ ] Phân biệt đúng 3 tình huống trên

---

## Phase C: Quét & Đồng bộ Shared Drive

### Task C1 — Quét đầu (Initial Scan)

| File | Sửa đổi |
|---|---|
| `app/workspace/drive_scanner.py` | `initial_scan(user_id)`: quét My Drive + từng Shared Drive |
| `tests/unit/test_drive_scanner.py` | Phân trang, checkpoint, worker chết giữa trang |

**Thiết kế**:
1. Phân trang `drives.list` để lấy các Shared Drive còn truy cập được. Phạm vi tệp ngoài Shared Drive gồm My Drive và tệp được chia sẻ trực tiếp.
2. Với mỗi scope (My Drive, từng Shared Drive):
   a. Lưu `changes.getStartPageToken` trước khi quét. Với Shared Drive, dùng đúng `driveId` khi lấy mốc và đọc Changes.
   b. Duyệt `files.list` theo trang với `orderBy=modifiedTime desc`, chọn đúng corpus người dùng hoặc `drive` kèm `driveId` và các cờ Shared Drive phù hợp. Lọc MIME hỗ trợ và loại tệp đã trash.
   c. Lưu kết quả trang và `scan_page_token` cùng transaction. Nếu cursor bị từ chối, quét lại phạm vi với ghi lặp an toàn. Phạm vi trả `incompleteSearch` giữ trạng thái chưa đầy đủ.
3. Hợp nhất tăng dần các danh sách đã sắp thứ tự, loại trùng theo file ID. Chỉ xếp hàng tải/embedding khi tệp đã được chọn vào top 3.000 chung. Dừng đọc trang dư khi đã đủ ứng viên hợp lệ và các trang còn lại không thể thay đổi tập được chọn.
4. Khi hoàn tất quét theo ngân sách, bắt kịp Changes từ mốc lấy trước quét. Dùng phần chỉ mục đã sẵn sàng và báo trạng thái đồng bộ còn tiếp tục. Giữ riêng cursor quét với cursor Changes.

**Ca kiểm thử đặc biệt**:
- [ ] Worker chết giữa trang → restart → tiếp tục từ cursor nội bộ, không mất dữ liệu đã lưu
- [ ] Tệp mất quyền giữa quét → ghi nhận `status='error'`, không crash lượt quét
- [ ] Tệp mới đẩy tệp cũ khỏi 3.000 → chunks của tệp cũ bị xóa
- [ ] Nhiều Shared Drive lớn có file trùng và thay đổi trong lúc quét. Chỉ tệp thuộc tập hợp lệ được tải/embedding

---

### Task C2 — Đồng bộ tăng dần Changes API

| File | Sửa đổi |
|---|---|
| `app/workspace/drive_sync.py` | `incremental_sync(user_id, scope)`: Changes API mỗi 5 phút |
| `tests/unit/test_drive_sync.py` | Thay đổi/xóa/quyền |

**Thiết kế**:
1. Đọc `start_page_token` cho đúng khóa liên kết và phạm vi.
2. Gọi `changes.list(pageToken=...)` phân trang với phạm vi tương ứng và xử lý bản ghi bị loại bỏ.
3. Xử lý từng thay đổi:
   - File mới/sửa đổi → cập nhật metadata, kiểm tra trần trước khi xếp hàng parse phiên bản mới.
   - File xóa/trash/mất quyền → bỏ khỏi phạm vi truy xuất và xóa nội dung tương ứng.
   - Quyền thay đổi → vô hiệu cache tương ứng. Mất quyền cả Shared Drive → chặn toàn phạm vi và dọn chỉ mục của phạm vi đó.
4. Lưu thay đổi và cursor cùng transaction với kiểm tra khóa liên kết. Dùng `nextPageToken` giữa lượt, `newStartPageToken` ở trang cuối. Đọc lại cùng trang phải an toàn.
5. Cập nhật `last_sync_at` khi thử đồng bộ. Chỉ cập nhật `last_success_at` sau khi bắt kịp trang cuối thành công.

**Nghiệm thu**

- [ ] Worker chết trước hoặc sau commit có thể tiếp tục, không mất thay đổi hay tạo việc trùng
- [ ] Mất quyền cả Shared Drive làm cache và truy xuất nội dung của phạm vi đó mất hiệu lực ngay khi phát hiện

---

### Task C3 — Giữ trần 3.000 + Đối soát hằng ngày

| File | Sửa đổi |
|---|---|
| `app/workspace/drive_cap.py` | `enforce_cap(user_id)` + `daily_reconcile(user_id)` |
| `tests/unit/test_drive_cap.py` | Tệp mới đẩy cũ, reconcile lấp chỗ trống |

**Đối soát hằng ngày** (chạy 1 lần/ngày):
- So sánh metadata trong DB với `files.list` thực tế
- Xóa entry metadata cho file đã bị xóa/mất quyền trên Drive
- Lấp chỗ trống nếu dưới 3.000 bằng ứng viên tiếp theo theo `modifiedTime desc`. Khi một tệp mới thay chỗ tệp cũ, loại tệp cũ và chunks trong cùng lần cập nhật tập chỉ mục. Kết quả parse đang chạy cho tệp đã bị loại phải bị bỏ.
- Nghiệm thu giữ trần trong khi quét, đồng bộ và đọc theo yêu cầu cùng chạy. Kiểm tra tệp dài ngày được đưa vào khi có chỗ trống và trường hợp mất quyền cả Shared Drive.

---

## Phase D: Fallback & Đánh giá

### Task D1 — Fallback tìm trực tiếp

Khi chỉ mục thiếu bằng chứng, thực hiện một lượt tìm trực tiếp trong phạm vi Drive được cấp quyền, gồm Shared Drive. Xác định ngưỡng bằng bộ D2 và báo khi kết quả tìm kiếm mới phủ một phần.

| File | Sửa đổi |
|---|---|
| `app/workspace/drive_fallback.py` | `fallback_search(user_id, query)`: tìm trực tiếp trên Drive |
| `tests/unit/test_drive_fallback.py` | Giới hạn 1 lượt, 4 tệp, cache 24h |

**Ràng buộc**:
- Tối đa **1 lượt** fallback mỗi câu hỏi
- Tối đa **4 tệp** download + parse on-demand
- Dùng chung parser ưu tiên đọc theo yêu cầu, giới hạn tệp, deadline tác vụ và bước kiểm tra quyền/phiên bản của B5. Ghép kết quả trong giới hạn 8 đoạn từ tối đa 4 tài liệu.
- Kết quả tạm được mã hóa, gắn khóa liên kết, kiểm tra quyền và phiên bản lại trước khi dùng tiếp, tự xóa trong **24 giờ** và lưu riêng khỏi chỉ mục thường trực.
- Nghiệm thu tìm được tệp cũ ngoài top 3.000, dừng sau một lượt, xử lý được một tệp lỗi trong nhóm và báo khi bằng chứng còn thiếu.

---

### Task D2 — Bộ 60 câu tiếng Việt

| File | Sửa đổi |
|---|---|
| `tests/evaluation/drive_knowledge_60q.json` | 60 câu hỏi mẫu |
| `tests/evaluation/test_drive_knowledge_eval.py` | Script đánh giá |

**Phân loại câu**:
| Loại | Tiêu chí | Số câu |
|---|---|---|
| Có bằng chứng | Tài liệu chuẩn xuất hiện trong top 8 ít nhất 90% trên nhóm này. Các khẳng định dựa vào Drive có nguồn thực sự hỗ trợ | ~30 |
| Thiếu bằng chứng | Nêu rõ bằng chứng còn thiếu trong phạm vi đã tra cứu | ~20 |
| Chào hỏi | PHẢI phát sinh **0 lần đọc Drive** | ~10 |

Gắn nhãn tài liệu/đoạn chuẩn cho từng ca có bằng chứng. Bộ câu hỏi bao gồm tiếng Việt có dấu và không dấu, mã nghiệp vụ, câu hỏi không link, câu hỏi nối tiếp, nhiều nguồn mâu thuẫn và chỉ dẫn độc hại trong tài liệu. Đo riêng độ đúng truy xuất và độ đúng của trích dẫn trong câu trả lời.

---

### Task D3 — Kiểm tra đổi liên kết khi tác vụ đang chạy

**Kịch bản**:
1. User A có chỉ mục từ tài khoản Google cũ
2. Xếp hàng câu hỏi cần tra cứu Drive
3. Lặp với ba thời điểm relink/disconnect: khi đang chờ, đang tải hoặc parse, và sau tổng hợp nhưng trước callback/giao kết quả.
4. Thử thêm lần refresh token thông thường xen giữa, và dùng một người dùng khác có quyền trên cùng file ID để kiểm tra cách ly.

**Nghiệm thu**:
- [ ] Tác vụ cũ dừng với trạng thái liên kết đã thay đổi. Kết quả cũ không ghi vào chỉ mục, không chuyển sang LLM sau khi phát hiện thay đổi và không giao qua callback/Telegram
- [ ] Tác vụ mới trên tài khoản mới trả `not_indexed` nếu chưa có chỉ mục khả dụng
- [ ] Refresh thông thường giữ nguyên khả năng đọc chỉ mục. Liên kết cũ mất quyền ngay và dữ liệu liên quan được dọn trong 24 giờ

---

### Task D4 — Hồi quy toàn bộ

```powershell
Set-Location "D:\ODOO\Auth_gateway"; .\.venv\Scripts\python.exe -m pytest tests/ -q
Set-Location "D:\ODOO\Module AI - Agent"; .\.venv\Scripts\python.exe -m pytest tests/ -q
```

- [ ] Chạy hồi quy Gateway và Agent. Ghi số pass/fail/skip và lý do skip theo môi trường. Các ca bắt buộc của Giai đoạn 4 chạy đầy đủ trên môi trường hỗ trợ chúng
- [ ] Chạy PostgreSQL 16 + pgvector và dữ liệu My Drive/Shared Drive thử qua luồng Agent → Gateway → callback → giao kết quả. Kiểm tra nâng/hạ migration trên bản sao dữ liệu Giai đoạn 3
- [ ] Đo **p95 truy xuất chỉ mục nội bộ ≤ 1 giây** khi có đồng bộ nền. Đo riêng thời gian embedding truy vấn, kiểm quyền Google và thời gian trả lời đầu cuối. Ghi quy mô dữ liệu, cấu hình và so sánh với baseline cùng fixture/model
- [ ] Chạy tải theo kế hoạch gốc với 25 người dùng trong 30 phút và đợt 50 người dùng trong 5 phút trong lúc đồng bộ nền. Kiểm tra p95 nhận tác vụ ≤ 3 giây, tỷ lệ thành công ≥ 99% khi dịch vụ mô phỏng hoạt động bình thường, cách ly dữ liệu giữa người dùng và không có deadlock
- [ ] Ghi kết quả bộ 60 câu, tỷ lệ cache hit/miss, quota Google, chi phí embedding, CPU/RAM và các tiêu chí còn thiếu bằng chứng

---

## Mốc chuyển task

- **A → B** khi định danh liên kết và migration đã qua kiểm tra trên PostgreSQL 16.
- **B → C** khi một tệp thử đi từ tải, parse, lưu mã hóa, truy xuất và kiểm quyền đến câu trả lời có nguồn qua Agent.
- **C → D** khi đã kiểm tra checkpoint, tiếp tục sau sự cố và trần 3.000 trên nhiều phạm vi. Có thể chuẩn bị bộ dữ liệu có nhãn của D2 trong lúc thực hiện C.
- **Hoàn tất Giai đoạn 4** khi các mục nghiệm thu D2–D4 có kết quả đo và các ca bắt buộc đã đạt.

## Chạy thử sau triển khai

1. Dùng PostgreSQL 16 + pgvector và dữ liệu thử riêng. Khởi động Gateway qua cả `compose.yaml` và `compose.postgres.yaml` để chạy migration A2 rồi khởi động hai Workspace worker và `drive-indexer`.
2. Đặt `DRIVE_INDEX_ENABLED=true` ở Gateway và Agent thử nghiệm. Bật `WORKSPACE_BACKGROUND_READS=true` ở Agent để thử callback và Telegram. Cấu hình Gateway và Agent trỏ đến nhau bằng URL và khóa nội bộ của môi trường thử.
3. Liên kết một tài khoản Google thử có My Drive và quyền đọc Shared Drive thử. Kiểm tra `GET /internal/v1/users/{user_id}/workspace/drive/sync-status` bằng xác thực nội bộ. Dùng `POST /internal/v1/users/{user_id}/workspace/drive/resync` khi cần quét lại.
4. Để trống `EMBEDDING_API_KEY` khi thử FTS. Đặt khóa embedding ở Gateway khi đã duyệt gửi dữ liệu thử đến nhà cung cấp. Lượt kiểm thử dùng mock embedding có thể kiểm tra nhánh vector mà không dùng khóa thật.
5. Để tái lập kiểm thử tải mô phỏng, đặt `STAGE4_POSTGRES_TEST_URL` trỏ đến PostgreSQL thử có pgvector và quyền tạo database tạm, rồi chạy `python -m scripts.stage4_drive_load` từ Gateway. Chạy các ca Drive thật và chấm câu trả lời từ bộ 60 câu riêng với kết quả tải mô phỏng.
