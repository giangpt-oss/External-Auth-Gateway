import asyncio
from datetime import datetime, timedelta, UTC
import pytest
from unittest.mock import MagicMock, patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, User, GoogleConnection
from app.workspace.models import OAuthRefreshLease
from app.auth.oauth_lease import DistributedOAuthLeaseManager


@pytest.fixture
def db_session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    
    with session_factory() as session:
        user = User(
            tenant="https://odoo.local",
            database="odoo_db",
            odoo_uid=1,
            login="test@company.com",
            name="Test User",
        )
        session.add(user)
        session.commit()
        
        # Add Google connection with token expiring in 2 minutes (< 5 minutes threshold)
        conn = GoogleConnection(
            user_id=1,
            google_sub="sub_123",
            email="test@gmail.com",
            scopes="https://www.googleapis.com/auth/calendar",
            encrypted_access_token="enc_old_access_token",
            encrypted_refresh_token="enc_refresh_token",
            expires_at=datetime.now(UTC) + timedelta(minutes=2),
            status="connected",
        )
        session.add(conn)
        session.commit()
        
    return session_factory


@pytest.mark.asyncio
async def test_acquire_token_safe_thundering_herd_prevention(db_session_factory):
    mock_oauth_service = MagicMock()
    mock_oauth_service.session_factory = db_session_factory
    mock_oauth_service._decrypt.side_effect = lambda x: x[4:] if x.startswith("enc_") else x
    mock_oauth_service._encrypt.side_effect = lambda x: f"enc_{x}"
    
    refresh_calls = 0
    
    def fake_perform_refresh(user_id):
        nonlocal refresh_calls
        refresh_calls += 1
        return "new_access_token_777", 3600
        
    mock_oauth_service._perform_network_refresh = fake_perform_refresh

    manager1 = DistributedOAuthLeaseManager(mock_oauth_service, worker_id="worker_A")
    manager2 = DistributedOAuthLeaseManager(mock_oauth_service, worker_id="worker_B")
    
    # 5 concurrent workers ask for token of the expiring user at the same moment
    results = await asyncio.gather(
        manager1.get_valid_token(1),
        manager2.get_valid_token(1),
        manager1.get_valid_token(1),
        manager2.get_valid_token(1),
        manager1.get_valid_token(1),
    )
    
    # All 5 workers must receive the new token
    for res in results:
        assert res == "new_access_token_777"
        
    # Crucial assertion: exactly 1 network refresh must happen, not 5!
    assert refresh_calls == 1


@pytest.mark.asyncio
async def test_force_refresh_invalidates_old_token(db_session_factory):
    mock_oauth_service = MagicMock()
    mock_oauth_service.session_factory = db_session_factory
    mock_oauth_service._decrypt.side_effect = lambda x: x[4:] if x.startswith("enc_") else x
    mock_oauth_service._encrypt.side_effect = lambda x: f"enc_{x}"
    mock_oauth_service._perform_network_refresh.return_value = ("forced_new_token_888", 3600)
    
    manager = DistributedOAuthLeaseManager(mock_oauth_service, worker_id="worker_A")
    token = await manager.get_valid_token(1, force_refresh=True)
    
    assert token == "forced_new_token_888"
    mock_oauth_service._perform_network_refresh.assert_called_once_with(1)


@pytest.mark.asyncio
async def test_reconnect_during_refresh_invalidates_stale_update(db_session_factory):
    mock_oauth_service = MagicMock()
    mock_oauth_service.session_factory = db_session_factory
    mock_oauth_service._decrypt.side_effect = lambda x: x[4:] if x.startswith("enc_") else x
    mock_oauth_service._encrypt.side_effect = lambda x: f"enc_{x}"
    
    # Simulate user disconnecting right as network refresh completes
    def fake_refresh_with_disconnect(user_id):
        with db_session_factory() as session:
            conn = session.query(GoogleConnection).filter_by(user_id=user_id).first()
            conn.status = "disconnected"
            session.commit()
        return "stale_token_after_disconnect", 3600

    mock_oauth_service._perform_network_refresh = fake_refresh_with_disconnect
    manager = DistributedOAuthLeaseManager(mock_oauth_service, worker_id="worker_A")
    
    from app.google.oauth import GoogleAuthRevokedError
    with pytest.raises(GoogleAuthRevokedError):
        await manager.get_valid_token(1, force_refresh=True)
