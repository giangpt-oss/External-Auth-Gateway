"""Observability metrics collector for Google Workspace operations."""

from __future__ import annotations

import collections
from typing import Any, Dict, List, Optional
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.workspace.mcp_client import CircuitBreaker
from app.workspace.models import WorkspaceJob


def _percentile(data: List[float], percentile: float) -> float:
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * percentile
    f = int(k)
    c = f + 1
    if c < len(sorted_data):
        d0 = sorted_data[f] * (c - k)
        d1 = sorted_data[c] * (k - f)
        return round(d0 + d1, 2)
    return round(sorted_data[f], 2)


class WorkspaceMetricsCollector:
    """In-memory rolling metrics collector and database queue aggregator."""

    def __init__(self, max_samples: int = 1000):
        self.max_samples = max_samples
        self._latencies: collections.deque[float] = collections.deque(maxlen=max_samples)
        self._total_requests = 0
        self._success_count = 0
        self._failure_count = 0
        self._protocols = collections.Counter()
        self._services = collections.Counter()

    def record_request(
        self,
        service: str,
        protocol: str,
        duration_ms: float,
        is_success: bool,
    ) -> None:
        self._latencies.append(duration_ms)
        self._total_requests += 1
        if is_success:
            self._success_count += 1
        else:
            self._failure_count += 1
        self._protocols[protocol] += 1
        self._services[service] += 1

    def get_metrics(
        self,
        db: Session,
        circuit_breaker: Optional[CircuitBreaker] = None,
    ) -> Dict[str, Any]:
        """Aggregate in-memory request stats and database job counts."""
        latencies = list(self._latencies)
        p50 = _percentile(latencies, 0.50)
        p95 = _percentile(latencies, 0.95)

        # Database job counts by status
        job_counts: Dict[str, int] = collections.defaultdict(int)
        try:
            results = (
                db.query(WorkspaceJob.status, func.count(WorkspaceJob.id))
                .group_by(WorkspaceJob.status)
                .all()
            )
            for status, count in results:
                job_counts[status] = count
        except Exception:
            pass

        # Circuit breaker status per service
        cb_status: Dict[str, bool] = {}
        if circuit_breaker:
            for s in ("gmail", "calendar", "drive", "docs", "sheets", "slides"):
                cb_status[s] = circuit_breaker.is_available(s)

        success_rate = (
            round((self._success_count / self._total_requests) * 100, 2)
            if self._total_requests > 0
            else 100.0
        )

        return {
            "total_requests": self._total_requests,
            "success_rate_percent": success_rate,
            "latency_ms": {
                "p50": p50,
                "p95": p95,
                "samples": len(latencies),
            },
            "protocols": dict(self._protocols),
            "services": dict(self._services),
            "jobs_by_status": dict(job_counts),
            "circuit_breakers": cb_status,
        }


# Global singleton instance
workspace_metrics = WorkspaceMetricsCollector()

