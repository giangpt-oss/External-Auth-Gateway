from datetime import UTC, datetime

from app.db import init_db
from app.google.account_resolver import GoogleAccountResolver
from app.models import GoogleAccountSelection, GoogleConnection, User
from app.portal.workspace_status import workspace_app_statuses


def test_reading_workspace_state_and_initializing_legacy_selection(temp_db_path):
    engine, sessions = init_db(f"sqlite:///{temp_db_path}")
    with sessions() as db:
        user = User(tenant="odoo", database="test", odoo_uid=1, login="test", name="Test")
        db.add(user)
        db.flush()
        accounts = []
        for name, scope in [("first", "gmail.readonly"), ("second", "drive.readonly")]:
            account = GoogleConnection(
                user_id=user.id, google_sub=name, email=f"{name}@example.com", scopes=scope,
                encrypted_access_token="unused", expires_at=datetime.now(UTC),
            )
            db.add(account)
            accounts.append(account)
        db.flush()
        first, second = accounts
        assert GoogleAccountResolver.get_active_account(db, user.id) is first
        assert db.query(GoogleAccountSelection).count() == 0

        selection = GoogleAccountResolver.lock_selection(db, user.id)
        assert selection.active_account_id == first.id
        assert GoogleAccountResolver.lock_selection(db, user.id) is selection
        selection.active_account_id = second.id
        db.flush()
        current = GoogleAccountResolver.get_active_account(db, user.id)
        assert current is second
        assert workspace_app_statuses(current) == {
            "gmail": False, "calendar": False, "drive": True,
            "docs": True, "sheets": True, "slides": True,
        }
        assert GoogleAccountResolver.resolve_for_task(db, user.id, first.id) is first
        selection.active_account_id = None
        db.flush()
        assert GoogleAccountResolver.get_active_account(db, user.id) is None
        assert not any(workspace_app_statuses(None).values())
    engine.dispose()
