import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx

from app.google.client import (
    UnifiedGoogleClient,
    GoogleApiError,
    GoogleErrorCode,
)


@pytest.fixture
def mock_oauth_service():
    service = MagicMock()
    service.get_valid_access_token.return_value = "ya29.initial_test_token"
    service.get_user_google_connection.return_value = MagicMock(status="connected", scopes="https://www.googleapis.com/auth/calendar")
    return service


@pytest.mark.asyncio
async def test_unified_client_successful_get(mock_oauth_service):
    client = UnifiedGoogleClient(oauth_service=mock_oauth_service)
    
    mock_resp = httpx.Response(
        status_code=200,
        json={"items": [{"id": "evt1", "summary": "Meeting"}]},
        request=httpx.Request("GET", "https://www.googleapis.com/calendar/v3/calendars/primary/events"),
    )
    
    with patch.object(client._http_client, "send", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = mock_resp
        
        data = await client.request(
            user_id=1,
            method="GET",
            url="https://www.googleapis.com/calendar/v3/calendars/primary/events",
            is_write=False,
        )
        
        assert data == {"items": [{"id": "evt1", "summary": "Meeting"}]}
        mock_send.assert_called_once()
        req = mock_send.call_args[0][0]
        assert req.headers["Authorization"] == "Bearer ya29.initial_test_token"
    await client.close()


@pytest.mark.asyncio
async def test_unified_client_401_force_refresh_retry(mock_oauth_service):
    client = UnifiedGoogleClient(oauth_service=mock_oauth_service)
    
    resp_401 = httpx.Response(
        status_code=401,
        json={"error": {"message": "Invalid Credentials", "status": "UNAUTHENTICATED"}},
        request=httpx.Request("GET", "https://www.googleapis.com/calendar/v3/calendars/primary/events"),
    )
    resp_200 = httpx.Response(
        status_code=200,
        json={"items": []},
        request=httpx.Request("GET", "https://www.googleapis.com/calendar/v3/calendars/primary/events"),
    )
    
    # After 401, token refresh returns new token
    mock_oauth_service.get_valid_access_token.side_effect = [
        "ya29.initial_test_token",
        "ya29.refreshed_test_token",
    ]
    
    with patch.object(client._http_client, "send", new_callable=AsyncMock) as mock_send:
        mock_send.side_effect = [resp_401, resp_200]
        
        data = await client.request(
            user_id=1,
            method="GET",
            url="https://www.googleapis.com/calendar/v3/calendars/primary/events",
            is_write=False,
        )
        
        assert data == {"items": []}
        assert mock_send.call_count == 2
        # Second call must use the refreshed token
        second_req = mock_send.call_args_list[1][0][0]
        assert second_req.headers["Authorization"] == "Bearer ya29.refreshed_test_token"
        assert mock_oauth_service.get_valid_access_token.call_count == 2
        # Ensure force_refresh=True was requested on second call
        mock_oauth_service.get_valid_access_token.assert_called_with(1, force_refresh=True)
    await client.close()


@pytest.mark.asyncio
async def test_unified_client_read_retry_on_429_with_retry_after(mock_oauth_service):
    client = UnifiedGoogleClient(oauth_service=mock_oauth_service)
    
    resp_429 = httpx.Response(
        status_code=429,
        headers={"Retry-After": "0.1"},
        json={"error": {"message": "Rate limit exceeded"}},
        request=httpx.Request("GET", "https://www.googleapis.com/calendar/v3/calendars/primary/events"),
    )
    resp_200 = httpx.Response(
        status_code=200,
        json={"items": [{"id": "ok"}]},
        request=httpx.Request("GET", "https://www.googleapis.com/calendar/v3/calendars/primary/events"),
    )
    
    with patch.object(client._http_client, "send", new_callable=AsyncMock) as mock_send, \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_send.side_effect = [resp_429, resp_200]
        
        data = await client.request(
            user_id=1,
            method="GET",
            url="https://www.googleapis.com/calendar/v3/calendars/primary/events",
            is_write=False,
        )
        
        assert data == {"items": [{"id": "ok"}]}
        assert mock_send.call_count == 2
        mock_sleep.assert_called_once()
    await client.close()


@pytest.mark.asyncio
async def test_unified_client_write_no_automatic_retry_on_5xx(mock_oauth_service):
    client = UnifiedGoogleClient(oauth_service=mock_oauth_service)
    
    resp_503 = httpx.Response(
        status_code=503,
        json={"error": {"message": "Backend Error"}},
        request=httpx.Request("POST", "https://www.googleapis.com/calendar/v3/calendars/primary/events"),
    )
    
    with patch.object(client._http_client, "send", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = resp_503
        
        with pytest.raises(GoogleApiError) as exc_info:
            await client.request(
                user_id=1,
                method="POST",
                url="https://www.googleapis.com/calendar/v3/calendars/primary/events",
                json_data={"summary": "New Event"},
                is_write=True,
            )
        
        assert exc_info.value.error_code == GoogleErrorCode.UNKNOWN_RESULT
        assert exc_info.value.status_code == 503
        # Writes must NOT be automatically retried when result is uncertain
        assert mock_send.call_count == 1
    await client.close()


@pytest.mark.asyncio
async def test_unified_client_per_user_rate_limiting(mock_oauth_service):
    client = UnifiedGoogleClient(oauth_service=mock_oauth_service)
    
    active_writes = 0
    max_concurrent_writes = 0
    
    async def fake_slow_write(*args, **kwargs):
        nonlocal active_writes, max_concurrent_writes
        active_writes += 1
        max_concurrent_writes = max(max_concurrent_writes, active_writes)
        await asyncio.sleep(0.05)
        active_writes -= 1
        return httpx.Response(
            status_code=200,
            json={"id": "done"},
            request=httpx.Request("POST", "https://www.googleapis.com/calendar/v3/calendars/primary/events"),
        )
    
    with patch.object(client._http_client, "send", side_effect=fake_slow_write):
        # Dispatch 3 concurrent writes for user 1
        tasks = [
            client.request(
                user_id=1,
                method="POST",
                url="https://www.googleapis.com/calendar/v3/calendars/primary/events",
                json_data={"summary": f"Event {i}"},
                is_write=True,
            )
            for i in range(3)
        ]
        results = await asyncio.gather(*tasks)
        assert len(results) == 3
        # Strict per-user write limit: exactly 1 concurrent write per user
        assert max_concurrent_writes == 1
    await client.close()

