import pytest
from app.workspace.telemetry import ExecutionTelemetry, TelemetryStore


def test_telemetry_recording_and_summary():
    store = TelemetryStore()
    
    for i in range(10):
        t = ExecutionTelemetry(
            request_id=f"req_{i}",
            job_id=f"job_{i}",
            service="calendar",
            operation="events.list",
            protocol="rest",
            classification_ms=10.0 + i,
            queue_wait_ms=5.0,
            token_refresh_ms=2.0,
            external_call_ms=150.0 + i * 10,
            internal_retrieval_ms=1.0,
            permission_check_ms=3.0,
            llm_synthesis_ms=500.0,
        )
        store.record(t)

    summary = store.get_summary()
    assert summary["total_records"] == 10
    assert "external_call_ms" in summary["stages"]
    assert summary["stages"]["external_call_ms"]["p50"] > 150.0
    assert summary["stages"]["total_duration_ms"]["p95"] > 600.0

