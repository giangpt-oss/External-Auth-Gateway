import ast
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from statistics import quantiles
from time import perf_counter
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from alembic import command
from alembic.config import Config
from app.db import create_app_engine, normalize_db_url
from app.models import Base, GoogleConnection, User
from app.workspace.drive_retriever import DriveRetriever
from app.workspace.drive_storage import DriveLink, DriveStorage, IndexedDriveChunk, IndexedDriveFile
from app.workspace.models import DrivePermissionState


@pytest.fixture
def pg_schema():
    raw_url = os.environ.get("STAGE4_POSTGRES_TEST_URL")
    if not raw_url:
        pytest.skip("Requires a disposable PostgreSQL instance with pgvector installed")
    url = make_url(normalize_db_url(raw_url))
    assert url.get_backend_name() == "postgresql"
    root = create_app_engine(url.render_as_string(hide_password=False))
    schema = f"stage4_{uuid4().hex}"
    with root.begin() as db:
        db.execute(text(f"CREATE SCHEMA {schema}"))
    scoped = url.update_query_dict({"options": f"-csearch_path={schema},public"})
    scoped_url = scoped.render_as_string(hide_password=False)
    engine = create_app_engine(scoped_url)
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", scoped_url.replace("%", "%%"))
    try:
        yield config, engine
    finally:
        engine.dispose()
        with root.begin() as db:
            db.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        root.dispose()


def test_stage4_migration_and_hybrid_search(pg_schema):
    config, engine = pg_schema
    with engine.begin() as db:
        Base.metadata.create_all(
            db, tables=[table for table in Base.metadata.sorted_tables if not table.name.startswith("drive_")]
        )
        db.execute(text("ALTER TABLE google_connections DROP COLUMN link_version"))
        db.execute(text("""
            INSERT INTO users (id, tenant, database, odoo_uid, login, name, tz, active, created_at, updated_at)
            VALUES (1, 'test', 'odoo', 1, 'fixture', 'Migration fixture', 'UTC', true, now(), now())
        """))
    command.stamp(config, "c7d8e9f0a1b2")
    command.upgrade(config, "d1e2f3a4b5c6")
    assert "link_version" in {col["name"] for col in inspect(engine).get_columns("google_connections")}
    command.upgrade(config, "head")

    factory = sessionmaker(bind=engine)
    with factory.begin() as db:
        assert db.scalar(text("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")) == 1
        db.add(GoogleConnection(
            user_id=1, google_sub="g1", email="test@example.org", scopes="drive",
            encrypted_access_token="token", expires_at=datetime.now(UTC),
        ))
    key = Fernet.generate_key().decode()
    link = DriveLink(1, "g1", 1)
    storage = DriveStorage(factory, key)
    retriever = DriveRetriever(factory)
    vector = [1.0, *([0.0] * 1535)]
    current = IndexedDriveFile("file", "Hồ sơ", "text/plain", "12")
    assert storage.replace_chunks(
        link, current, [IndexedDriveChunk("Tiến độ dự án", vector, "Trang 4")],
        embedding_model="text-embedding-3-small",
    )
    assert storage.replace_chunks(
        link,
        IndexedDriveFile("file2", "Meeting", "text/plain", "1"),
        [IndexedDriveChunk("Lịch họp", [0.0, *vector[:-1]])],
        embedding_model="text-embedding-3-small",
    )
    results = retriever.search(link, "tiến độ", vector)
    # The orthogonal vector for file2 is deliberately not semantic evidence.
    assert [candidate.file_id for candidate in results] == ["file"]
    assert results[0].page_or_section == "Trang 4"
    assert results[0].score == pytest.approx(2 / 61)
    assert retriever.search(link, "tien do")[0].chunk_id == results[0].chunk_id
    assert retriever.search(link, "Cho tôi hỏi tiến độ thế nào?")[0].chunk_id == results[0].chunk_id
    assert retriever.search(link, "dự án còn bao nhiêu việc", terms=["không trùng", "tiến độ"])
    assert storage.decrypt_candidate(link, results[0], require_permission=True) is None
    with factory.begin() as db:
        db.add(DrivePermissionState(
            user_id=1, google_sub="g1", link_version=1, file_id="file", scope="my_drive",
            file_version="12", checked_at=datetime.now(UTC),
        ))
    assert storage.decrypt_candidate(link, results[0], require_permission=True) == "Tiến độ dự án"
    with factory.begin() as db:
        assert db.scalar(text("SELECT vector_dims(embedding) FROM drive_file_chunks LIMIT 1")) == 1536
        assert db.scalar(text("SELECT tsv::text FROM drive_file_chunks WHERE file_id = 'file'")) == (
            "'an':4 'do':2 'du':3 'tien':1"
        )
        db.execute(text("UPDATE drive_file_metadata SET version = '13' WHERE file_id = 'file'"))
    assert not retriever.search(link, "tien do")
    assert storage.decrypt_candidate(link, results[0]) is None
    assert not storage.replace_chunks(link, current, [], require_metadata=True)
    with factory.begin() as db:
        db.query(GoogleConnection).update({"link_version": 2})
    assert not retriever.search(link, "lich hop", vector)

    command.downgrade(config, "d1e2f3a4b5c6")
    assert not any(table.startswith("drive_") for table in inspect(engine).get_table_names())
    with engine.connect() as db:
        assert db.scalar(text("SELECT name FROM users WHERE id = 1")) == "Migration fixture"
        assert db.scalar(text("SELECT link_version FROM google_connections WHERE user_id = 1")) == 2


def test_stage4_fresh_database_bootstrap(pg_schema):
    _, engine = pg_schema
    environment = {
        **os.environ,
        "APP_ENV": "test",
        "DATABASE_URL": engine.url.render_as_string(hide_password=False),
    }
    subprocess.run([sys.executable, "-m", "app.db_bootstrap"], env=environment, check=True, timeout=30)
    expected = {"drive_file_metadata", "drive_file_chunks", "drive_sync_checkpoints",
                "drive_scan_candidates", "drive_permission_states"}
    assert expected <= set(inspect(engine).get_table_names())
    with engine.connect() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == "e2f3a4b5c6d7"
        assert db.scalar(text("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")) == 1


def _agent_query_terms(agent_root):
    """Load the Agent's pure term extraction without starting its model or importing its app."""
    source = agent_root / "app" / "knowledge" / "drive.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    nodes = [
        node for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name == "drive_query_terms")
        or (isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "STOPWORDS" for target in node.targets))
    ]
    namespace = {"re": re}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
    return namespace["drive_query_terms"]


def test_stage4_evidence_recall(pg_schema):
    _, engine = pg_schema
    agent_root = Path(__file__).resolve().parents[3] / "Module AI - Agent"
    corpus_path = agent_root / "tests" / "evaluation" / "drive_knowledge_60q.json"
    if not corpus_path.is_file():
        pytest.skip("Requires the sibling Agent checkout for the labeled Drive corpus")
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    evidence_cases = [case for case in corpus["cases"] if case["category"] == "evidence"]
    assert len(evidence_cases) == 30
    agent_query_terms = _agent_query_terms(agent_root)

    with engine.begin() as db:
        db.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        Base.metadata.create_all(db)
    factory = sessionmaker(bind=engine)
    with factory.begin() as db:
        db.add(User(
            id=1, tenant="test", database="fixture", odoo_uid=1, login="recall",
            name="Recall fixture",
        ))
        db.flush()
        db.add(GoogleConnection(
            user_id=1, google_sub="fixture", email="test@example.org", scopes="drive",
            encrypted_access_token="fixture", expires_at=datetime.now(UTC),
        ))
    link = DriveLink(1, "fixture", 1)
    storage = DriveStorage(factory, Fernet.generate_key().decode())
    for document in corpus["documents"]:
        file = IndexedDriveFile(document["file_id"], document["name"], "text/plain", "1")
        chunks = [
            IndexedDriveChunk(section["content"], None, section["label"])
            for section in document["sections"]
        ]
        assert storage.replace_chunks(link, file, chunks)

    requests = []
    for case in evidence_cases:
        # For follow-ups, carry the most recent prior user's explicit identifier.
        # This supplies established context without using the labeled source.
        previous_user = next(
            (turn["content"] for turn in reversed(case.get("history", [])) if turn["role"] == "user"), ""
        )
        prior_terms = agent_query_terms(previous_user)[1]
        identifiers = [term for term in prior_terms if any(c.isdigit() for c in term)
                       and any(c.isalpha() for c in term)]
        query = " ".join([*identifiers, case["question"]])
        _, terms = agent_query_terms(query)
        requests.append((case, query, terms))

    retriever = DriveRetriever(factory)
    document_misses = []
    section_misses = []
    for case, query, terms in requests:
        candidates = retriever.search(link, query, terms=terms)
        source = case["expected_source"]
        assert len(candidates) <= 8 and len({row.file_id for row in candidates}) <= 4
        if not any(row.file_id == source["file_id"] for row in candidates):
            document_misses.append(case["id"])
        if not any(
            row.file_id == source["file_id"] and row.page_or_section == source["page_or_section"]
            for row in candidates
        ):
            section_misses.append(case["id"])

    durations_ms = []
    for _ in range(5):
        for _, query, terms in requests:
            started = perf_counter()
            retriever.search(link, query, terms=terms)
            durations_ms.append((perf_counter() - started) * 1000)
    print(json.dumps({
        "method": "PostgreSQL FTS on synthetic indexed sections, with Agent lexical terms",
        "document_hits_at_8": len(requests) - len(document_misses),
        "document_misses": document_misses,
        "section_hits_at_8": len(requests) - len(section_misses),
        "section_misses": section_misses,
        "retrieval_samples": len(durations_ms),
        "internal_retrieval_p95_ms": round(quantiles(durations_ms, n=100, method="inclusive")[94], 2),
    }))
    assert len(document_misses) <= 3
