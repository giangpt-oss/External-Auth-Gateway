from typing import Any

from app.odoo.client import (
    OdooAccessError,
    OdooAccountInactiveError,
    OdooClient,
    OdooExternalUserError,
)


class OdooService:
    def __init__(self, client: OdooClient):
        self.client = client

    def validate_user_account(self, uid: int, secret: str) -> dict[str, Any]:
        """Xác minh trạng thái tài khoản Odoo bằng credential người dùng.
        Đọc lại res.users, kiểm tra active=True và share=False.
        Ném OdooAccountInactiveError nếu active=False.
        Ném OdooExternalUserError nếu share=True.
        Ném OdooAccessError/OdooAuthError nếu credential bị từ chối/thu hồi.
        Ném OdooNetworkError nếu timeout/mất kết nối."""
        fields = ["id", "name", "login", "active", "share", "tz", "partner_id"]
        records = self.client.execute(
            uid=uid,
            secret=secret,
            model="res.users",
            method="read",
            args=[[uid]],
            kwargs={"fields": fields},
        )
        if not records or not isinstance(records, list) or len(records) == 0:
            raise OdooAccessError("Không tìm thấy thông tin tài khoản Odoo hoặc quyền truy cập bị từ chối.")
        user_data = records[0]
        if not user_data.get("active"):
            raise OdooAccountInactiveError("Tài khoản Odoo của bạn đã bị vô hiệu hóa hoặc bị khóa.")
        if user_data.get("share"):
            raise OdooExternalUserError("Tài khoản người dùng ngoài (portal/external) không được cấp quyền.")
        return user_data

    def get_user_profile(self, uid: int, secret: str) -> dict[str, Any] | None:
        fields = ["id", "name", "login", "active", "share", "tz", "partner_id"]
        records = self.client.execute(
            uid=uid,
            secret=secret,
            model="res.users",
            method="read",
            args=[[uid]],
            kwargs={"fields": fields},
        )
        if records and isinstance(records, list):
            return records[0]
        return None

    def search_my_leads(
        self,
        uid: int,
        secret: str,
        search_term: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        domain: list[Any] = [
            ("user_id", "=", uid),
            ("active", "=", True),
        ]
        if search_term:
            domain.append(("name", "ilike", search_term.strip()[:100]))

        fields = ["id", "name", "stage_id"]
        order = "write_date desc, id desc"
        return self.client.execute(
            uid=uid,
            secret=secret,
            model="crm.lead",
            method="search_read",
            args=[domain],
            kwargs={"fields": fields, "order": order, "limit": limit},
        )

    def get_employee_profile(self, uid: int, secret: str) -> dict[str, Any]:
        """Lấy hồ sơ nhân viên qua hr.employee bằng credential của chính người dùng."""
        try:
            records = self.client.execute(
                uid=uid,
                secret=secret,
                model="hr.employee",
                method="search_read",
                args=[[("user_id", "=", uid)]],
                kwargs={"fields": ["name", "job_title", "department_id", "parent_id"], "limit": 1},
            )
            if records and isinstance(records, list) and len(records) > 0:
                rec = records[0]
                dept = rec.get("department_id")
                department = dept[1] if isinstance(dept, list | tuple) and len(dept) > 1 else None
                mgr = rec.get("parent_id")
                manager_name = mgr[1] if isinstance(mgr, list | tuple) and len(mgr) > 1 else None
                job_title = rec.get("job_title") or None
                return {
                    "job_title": job_title,
                    "department": department,
                    "manager_name": manager_name,
                }
        except Exception:
            # Thiếu quyền đọc hồ sơ thì trả thông tin thiếu; không tự gán chức vụ hoặc dùng credential quản trị
            pass
        return {
            "job_title": None,
            "department": None,
            "manager_name": None,
        }

    def compute_permissions(
        self,
        uid: int,
        secret: str,
        is_internal_user: bool = True,
        google_status: str = "disconnected",
        google_scopes: list[str] | None = None,
    ) -> list[str]:
        """Tính toán danh sách 9 quyền hạn chuẩn hóa dựa trên trạng thái thực tế."""
        perms: set[str] = set()

        # 1. Công việc cá nhân (tasks): cấp cho người dùng Odoo nội bộ đang hoạt động
        if is_internal_user:
            perms.add("tasks.read")
            perms.add("tasks.write")

        # 2. CRM: xác định quyền model thực tế trên crm.lead, cô lập từng thao tác
        if secret:
            for op, perm_name in [("read", "crm.read"), ("create", "crm.create"), ("write", "crm.write")]:
                try:
                    can_op = self.client.execute(
                        uid=uid,
                        secret=secret,
                        model="crm.lead",
                        method="check_access_rights",
                        args=[op, False],
                    )
                    if can_op:
                        perms.add(perm_name)
                except Exception:
                    # Lỗi kiểm tra một quyền không giữ lại kết quả chưa xác minh đầy đủ
                    pass

        # 3. Google: quyền phụ thuộc trạng thái kết nối và scopes chính xác
        if google_status == "connected" and google_scopes:
            scopes = [s.lower() for s in google_scopes]

            # Calendar read: calendar.readonly, calendar.events.readonly, calendar.events, hoặc full /calendar
            has_cal_read = any(
                "calendar.readonly" in s
                or "calendar.events.readonly" in s
                or "calendar.events" in s
                or s.endswith("/calendar")
                for s in scopes
            )
            if has_cal_read:
                perms.add("calendar.read")

            # Calendar write: calendar.events hoặc full /calendar NHƯNG không chứa readonly
            has_cal_write = any(
                ("calendar.events" in s or s.endswith("/calendar"))
                and "readonly" not in s
                for s in scopes
            )
            if has_cal_write:
                perms.add("calendar.write")

            # Gmail read: gmail.readonly, gmail.modify, mail.google.com (KHÔNG cấp cho gmail.send đơn thuần)
            has_gmail_read = any(
                "gmail.readonly" in s
                or "gmail.modify" in s
                or "mail.google.com" in s
                for s in scopes
            )
            if has_gmail_read:
                perms.add("gmail.read")

            # Gmail send: gmail.send, gmail.modify, mail.google.com
            has_gmail_send = any(
                "gmail.send" in s
                or "gmail.modify" in s
                or "mail.google.com" in s
                for s in scopes
            )
            if has_gmail_send:
                perms.add("gmail.send")

        return sorted(perms)

    def create_lead(
        self,
        uid: int,
        secret: str,
        name: str,
        contact_name: str | None = None,
        email_from: str | None = None,
        phone: str | None = None,
        description: str | None = None,
        expected_revenue: float | None = None,
        stage_id: int | None = None,
    ) -> dict[str, Any]:
        """Tạo lead/opportunity trong crm.lead dưới tài khoản người dùng.
        Tuyệt đối không cho chỉnh user_id hay company_id."""
        vals: dict[str, Any] = {
            "name": name.strip()[:200],
            "user_id": uid,
        }
        if contact_name:
            vals["contact_name"] = contact_name.strip()[:100]
        if email_from:
            vals["email_from"] = email_from.strip()[:100]
        if phone:
            vals["phone"] = phone.strip()[:50]
        if description:
            vals["description"] = description.strip()[:2000]
        if expected_revenue is not None:
            vals["expected_revenue"] = float(expected_revenue)
        if stage_id is not None:
            stage_domain = [("id", "=", stage_id)]
            stages = self.client.execute(
                uid=uid,
                secret=secret,
                model="crm.stage",
                method="search_read",
                args=[stage_domain],
                kwargs={"fields": ["id", "name"], "limit": 1},
            )
            if not stages:
                raise ValueError(f"Giai đoạn CRM (stage_id={stage_id}) không tồn tại hoặc không có quyền truy cập.")
            vals["stage_id"] = stage_id

        lead_id = self.client.execute(
            uid=uid,
            secret=secret,
            model="crm.lead",
            method="create",
            args=[vals],
        )
        return {"id": lead_id, "name": vals["name"]}

    def get_lead_detail(self, uid: int, secret: str, lead_id: int) -> dict[str, Any] | None:
        """Đọc thông tin chi tiết một bản ghi CRM lead."""
        fields = [
            "id", "name", "contact_name", "email_from", "phone",
            "expected_revenue", "stage_id", "write_date", "user_id"
        ]
        domain = [("id", "=", lead_id)]
        records = self.client.execute(
            uid=uid,
            secret=secret,
            model="crm.lead",
            method="search_read",
            args=[domain],
            kwargs={"fields": fields, "limit": 1},
        )
        if records and isinstance(records, list) and len(records) > 0:
            return records[0]
        return None

    def update_lead(
        self,
        uid: int,
        secret: str,
        lead_id: int,
        name: str | None = None,
        contact_name: str | None = None,
        email_from: str | None = None,
        phone: str | None = None,
        description: str | None = None,
        expected_revenue: float | None = None,
        stage_id: int | None = None,
        expected_write_date: str | None = None,
    ) -> dict[str, Any]:
        """Cập nhật lead/opportunity trong crm.lead.
        Kiểm tra ID bản ghi và quyền truy cập giai đoạn CRM. Không cho chỉnh chủ sở hữu/công ty."""
        domain = [("id", "=", lead_id)]
        existing = self.client.execute(
            uid=uid,
            secret=secret,
            model="crm.lead",
            method="search_read",
            args=[domain],
            kwargs={"fields": ["id", "name", "stage_id", "user_id", "write_date"], "limit": 1},
        )
        if not existing:
            raise ValueError(f"Bản ghi CRM (lead_id={lead_id}) không tồn tại hoặc bạn không có quyền truy cập.")

        current_rec = existing[0]
        if expected_write_date and current_rec.get("write_date"):
            if str(current_rec.get("write_date")).strip() != str(expected_write_date).strip():
                raise ValueError(
                    f"Dữ liệu cơ hội CRM đã bị thay đổi trên hệ thống sau khi tạo bản xem trước "
                    f"(hiện tại: {current_rec.get('write_date')}, xem trước: {expected_write_date}). "
                    f"Vui lòng tạo yêu cầu xem trước mới."
                )

        vals: dict[str, Any] = {}
        if name:
            vals["name"] = name.strip()[:200]
        if contact_name:
            vals["contact_name"] = contact_name.strip()[:100]
        if email_from:
            vals["email_from"] = email_from.strip()[:100]
        if phone:
            vals["phone"] = phone.strip()[:50]
        if description:
            vals["description"] = description.strip()[:2000]
        if expected_revenue is not None:
            vals["expected_revenue"] = float(expected_revenue)
        if stage_id is not None:
            stage_domain = [("id", "=", stage_id)]
            stages = self.client.execute(
                uid=uid,
                secret=secret,
                model="crm.stage",
                method="search_read",
                args=[stage_domain],
                kwargs={"fields": ["id", "name"], "limit": 1},
            )
            if not stages:
                raise ValueError(f"Giai đoạn CRM (stage_id={stage_id}) không tồn tại hoặc không có quyền truy cập.")
            vals["stage_id"] = stage_id

        if vals:
            self.client.execute(
                uid=uid,
                secret=secret,
                model="crm.lead",
                method="write",
                args=[[lead_id], vals],
            )
        return {"id": lead_id, "updated": True}

    def execute_catalog_query(
        self,
        uid: int,
        secret: str,
        model: str,
        fields: list[str] | None = None,
        domain: list[Any] | None = None,
        limit: int | None = None,
        offset: int | None = None,
        order: str | None = None,
    ) -> dict[str, Any]:
        """Thực hiện truy vấn đọc an toàn qua Whitelist Catalog bằng credential của chính người dùng.

        Trả về dict: {"items": [...], "count": len(items), "limit": limit, "offset": offset, "model": model}
        """
        from app.odoo.catalog import validate_catalog_query

        val_model, val_fields, val_domain, val_limit, val_offset, val_order = validate_catalog_query(
            model=model,
            fields=fields,
            domain=domain,
            limit=limit,
            offset=offset,
            order=order,
        )

        records = self.client.execute(
            uid=uid,
            secret=secret,
            model=val_model,
            method="search_read",
            args=[val_domain],
            kwargs={
                "fields": val_fields,
                "limit": val_limit,
                "offset": val_offset,
                "order": val_order,
            },
        )

        items = records if isinstance(records, list) else []
        return {
            "items": items,
            "count": len(items),
            "limit": val_limit,
            "offset": val_offset,
            "model": val_model,
        }

