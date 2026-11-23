from time import monotonic
from types import SimpleNamespace

import pytest

from app.workspace.network_slots import NetworkSlots, request_deadline


def test_expired_deadline_prevents_acquiring_an_available_slot():
    slots = NetworkSlots(SimpleNamespace(dialect=SimpleNamespace(name="sqlite")))
    token = request_deadline.set(monotonic() - 1)
    try:
        with pytest.raises(TimeoutError), slots.acquire(1):
            pytest.fail("An expired request must not begin a provider call")
    finally:
        request_deadline.reset(token)

    with slots.acquire(1):
        pass


def test_waiting_for_slot_uses_remaining_budget():
    slots = NetworkSlots(SimpleNamespace(dialect=SimpleNamespace(name="sqlite")))
    waits = []

    def unavailable(*, timeout):
        waits.append(timeout)
        return False

    slots.local = SimpleNamespace(acquire=unavailable)
    token = request_deadline.set(monotonic() + 0.2)
    try:
        with pytest.raises(TimeoutError), slots.acquire(1):
            pytest.fail("The unavailable slot must not be granted")
    finally:
        request_deadline.reset(token)
    assert len(waits) == 1 and 0 < waits[0] <= 0.2
