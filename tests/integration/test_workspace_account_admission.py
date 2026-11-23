from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from os import environ
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.google.client import GoogleClient
from app.google.oauth import GoogleOAuthService
from app.models import Base, GoogleAccountSelection, GoogleConnection, User
from app.workspace.models import WorkspaceJob
from app.workspace.selection_manager import SelectionManager, SwitchLockError
from app.workspace.service import WorkspaceError, WorkspaceService


@pytest.fixture(params=["sqlite"])
def accounts(request, tmp_path):
    url = (
        environ.get("STAGE2_POSTGRES_TEST_URL")
        if request.param == "postgresql"
        else f"sqlite:///{tmp_path / 'admission.db'}"
    )
    if not url:
        pytest.skip("Requires a disposable PostgreSQL database")
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_engine(url)
    if request.param == "sqlite":
        Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    suffix = uuid4().hex
    now = datetime.now(UTC)
    with factory.begin() as db:
        user = User(tenant="test", database=suffix, odoo_uid=1, login=suffix, name=suffix)
        db.add(user)
        db.flush()
        user_id = user.id
        connections = [
            GoogleConnection(
                user_id=user_id, google_sub=f"{suffix}_{account}", email=f"{account}@example.com",
                scopes="https://www.googleapis.com/auth/drive.readonly",
                encrypted_access_token="fixture", status="connected", expires_at=now,
            )
            for account in ("a", "b")
        ]
        db.add_all(connections)
        db.flush()
        account_a, account_b = (connection.id for connection in connections)
        db.add(GoogleAccountSelection(user_id=user_id, active_account_id=account_a))
    try:
        yield factory, user_id, account_a, account_b
    finally:
        with factory.begin() as db:
            db.query(WorkspaceJob).filter_by(user_id=user_id).delete()
            db.query(GoogleAccountSelection).filter_by(user_id=user_id).delete()
            db.query(GoogleConnection).filter_by(user_id=user_id).delete()
            db.query(User).filter_by(id=user_id).delete()
        engine.dispose()


def test_confirm_requires_pending_target(accounts):
    factory, user_id, _, account_b = accounts
    with factory() as db, pytest.raises(SwitchLockError) as error:
        SelectionManager.confirm_switch(db, user_id, account_b, current_version=1)
    assert error.value.code == "SWITCH_NOT_REQUESTED"


@pytest.mark.asyncio
async def test_admitted_read_keeps_its_account_while_draining(accounts, fernet_key, monkeypatch):
    from app.workspace.selection_manager import WorkspaceAccountPin, active_account_pin

    factory, user_id, account_a, account_b = accounts
    with factory() as db:
        SelectionManager.request_switch(db, user_id, account_b)

    oauth = GoogleOAuthService(factory, "test-client", "test-secret", "http://localhost", fernet_key)
    monkeypatch.setattr(
        oauth, "get_valid_access_token", lambda user_id, *, google_account_id: f"token-{google_account_id}",
    )
    google = GoogleClient(oauth)
    seen_tokens = []

    def provider(request):
        seen_tokens.append(request.headers["Authorization"])
        return httpx.Response(200, json={"files": []})

    service = WorkspaceService(google, "test-signer", transport=httpx.MockTransport(provider))
    try:
        with pytest.raises(WorkspaceError) as rejection:
            service.read(user_id, "drive", {"method": "files.list"})
        assert rejection.value.code == "ACCOUNT_DRAINING"

        pin_token = active_account_pin.set(WorkspaceAccountPin(user_id, account_a, 1))
        try:
            assert service.read(user_id, "drive", {"method": "files.list"})["status"] == "OK"
        finally:
            active_account_pin.reset(pin_token)
        assert seen_tokens == [f"Bearer token-{account_a}"]
    finally:
        service.close()
        google.close()
        await google.unified_client.close()


@pytest.mark.parametrize("accounts", ["postgresql"], indirect=True)
def test_postgres_switch_serializes_with_durable_admission(accounts):
    factory, user_id, account_a, account_b = accounts

    def blocked_switch():
        with factory() as competing:
            competing.execute(text("SET LOCAL lock_timeout = '200ms'"))
            with pytest.raises(OperationalError) as failure:
                SelectionManager.request_switch(competing, user_id, account_b)
            return failure.value.orig.sqlstate

    now = datetime.now(UTC)
    with factory.begin() as admitting:
        admitted, _, selection = SelectionManager.check_admission(admitting, user_id)
        assert admitted
        admitting.add(WorkspaceJob(
            job_id=uuid4().hex, user_id=user_id, request_id=uuid4().hex, service="drive",
            operation="files.list", google_account_id=selection.active_account_id,
            selection_version=selection.selection_version, payload_encrypted="{}",
            deadline_at=now + timedelta(minutes=1),
        ))
        with ThreadPoolExecutor(max_workers=1) as threads:
            assert threads.submit(blocked_switch).result(timeout=5) == "55P03"

    with factory() as db:
        assert SelectionManager.request_switch(db, user_id, account_b)["status"] == "busy"
        assert not SelectionManager.check_admission(db, user_id)[0]
        job = db.query(WorkspaceJob).filter_by(user_id=user_id).one()
        assert job.google_account_id == account_a
        job.status = "completed"
        db.commit()

    with factory() as db:
        assert SelectionManager.resume_current(db, user_id)["active_account_id"] == account_a
        assert SelectionManager.request_switch(db, user_id, account_b)["status"] == "ready_to_switch"
        switched = SelectionManager.confirm_switch(db, user_id, account_b, current_version=1)
        assert switched["active_account_id"] == account_b
        assert switched["selection_version"] == 2
