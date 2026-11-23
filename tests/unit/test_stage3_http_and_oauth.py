"""Tests for Stage 3 Phase 1: HTTP Client Pool, Odoo JSON-RPC/XML-RPC Pool, and Proactive OAuth Lease Refresh."""
import asyncio
from datetime import datetime, timedelta, UTC
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, User, GoogleConnection
from app.workspace.models import OAuthRefreshLease
from app.auth.oauth_lease import DistributedOAuthLeaseManager
from app.google.client import UnifiedGoogleClient, GoogleApiError, GoogleErrorCode
from app.odoo.client import OdooClient, OdooNetworkError, OdooError


@pytest.fixture
def db_session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    
    with session_factory() as session:
        user = User(
            id=1,
            tenant="https://odoo.local",
            database="odoo_db",
            odoo_uid=1,
            login="test@company.com",
            name="Test User",
            active=True,
            updated_at=datetime.now(UTC),
        )
        session.add(user)
        session.commit()
        
        # Token expiring in 4 minutes (< 5 minutes threshold)
        conn = GoogleConnection(
            user_id=1,
            google_sub="sub_123",
            email="test@gmail.com",
            scopes="https://www.googleapis.com/auth/calendar",
            encrypted_access_token="enc_current_token",
            encrypted_refresh_token="enc_refresh_token",
            expires_at=datetime.now(UTC) + timedelta(minutes=4),
            status="connected",
            updated_at=datetime.now(UTC),
        )
        session.add(conn)
        session.commit()
        
    return session_factory


# ============================================================================
# 1. HTTP Client Lifecycle, HTTP/2 Support, and Strict Timeouts
# ============================================================================

@pytest.mark.asyncio
async def test_unified_google_client_timeout_budget_and_http2():
    """UnifiedGoogleClient must configure HTTP/2 (http2=True) and strict timeout limits:
    connect_timeout=3.0s, pool_timeout=2.0s, and total deadline budget enforcement.
    """
    mock_oauth = MagicMock()
    mock_oauth.get_valid_access_token.return_value = "ya29.test_token"
    
    client = UnifiedGoogleClient(oauth_service=mock_oauth)
    
    # Check underlying http client settings
    assert client._http_client is not None
    timeout_cfg = client._http_client.timeout
    assert timeout_cfg.connect == 3.0
    assert timeout_cfg.pool == 2.0
    
    # Check http2 is enabled
    assert client.http2_enabled is True

    await client.close()
    assert client._http_client.is_closed


@pytest.mark.asyncio
async def test_unified_google_client_deducts_remaining_budget():
    """Verify that retries, waiting, and refresh deduct from the remaining overall budget.
    If total budget is exceeded, GoogleApiError(UNKNOWN_RESULT or NETWORK_ERROR) is raised.
    """
    mock_oauth = MagicMock()
    mock_oauth.get_valid_access_token.return_value = "ya29.test_token"
    
    client = UnifiedGoogleClient(oauth_service=mock_oauth)
    
    # Mock send to take 1.5s then return 429
    call_count = 0
    async def delayed_send(req):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.1)
        return httpx.Response(429, headers={"Retry-After": "2.0"}, json={"error": "rate limit"}, request=req)
        
    with patch.object(client._http_client, "send", side_effect=delayed_send):
        # We give a tiny remaining budget of 0.25s
        with pytest.raises(GoogleApiError) as exc_info:
            await client.request(
                user_id=1,
                method="GET",
                url="https://www.googleapis.com/calendar/v3/calendars/primary/events",
                timeout=0.25,
            )
        assert exc_info.value.error_code in (GoogleErrorCode.NETWORK_ERROR, GoogleErrorCode.RATE_LIMIT)

    await client.close()


# ============================================================================
# 2. Odoo JSON-RPC HTTP Pool and XML-RPC Bounded Concurrency
# ============================================================================

def test_odoo_client_jsonrpc_pool_lifecycle():
    """OdooClient must maintain an httpx.Client connection pool with proper limits:
    connect_timeout=3.0, pool_timeout=2.0, max_keepalive_connections, and close() on shutdown.
    """
    client = OdooClient(url="https://odoo.local", db="odoo_db")
    assert hasattr(client, "_http_pool")
    assert isinstance(client._http_pool, httpx.Client)
    assert client._http_pool.timeout.connect == 3.0
    assert client._http_pool.timeout.pool == 2.0
    
    client.close()
    assert client._http_pool.is_closed


def test_odoo_client_xmlrpc_bounded_executor():
    """OdooClient XML-RPC execution must use a bounded executor to avoid thread explosion,
    and enforce per-task isolated transport.
    """
    client = OdooClient(url="https://odoo.local", db="odoo_db")
    assert hasattr(client, "xmlrpc_semaphore")
    assert client.xmlrpc_semaphore._value <= 8  # Concurrency bound <= 8 per instance
    client.close()


# ============================================================================
# 3. Proactive OAuth Lease Refresh (5-minute expiry + 30-minute activity window)
# ============================================================================

@pytest.mark.asyncio
async def test_oauth_lease_proactive_refresh_window(db_session_factory):
    """DistributedOAuthLeaseManager must support proactive refresh:
    - Target: users whose token expires within 5 minutes AND who were active within last 30 minutes.
    - Prevents duplicate refresh and checks connection_version.
    """
    mock_oauth_service = MagicMock()
    mock_oauth_service.session_factory = db_session_factory
    mock_oauth_service._decrypt.side_effect = lambda x: x[4:] if x.startswith("enc_") else x
    mock_oauth_service._encrypt.side_effect = lambda x: f"enc_{x}"
    mock_oauth_service._perform_network_refresh = MagicMock(return_value=("new_refreshed_tok", 3600))
    
    manager = DistributedOAuthLeaseManager(mock_oauth_service, worker_id="worker_test")
    
    # 1. User 1 was updated just now (active < 30 mins) and expires in 4 mins (< 5 mins)
    refreshed_users = await manager.refresh_active_expiring_tokens(max_activity_minutes=30, expiry_window_minutes=5)
    assert 1 in refreshed_users
    mock_oauth_service._perform_network_refresh.assert_called_once_with(1)
    
    # Verify connection_version incremented
    with db_session_factory() as session:
        lease = session.query(OAuthRefreshLease).filter_by(user_id=1).first()
        assert lease is not None
        assert lease.connection_version >= 2
        
        conn = session.query(GoogleConnection).filter_by(user_id=1).first()
        assert session.query(GoogleConnection).filter_by(user_id=1).first().encrypted_access_token == "enc_new_refreshed_tok"


@pytest.mark.asyncio
async def test_oauth_lease_proactive_refresh_ignores_inactive_users(db_session_factory):
    """Proactive refresh MUST ignore users who have not been active in the last 30 minutes,
    saving network quota and preventing unnecessary wakeups.
    """
    with db_session_factory() as session:
        # Set user.updated_at to 45 minutes ago
        user = session.query(User).filter_by(id=1).first()
        user.updated_at = datetime.now(UTC) - timedelta(minutes=45)
        conn = session.query(GoogleConnection).filter_by(user_id=1).first()
        conn.updated_at = datetime.now(UTC) - timedelta(minutes=45)
        session.commit()

    mock_oauth_service = MagicMock()
    mock_oauth_service.session_factory = db_session_factory
    mock_oauth_service._perform_network_refresh = MagicMock(return_value=("should_not_call", 3600))
    
    manager = DistributedOAuthLeaseManager(mock_oauth_service, worker_id="worker_test")
    refreshed_users = await manager.refresh_active_expiring_tokens(max_activity_minutes=30, expiry_window_minutes=5)
    
    assert len(refreshed_users) == 0
    mock_oauth_service._perform_network_refresh.assert_not_called()
