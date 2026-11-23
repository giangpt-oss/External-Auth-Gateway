"""Tests for Stage 3 Phase 2: Error Taxonomy, Circuit Breaker, and Retry Policy for Google & Odoo."""
import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx

from app.google.client import (
    UnifiedGoogleClient,
    GoogleApiError,
    GoogleErrorCode,
)
from app.odoo.client import (
    OdooClient,
    OdooError,
    OdooErrorCode,
    OdooApiError,
    OdooAuthError,
    OdooAccessError,
    OdooNetworkError,
)
from app.workspace.circuit_breaker import AccountServiceCircuitBreaker


# ============================================================================
# 1. Error Taxonomy Tests (Google & Odoo)
# ============================================================================

def test_google_error_taxonomy_coverage():
    """GoogleErrorCode must cover authentication, missing scope, resource permission,
    quota (even on 403), service disabled, invalid params, network error, conflict, and unknown_result.
    """
    required_codes = {
        "AUTH_EXPIRED",
        "RECONNECT_REQUIRED",
        "MISSING_SCOPE",
        "PERMISSION_DENIED",
        "QUOTA_EXCEEDED",
        "SERVICE_DISABLED",
        "INVALID_ARGUMENT",
        "NETWORK_ERROR",
        "CONFLICT",
        "UNKNOWN_RESULT",
    }
    actual_codes = {e.value for e in GoogleErrorCode}
    assert required_codes.issubset(actual_codes)


def test_odoo_error_taxonomy_coverage():
    """OdooErrorCode must classify errors: AUTH_FAILED, ACCESS_DENIED, INACTIVE_USER,
    EXTERNAL_USER, INVALID_PARAMS, NETWORK_ERROR, CONFLICT, UNKNOWN_RESULT.
    """
    required_codes = {
        "AUTH_FAILED",
        "ACCESS_DENIED",
        "INACTIVE_USER",
        "EXTERNAL_USER",
        "INVALID_PARAMS",
        "NETWORK_ERROR",
        "CONFLICT",
        "UNKNOWN_RESULT",
    }
    actual_codes = {e.value for e in OdooErrorCode}
    assert required_codes.issubset(actual_codes)


@pytest.mark.asyncio
async def test_google_client_classifies_service_disabled_and_missing_scope():
    mock_oauth = MagicMock()
    mock_oauth.get_valid_access_token.return_value = "ya29.test_token"
    client = UnifiedGoogleClient(oauth_service=mock_oauth)

    # 403 with ACCESS_TOKEN_SCOPE_INSUFFICIENT
    resp_scope = httpx.Response(
        403,
        json={"error": {"message": "Insufficient Permission: Request had insufficient authentication scopes."}},
        request=httpx.Request("GET", "https://www.googleapis.com/calendar/v3/events"),
    )
    with patch.object(client._http_client, "send", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = resp_scope
        with pytest.raises(GoogleApiError) as exc_info:
            await client.request(user_id=1, method="GET", url="https://www.googleapis.com/calendar/v3/events")
        assert exc_info.value.error_code == GoogleErrorCode.MISSING_SCOPE

    # 403 with Service Not Enabled
    resp_disabled = httpx.Response(
        403,
        json={"error": {"message": "Google Calendar API has not been used in project 123 before or it is disabled."}},
        request=httpx.Request("GET", "https://www.googleapis.com/calendar/v3/events"),
    )
    with patch.object(client._http_client, "send", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = resp_disabled
        with pytest.raises(GoogleApiError) as exc_info:
            await client.request(user_id=1, method="GET", url="https://www.googleapis.com/calendar/v3/events")
        assert exc_info.value.error_code == GoogleErrorCode.SERVICE_DISABLED

    await client.close()


# ============================================================================
# 2. Circuit Breaker per (Account, Service) - 5 consecutive failures, 30s probe
# ============================================================================

def test_circuit_breaker_per_account_and_service():
    """Circuit breaker must track failures per (account_id, service).
    5 consecutive infrastructure failures trip the breaker for 30s.
    """
    cb = AccountServiceCircuitBreaker(failure_threshold=5, recovery_timeout=30.0)
    
    # User 1, Google Calendar
    assert cb.is_available(user_id=1, service="calendar") is True
    
    for _ in range(4):
        cb.record_failure(user_id=1, service="calendar")
        assert cb.is_available(user_id=1, service="calendar") is True
        
    # 5th failure trips the breaker
    cb.record_failure(user_id=1, service="calendar")
    assert cb.is_available(user_id=1, service="calendar") is False
    
    # Other services or other users must NOT be affected!
    assert cb.is_available(user_id=1, service="drive") is True
    assert cb.is_available(user_id=2, service="calendar") is True
    
    # Fast forward past 30 seconds -> half-open probe permitted
    with patch("time.time", return_value=time.time() + 31.0):
        assert cb.is_available(user_id=1, service="calendar") is True
        
        # Probe succeeds -> reset
        cb.record_success(user_id=1, service="calendar")
        assert cb.is_available(user_id=1, service="calendar") is True


# ============================================================================
# 3. Read Retry Policy (3 attempts, backoff 0.5s then 1.0s + jitter) & Write UNKNOWN_RESULT
# ============================================================================

@pytest.mark.asyncio
async def test_read_retry_policy_with_jitter_and_circuit_breaker():
    mock_oauth = MagicMock()
    mock_oauth.get_valid_access_token.return_value = "ya29.test_token"
    cb = AccountServiceCircuitBreaker(failure_threshold=5, recovery_timeout=30.0)
    client = UnifiedGoogleClient(oauth_service=mock_oauth, circuit_breaker=cb)

    # 3 consecutive 503s on read
    resp_503 = httpx.Response(503, json={"error": "backend error"}, request=httpx.Request("GET", "https://google.com"))
    sleep_calls = []
    
    async def mock_sleep(seconds):
        sleep_calls.append(seconds)

    with patch.object(client._http_client, "send", new_callable=AsyncMock) as mock_send, \
         patch("asyncio.sleep", side_effect=mock_sleep):
        mock_send.return_value = resp_503
        
        with pytest.raises(GoogleApiError) as exc_info:
            await client.request(user_id=1, method="GET", url="https://google.com/events", service="calendar")
        
        assert exc_info.value.error_code == GoogleErrorCode.SERVICE_TEMPORARY
        assert mock_send.call_count == 3
        assert len(sleep_calls) == 2
        # Backoff: attempt 1 ~0.5s, attempt 2 ~1.0s
        assert 0.4 <= sleep_calls[0] <= 0.8
        assert 0.9 <= sleep_calls[1] <= 1.4

    await client.close()


@pytest.mark.asyncio
async def test_write_timeout_unknown_result_no_replay():
    mock_oauth = MagicMock()
    mock_oauth.get_valid_access_token.return_value = "ya29.test_token"
    client = UnifiedGoogleClient(oauth_service=mock_oauth)

    with patch.object(client._http_client, "send", new_callable=AsyncMock) as mock_send:
        mock_send.side_effect = httpx.ReadTimeout("timed out waiting for response")
        
        with pytest.raises(GoogleApiError) as exc_info:
            await client.request(
                user_id=1,
                method="POST",
                url="https://google.com/events",
                json_data={"summary": "New Event"},
                is_write=True,
            )
            
        assert exc_info.value.error_code == GoogleErrorCode.UNKNOWN_RESULT
        # Crucial: write operations MUST NOT retry automatically
        assert mock_send.call_count == 1

    await client.close()
