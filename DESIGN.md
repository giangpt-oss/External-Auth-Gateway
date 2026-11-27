# Giao diện Portal Gateway

Tài liệu giúp dev tìm đúng template và tài nguyên đang được sử dụng khi sửa Portal.

## Điểm vào giao diện

| File | Vai trò |
|---|---|
| [app/portal/routes.py](app/portal/routes.py) | Lấy dữ liệu và render trang |
| [templates/login.html](app/portal/templates/login.html) | Chọn giao diện đăng nhập |
| [templates/dashboard.html](app/portal/templates/dashboard.html) | Chọn màn hình theo trạng thái kết nối |
| [templates/stitch/](app/portal/templates/stitch/) | Template các màn hình và thành phần chung |
| [static/stitch.js](app/portal/static/stitch.js) | Tương tác phía trình duyệt |
| [static/stitch/theme-colors.css](app/portal/static/stitch/theme-colors.css) | Màu theo màn hình và chế độ sáng/tối |

## Các màn hình đang dùng

Dashboard chọn screen từ dữ liệu backend.

| Screen | Điều kiện trong dashboard.html |
|---|---|
| verified | telegram_state bằng verified |
| empty | Chưa có google_connections và chưa vào nhánh verified |
| accounts | Các trường hợp còn lại |

login.html dùng login_light.html. dashboard.html include template tương ứng có hậu tố _light.html. Biến theme nhận dark khi query parameter theme=dark, các trường hợp còn lại dùng light.

Trong thư mục stitch còn có template _dark.html. Khi sửa, kiểm tra chuỗi include thực tế từ template đầu vào để xác định nội dung đang render.

## Màu và chuyển theme

Các template hiện có bridge_head.html, theme_switch.html và các file palette theo màn hình. theme-colors.css áp dụng màu qua thuộc tính data-stitch-theme, data-stitch-screen và data-stitch-palette.

Khi đổi theme, kiểm tra đồng thời nền, chữ, đường viền, ô nhập, focus và trạng thái hover. Lấy giá trị đang dùng từ CSS và template của màn hình liên quan để giữ đồng nhất.

## Cách sửa một màn hình

1. Xác định route và dữ liệu truyền sang template.
2. Theo include từ login.html hoặc dashboard.html đến màn hình đang render.
3. Sửa bố cục hoặc nội dung tại template tương ứng.
4. Sửa màu tại palette hoặc stylesheet đang áp dụng.
5. Kiểm tra tương tác trong stitch.js nếu thay nút hoặc thuộc tính HTML.
6. Mở cả theme sáng/tối và các trạng thái kết nối liên quan.

## Nội dung và trạng thái

Dùng tiếng Việt dễ hiểu cho nhãn và thông báo. Các trạng thái tài khoản cần phản ánh dữ liệu backend. Với hành động liên kết hoặc kết nối lại, giữ đúng form, route và dữ liệu xác thực mà backend yêu cầu.

Thông báo lỗi cần cho người dùng biết bước có thể làm tiếp. Dùng chữ hoặc biểu tượng cùng màu để nhận biết trạng thái. Kiểm tra tên dài, email dài, lỗi đăng nhập và tài khoản chưa kết nối.

## Kiểm tra sau khi sửa

Chạy kiểm thử Portal và kiểm tra trực tiếp trong trình duyệt.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_portal.py -q
```

Kiểm tra màn hình desktop, màn hình hẹp, điều hướng bàn phím, focus, nội dung tràn dòng và chuyển theme. Kết quả render và hành vi thực tế là căn cứ xác nhận thay đổi giao diện.
