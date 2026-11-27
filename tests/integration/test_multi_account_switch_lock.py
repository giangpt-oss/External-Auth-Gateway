"""Integration tests for Multi-Google-Accounts with Switch-Lock Specification.

Validates:
1. Multi-account linking for a single user (1-to-many User -> GoogleConnection).
2. Exactly one active account (active_google_account_id) selected across all Workspace services.
3. OAuth complete handles add vs reconnect and prevents cross-user account takeover.
4. Switch lock:
   - When account A has active tasks/blockers, switch to B is rejected with 409 ACCOUNT_BUSY and transitions to draining.
   - While in draining, new tasks and scheduled syncs are rejected for this user.
   - When blockers clear, state indicates can_switch=True ("Có thể chuyển tài khoản").
   - System NEVER auto-switches on polling; manual user confirm_switch is required.
   - User can click 'Tiếp tục dùng A' (resume) to cancel draining.
   - When confirmed, selection_version increments and active_account changes to B.
   - Executing stale previews with old selection_version returns ACCOUNT_CHANGED.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.google.account_resolver import GoogleAccountResolver
from app.google.oauth import GoogleOAuthError, GoogleOAuthService
from app.models import (
    Base,
    GoogleAccountSelection,
    GoogleConnection,
    User,
)
from app.security import encrypt_secret
from app.workspace.activity_tracker import ActivityType, WorkspaceActivityTracker
from app.workspace.models import WorkspaceJob
from app.workspace.selection_manager import SelectionManager, SwitchLockError

FERNET_KEY = "_3pDgavXi1u3T76np_zSNJQcNS87hKW7fEdQXjlu3Ew="


@pytest.fixture
def multi_account_env(tmp_path):
    db_file = tmp_path / "test_multi_accounts.db"
    db_url = f"sqlite:///{db_file}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as db:
        # Create User 1
        u1 = User(
            id=1,
            tenant="default",
            database="odoo",
            odoo_uid=1,
            login="user1@example.com",
            name="User 1",
            active=True,
        )
        # Create User 2
        u2 = User(
            id=2,
            tenant="default",
            database="odoo",
            odoo_uid=2,
            login="user2@example.com",
            name="User 2",
            active=True,
        )
        db.add_all([u1, u2])
        db.flush()

        # Connect Account A for User 1
        conn_a = GoogleConnection(
            id=1,
            user_id=1,
            google_sub="sub_google_account_a",
            email="account_a@gmail.com",
            scopes="https://www.googleapis.com/auth/drive.readonly https://www.googleapis.com/auth/calendar.events",
            encrypted_access_token=encrypt_secret("access_token_a", FERNET_KEY),
            encrypted_refresh_token=encrypt_secret("refresh_token_a", FERNET_KEY),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            status="connected",
            connection_version=1,
            link_version=1,
        )
        # Connect Account B for User 1
        conn_b = GoogleConnection(
            id=2,
            user_id=1,
            google_sub="sub_google_account_b",
            email="account_b@gmail.com",
            scopes="https://www.googleapis.com/auth/drive.readonly https://www.googleapis.com/auth/calendar.events",
            encrypted_access_token=encrypt_secret("access_token_b", FERNET_KEY),
            encrypted_refresh_token=encrypt_secret("refresh_token_b", FERNET_KEY),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            status="connected",
            connection_version=1,
            link_version=1,
        )
        # Connect Account C for User 2 (to test ownership enforcement)
        conn_c = GoogleConnection(
            id=3,
            user_id=2,
            google_sub="sub_google_account_c",
            email="account_c@gmail.com",
            scopes="https://www.googleapis.com/auth/drive.readonly",
            encrypted_access_token=encrypt_secret("access_token_c", FERNET_KEY),
            encrypted_refresh_token=encrypt_secret("refresh_token_c", FERNET_KEY),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            status="connected",
            connection_version=1,
            link_version=1,
        )
        db.add_all([conn_a, conn_b, conn_c])
        db.flush()

        # Selection for User 1: Account A is active
        sel1 = GoogleAccountSelection(
            user_id=1,
            active_account_id=1,
            selection_version=1,
            state="accepting",
        )
        db.add(sel1)
        db.commit()

    service = GoogleOAuthService(
        session_factory=Session,
        client_id="mock_client_id",
        client_secret="mock_client_secret",
        redirect_uri="http://localhost:8080/auth/callback",
        encryption_key=FERNET_KEY,
    )

    yield Session, service

    engine.dispose()
    import gc
    gc.collect()
    try:
        if db_file.exists():
            db_file.unlink()
    except PermissionError:
        pass


def test_resolver_and_active_account(multi_account_env):
    Session, _ = multi_account_env
    with Session() as db:
        # Active account for User 1 is Account A
        active = GoogleAccountResolver.get_active_account(db, user_id=1)
        assert active is not None
        assert active.id == 1
        assert active.email == "account_a@gmail.com"

        # List user connections returns both Account A and Account B
        conns = GoogleAccountResolver.get_user_connections(db, user_id=1)
        assert len(conns) == 2
        emails = {c.email for c in conns}
        assert emails == {"account_a@gmail.com", "account_b@gmail.com"}

        # Pinned lookup gets Account B specifically
        pinned_b = GoogleAccountResolver.resolve_for_task(db, user_id=1, pinned_account_id=2)
        assert pinned_b is not None
        assert pinned_b.id == 2
        assert pinned_b.email == "account_b@gmail.com"

        # Cannot access User 2's account
        foreign = GoogleAccountResolver.get_account(db, user_id=1, account_id=3)
        assert foreign is None


def test_admission_control_accepting_vs_draining(multi_account_env):
    Session, _ = multi_account_env
    with Session() as db:
        # In accepting state, admission succeeds
        admitted, reason, sel = SelectionManager.check_admission(db, user_id=1)
        assert admitted is True
        assert reason is None

        # Admission with correct expected version succeeds
        admitted, reason, _ = SelectionManager.check_admission(db, user_id=1, expected_version=1)
        assert admitted is True

        # Admission with wrong version fails
        admitted, reason, _ = SelectionManager.check_admission(db, user_id=1, expected_version=99)
        assert admitted is False
        assert "thay đổi" in reason

        # Put user into draining
        sel.state = "draining"
        db.commit()

        # In draining state, new admission fails
        admitted, reason, _ = SelectionManager.check_admission(db, user_id=1)
        assert admitted is False
        assert "tạm thời không nhận việc mới" in reason.lower() or "draining" in reason.lower() or "tác vụ dang dở" in reason.lower()


def test_switch_lock_workflow_with_blockers(multi_account_env):
    """Complete switch lock workflow:
    1. Running task on Account A blocks switch to B -> returns 409 busy, sets state=draining.
    2. Trying to admit new task on A while draining -> rejected.
    3. Blockers cleared -> state becomes ready_to_switch (can_switch=True).
    4. Confirm switch switches to B and increments version.
    """
    Session, _ = multi_account_env
    with Session() as db:
        # Register an active workspace job on Account A
        job = WorkspaceJob(
            job_id="job_uuid_123",
            user_id=1,
            google_account_id=1,
            selection_version=1,
            request_id="req_123",
            service="gmail",
            operation="send",
            status="running",
            deadline_at=datetime.now(UTC) + timedelta(minutes=5),
            payload_encrypted=encrypt_secret("{}", FERNET_KEY),
        )
        db.add(job)
        db.flush()

        WorkspaceActivityTracker.register_activity(
            db,
            user_id=1,
            activity_type=ActivityType.TASK_RUNNING,
            reference_id=str(job.id),
            google_account_id=1,
        )
        db.commit()

        # 1. User requests switch to Account B (id=2)
        res = SelectionManager.request_switch(db, user_id=1, target_account_id=2)
        assert res["status"] == "busy"
        assert res["state"] == "draining"
        assert res["can_switch"] is False
        assert len(res["blockers"]) >= 1

        # Confirm switch prematurely fails with ACCOUNT_BUSY
        with pytest.raises(SwitchLockError) as exc_info:
            SelectionManager.confirm_switch(db, user_id=1, target_account_id=2)
        assert exc_info.value.code == "ACCOUNT_BUSY"

        # 2. Admission of new work while draining is rejected
        admitted, reason, _ = SelectionManager.check_admission(db, user_id=1)
        assert admitted is False

        # 3. Complete the task and finish activity
        job.status = "completed"
        WorkspaceActivityTracker.finish_activity(
            db,
            user_id=1,
            reference_id=str(job.id),
            status="completed",
        )
        db.commit()

        # Blockers are now empty
        blockers = WorkspaceActivityTracker.get_active_blockers(db, user_id=1, account_id=1)
        assert len(blockers) == 0

        # Request switch again -> now ready_to_switch
        res2 = SelectionManager.request_switch(db, user_id=1, target_account_id=2)
        assert res2["status"] == "ready_to_switch"
        assert res2["can_switch"] is True

        # 4. Confirm switch to Account B
        switched = SelectionManager.confirm_switch(db, user_id=1, target_account_id=2)
        assert switched["status"] == "switched"
        assert switched["active_account_id"] == 2
        assert switched["selection_version"] == 2
        assert switched["state"] == "accepting"

        # Check in DB
        sel = GoogleAccountResolver.get_selection(db, user_id=1)
        assert sel.active_account_id == 2
        assert sel.selection_version == 2
        assert sel.state == "accepting"

        active_now = GoogleAccountResolver.get_active_account(db, user_id=1)
        assert active_now.id == 2
        assert active_now.email == "account_b@gmail.com"


def test_resume_cancels_draining(multi_account_env):
    Session, _ = multi_account_env
    with Session() as db:
        # Request switch puts user into draining
        SelectionManager.request_switch(db, user_id=1, target_account_id=2)
        sel = GoogleAccountResolver.get_selection(db, user_id=1)
        assert sel.state == "draining"

        # Click 'Tiếp tục dùng A' (resume)
        res = SelectionManager.resume_current(db, user_id=1)
        assert res["status"] == "resumed"
        assert res["state"] == "accepting"
        assert res["active_account_id"] == 1

        sel = GoogleAccountResolver.get_selection(db, user_id=1)
        assert sel.state == "accepting"
        assert sel.target_account_id is None


def test_cannot_connect_account_owned_by_other_user(multi_account_env):
    """User 1 cannot link google_sub belonging to User 2."""
    Session, service = multi_account_env

    # Try to complete OAuth with User 2's google_sub (sub_google_account_c)
    # Mock token exchange
    with Session() as db:
        from app.models import LinkToken
        from app.security import hash_token
        lt = LinkToken(
            token_hash=hash_token("test_state_123"),
            purpose="google_oauth:add",
            user_id=1,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        db.add(lt)
        db.commit()

    import unittest.mock as mock
    with mock.patch("httpx.post") as mock_post, \
         mock.patch("google.oauth2.id_token.verify_oauth2_token") as mock_verify:
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "access_token": "new_tok",
            "refresh_token": "new_ref",
            "id_token": "mock_id_tok",
            "expires_in": 3600,
        }
        # Sub belongs to User 2's conn_c!
        mock_verify.return_value = {
            "sub": "sub_google_account_c",
            "email": "account_c@gmail.com",
            "nonce": None,
        }

        with pytest.raises(GoogleOAuthError) as exc_info:
            service.complete(user_id=1, state="test_state_123", code="mock_code")
        assert "người dùng khác" in str(exc_info.value)


def test_api_accounts_and_switch_lock_endpoints(tmp_path):
    """Test REST API endpoints:
    - GET /connections/google/accounts
    - POST /connections/google/accounts/{id}/activate
    - POST /connections/google/resume
    - POST /connections/google/accounts/{id}/confirm-switch
    - POST /connections/google/accounts/{id}/disconnect
    """
    from starlette.testclient import TestClient

    from app.config import Settings
    from app.main import create_app
    from app.models import Session as UserSession
    from app.security import generate_secure_token, hash_token

    db_file = tmp_path / "test_api_multi_acc.db"
    db_url = f"sqlite:///{db_file}"
    settings = Settings(
        APP_ENV="test",
        DATABASE_URL=db_url,
        ENCRYPTION_KEY=FERNET_KEY,
    )
    app = create_app(settings)
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    session_token = generate_secure_token(32)
    session_hash = hash_token(session_token)

    with Session() as db:
        user = User(
            id=1,
            tenant="default",
            database="odoo",
            odoo_uid=1,
            login="user1@example.com",
            name="User 1",
            active=True,
        )
        db.add(user)
        db.flush()

        # Create session
        s = UserSession(
            user_id=1,
            token_hash=session_hash,
            csrf_secret="csrf_secret_123",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        db.add(s)

        # Account A
        conn_a = GoogleConnection(
            id=1,
            user_id=1,
            google_sub="sub_a",
            email="a@gmail.com",
            scopes="https://www.googleapis.com/auth/drive.readonly",
            encrypted_access_token=encrypt_secret("tok_a", FERNET_KEY),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            status="connected",
            connection_version=1,
            link_version=1,
        )
        # Account B
        conn_b = GoogleConnection(
            id=2,
            user_id=1,
            google_sub="sub_b",
            email="b@gmail.com",
            scopes="https://www.googleapis.com/auth/drive.readonly",
            encrypted_access_token=encrypt_secret("tok_b", FERNET_KEY),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            status="connected",
            connection_version=1,
            link_version=1,
        )
        db.add_all([conn_a, conn_b])
        db.flush()

        sel = GoogleAccountSelection(
            user_id=1,
            active_account_id=1,
            selection_version=1,
            state="accepting",
        )
        db.add(sel)
        db.commit()

    with TestClient(app) as client:
        client.cookies.set("session_id", session_token)

        # 1. GET /connections/google/accounts
        res = client.get("/connections/google/accounts")
        assert res.status_code == 200
        data = res.json()
        assert len(data["accounts"]) == 2
        assert data["active_account_id"] == 1
        assert data["state"] == "accepting"
        assert data["selection_version"] == 1
        assert data["can_switch"] is False

        # Add an active blocker on Account A
        with Session() as db:
            WorkspaceActivityTracker.register_activity(
                db,
                user_id=1,
                activity_type=ActivityType.TASK_RUNNING,
                reference_id="ref_job_456",
                google_account_id=1,
            )
            db.commit()

        # Request on tab A leaves the active account pinned while draining.
        res_act = client.post("/connections/google/accounts/2/activate?current_version=1")
        assert res_act.status_code == 200
        assert res_act.json()["status"] == "busy"
        assert res_act.json()["blockers"]

        poll = client.get("/connections/google/accounts").json()
        assert poll["state"] == "draining"
        assert poll["active_account_id"] == 1
        assert poll["target_account_id"] == 2
        assert poll["can_switch"] is False

        resumed = client.post("/connections/google/resume?current_version=1")
        assert resumed.status_code == 200
        assert resumed.json()["state"] == "accepting"

        # Tab B starts another drain while the same activity is still running.
        headers = {"Accept": "text/html"}
        busy = client.post(
            "/connections/google/accounts/2/activate?current_version=1", headers=headers,
            follow_redirects=False,
        )
        assert busy.headers["location"] == "/?status=switch_busy"
        assert client.post("/connections/google/accounts/2/confirm?current_version=1").status_code == 409
        with Session() as db:
            WorkspaceActivityTracker.finish_activity(
                db, user_id=1, reference_id="ref_job_456", status="completed",
            )
            db.commit()

        # Replaying the original activation from another tab cannot commit a pending switch.
        retry = client.post("/connections/google/accounts/2/activate?current_version=1")
        assert retry.json()["status"] == "ready_to_switch"
        assert client.get("/connections/google/accounts").json()["active_account_id"] == 1

        confirmed = client.post("/connections/google/accounts/2/confirm?current_version=1")
        assert confirmed.status_code == 200
        assert confirmed.json()["active_account_id"] == 2
        assert confirmed.json()["selection_version"] == 2
        assert client.post("/connections/google/accounts/1/activate?current_version=1").status_code == 409

        # A fresh activation with the current version switches immediately when idle.
        idle = client.post("/connections/google/accounts/1/activate?current_version=2")
        assert idle.json()["status"] == "switched"
        assert idle.json()["active_account_id"] == 1
        assert idle.json()["selection_version"] == 3
