from datetime import UTC, datetime, timedelta
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.workspace.mcp_client import CircuitBreaker
from app.workspace.metrics import WorkspaceMetricsCollector
from app.workspace.models import WorkspaceJob, WorkspaceJobStatus


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def test_metrics_collector(db_session):
    collector = WorkspaceMetricsCollector()
    cb = CircuitBreaker()
    
    # Record some latencies
    for d in [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]:
        collector.record_request("calendar", "rest", d, True)
    collector.record_request("drive", "mcp", 120.0, True)
    collector.record_request("gmail", "mcp", 500.0, False)

    # Insert a job into db
    job = WorkspaceJob(
        job_id="job_metrics_1",
        user_id=1,
        request_id="req_m_1",
        service="calendar",
        operation="events.list",
        status=WorkspaceJobStatus.QUEUED.value,
        payload_encrypted="{}",
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    db_session.add(job)
    db_session.commit()

    metrics = collector.get_metrics(db_session, cb)
    
    assert metrics["total_requests"] == 12
    assert metrics["protocols"]["rest"] == 10
    assert metrics["protocols"]["mcp"] == 2
    assert metrics["latency_ms"]["p50"] > 0
    assert metrics["latency_ms"]["p95"] > metrics["latency_ms"]["p50"]
    assert metrics["jobs_by_status"]["queued"] >= 1
    assert "gmail" in metrics["circuit_breakers"]
