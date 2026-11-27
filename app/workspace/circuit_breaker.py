"""Distributed / Per-(Account, Service) Circuit Breaker to prevent cascading failures."""
import time


class AccountServiceCircuitBreaker:
    """Circuit breaker scoped to (user_id/account, service) pairs.

    Prevents cascading infrastructure outages by tripping after `failure_threshold`
    consecutive failures, entering an open state for `recovery_timeout` seconds,
    and then allowing a half-open probe.
    """
    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 30.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._failures: dict[tuple[int, str], int] = {}
        self._tripped_until: dict[tuple[int, str], float] = {}

    def _key(self, user_id: int | tuple[int, int], service: str) -> tuple[int | tuple[int, int], str]:
        return (user_id, service.lower())

    def record_failure(self, user_id: int | tuple[int, int], service: str) -> None:
        key = self._key(user_id, service)
        self._failures[key] = self._failures.get(key, 0) + 1
        if self._failures[key] >= self.failure_threshold:
            self._tripped_until[key] = time.time() + self.recovery_timeout

    def record_success(self, user_id: int | tuple[int, int], service: str) -> None:
        key = self._key(user_id, service)
        self._failures[key] = 0
        self._tripped_until.pop(key, None)

    def is_available(self, user_id: int | tuple[int, int], service: str) -> bool:
        key = self._key(user_id, service)
        tripped_time = self._tripped_until.get(key)
        if tripped_time is None:
            return True
        now = time.time()
        if now >= tripped_time:
            # Half-open: allow a probe call through
            return True
        return False
