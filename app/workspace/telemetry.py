"""Detailed timing telemetry for Workspace and Agent execution stages.

Measures:
1. classification_ms: Intent classification
2. queue_wait_ms: Wait time in background job queue
3. token_refresh_ms: OAuth token validation and refresh lease
4. external_call_ms: External API calls (Google / Odoo / MCP)
5. internal_retrieval_ms: Database / FTS / vector retrieval
6. permission_check_ms: Account binding & capability checks
7. llm_synthesis_ms: LLM generation and answer synthesis
"""

from __future__ import annotations

import collections
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
import logging
from threading import Lock
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ExecutionTelemetry:
    request_id: str
    job_id: Optional[str] = None
    operation_id: Optional[str] = None
    service: Optional[str] = None
    operation: Optional[str] = None
    protocol: Optional[str] = None
    
    # Measured durations in milliseconds (None if stage did not execute in this run)
    classification_ms: Optional[float] = None
    queue_wait_ms: Optional[float] = None
    token_refresh_ms: Optional[float] = None
    external_call_ms: Optional[float] = None
    internal_retrieval_ms: Optional[float] = None
    permission_check_ms: Optional[float] = None
    llm_synthesis_ms: Optional[float] = None
    
    total_duration_ms: float = 0.0
    success: bool = True
    error_code: Optional[str] = None
    created_at: float = field(default_factory=time.time)

    def finish(self) -> ExecutionTelemetry:
        if self.total_duration_ms <= 0:
            stages = [
                self.classification_ms,
                self.queue_wait_ms,
                self.token_refresh_ms,
                self.external_call_ms,
                self.internal_retrieval_ms,
                self.permission_check_ms,
                self.llm_synthesis_ms,
            ]
            self.total_duration_ms = round(sum(s for s in stages if s is not None), 2)
        return self


class TelemetryStore:
    """In-memory rolling buffer for stage-by-stage execution telemetry."""

    def __init__(self, max_records: int = 5000):
        self.max_records = max_records
        self._records: collections.deque[ExecutionTelemetry] = collections.deque(maxlen=max_records)

    def record(self, telemetry: ExecutionTelemetry) -> None:
        telemetry.finish()
        self._records.append(telemetry)
        def _fmt(val: Optional[float]) -> str:
            return f"{val:.1f}ms" if val is not None else "-"
        logger.info(
            "TELEMETRY: req_id=%s job_id=%s op_id=%s service=%s op=%s proto=%s "
            "classif=%s queue=%s oauth=%s ext=%s int=%s perm=%s llm=%s total=%.1fms success=%s",
            telemetry.request_id,
            telemetry.job_id or "-",
            telemetry.operation_id or "-",
            telemetry.service or "-",
            telemetry.operation or "-",
            telemetry.protocol or "-",
            _fmt(telemetry.classification_ms),
            _fmt(telemetry.queue_wait_ms),
            _fmt(telemetry.token_refresh_ms),
            _fmt(telemetry.external_call_ms),
            _fmt(telemetry.internal_retrieval_ms),
            _fmt(telemetry.permission_check_ms),
            _fmt(telemetry.llm_synthesis_ms),
            telemetry.total_duration_ms,
            telemetry.success,
        )

    def get_summary(self) -> Dict[str, Any]:
        """Aggregate count, p50 and p95 across measured stages (skipping non-executed stages)."""
        records = list(self._records)
        if not records:
            return {"total_records": 0, "stages": {}}

        def _calc_p(vals: List[float], p: float) -> float:
            if not vals:
                return 0.0
            s = sorted(vals)
            idx = int((len(s) - 1) * p)
            return round(s[idx], 2)

        stage_names = [
            "classification_ms",
            "queue_wait_ms",
            "token_refresh_ms",
            "external_call_ms",
            "internal_retrieval_ms",
            "permission_check_ms",
            "llm_synthesis_ms",
            "total_duration_ms",
        ]
        summary: Dict[str, Any] = {}
        for stage in stage_names:
            vals = [getattr(r, stage) for r in records if getattr(r, stage) is not None]
            if vals:
                summary[stage] = {
                    "count": len(vals),
                    "avg": round(sum(vals) / len(vals), 2),
                    "p50": _calc_p(vals, 0.50),
                    "p95": _calc_p(vals, 0.95),
                }
            else:
                summary[stage] = {"count": 0, "avg": 0.0, "p50": 0.0, "p95": 0.0}

        return {
            "total_records": len(records),
            "stages": summary,
        }


# Global instance
telemetry_store = TelemetryStore()



active_execution: ContextVar[ExecutionTelemetry | None] = ContextVar("workspace_execution", default=None)
_stage_lock = Lock()


@contextmanager
def measure_stage(field: str, *, protocol: str | None = None):
    record = active_execution.get()
    if record is None:
        yield
        return
    if protocol is not None:
        record.protocol = protocol
    started = time.perf_counter()
    try:
        yield
    finally:
        elapsed = (time.perf_counter() - started) * 1000
        with _stage_lock:
            setattr(record, field, (getattr(record, field) or 0.0) + elapsed)
