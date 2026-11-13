import pytest

from app.workspace.protocol_router import (
    ProtocolRouter,
    ProtocolChoice,
)


def test_protocol_router_auto_selection():
    router = ProtocolRouter()
    
    # 1. Operation with valid MCP translation & normal parameters -> MCP
    choice1 = router.select_protocol(
        service="drive",
        method="files.list",
        params={"pageSize": 10},
        body={},
        media=None,
    )
    assert choice1 == ProtocolChoice.MCP
    
    # 2. Upload with media -> REST (MCP doesn't support direct multipart media uploads)
    choice2 = router.select_protocol(
        service="drive",
        method="files.create",
        params={},
        body={"name": "test.png"},
        media={"data": "base64data", "mime_type": "image/png"},
    )
    assert choice2 == ProtocolChoice.REST
    
    # 3. Method not mapped in MCP -> REST
    choice3 = router.select_protocol(
        service="calendar",
        method="calendars.delete",
        params={"calendarId": "cal_123"},
        body={},
        media=None,
    )
    assert choice3 == ProtocolChoice.REST


def test_protocol_router_circuit_breaker_fallback():
    router = ProtocolRouter()
    
    # Trip circuit breaker for drive
    for _ in range(5):
        router.circuit_breaker.record_failure("drive")
        
    assert router.circuit_breaker.is_available("drive") is False
    
    # Even though files.list has MCP mapping, circuit breaker routes it to REST
    choice = router.select_protocol(
        service="drive",
        method="files.list",
        params={"pageSize": 10},
        body={},
        media=None,
    )
    assert choice == ProtocolChoice.REST

