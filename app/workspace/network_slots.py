"""Cross-process limits for outbound Workspace requests."""

import hashlib
import time
from contextlib import contextmanager
from contextvars import ContextVar
from threading import BoundedSemaphore

from sqlalchemy import text


network_slots: ContextVar[object | None] = ContextVar("workspace_network_slots", default=None)
outbound_user_id: ContextVar[int | None] = ContextVar("workspace_outbound_user", default=None)


def _key(*parts):
    value = ":".join(map(str, parts)).encode()
    return int.from_bytes(hashlib.blake2b(value, digest_size=8).digest(), "big", signed=True)


class NetworkSlots:
    def __init__(self, engine, *, background=False):
        self.engine = engine
        self.background = background
        self.local = BoundedSemaphore(2 if background else 8)

    @contextmanager
    def acquire(self, user_id, write=False):
        if not self.local.acquire(timeout=2):
            raise TimeoutError("Workspace worker network slots busy")
        try:
            if self.engine.dialect.name != "postgresql":
                yield
                return
            groups = []
            if self.background:
                groups.append([_key("workspace-background", user_id)])
            groups.append([_key("workspace-user", user_id, write, i) for i in range(1 if write else 4)])
            groups.append([_key("workspace-global", i) for i in (range(14, 16) if self.background else range(16))])
            end = time.monotonic() + 2
            with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                while True:
                    acquired = []
                    try:
                        for group in groups:
                            for key in group:
                                if conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}).scalar_one():
                                    acquired.append(key)
                                    break
                            else:
                                break
                        if len(acquired) == len(groups):
                            yield
                            return
                    finally:
                        try:
                            for key in reversed(acquired):
                                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                        except Exception:
                            conn.invalidate()
                            raise
                    if time.monotonic() >= end:
                        raise TimeoutError("Workspace account or global network slots busy")
                    time.sleep(0.025)
        finally:
            self.local.release()


@contextmanager
def outbound_slot(user_id, *, write=False):
    limiter = network_slots.get()
    if limiter is None or user_id is None:
        yield
    else:
        with limiter.acquire(user_id, write):
            yield
