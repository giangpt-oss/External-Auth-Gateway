"""Exercise the Gateway job API, workers, and Drive index on disposable PostgreSQL.

Run from the Gateway repo with python -m scripts.stage4_drive_load. Set
STAGE4_POSTGRES_TEST_URL to a disposable local PostgreSQL 16 server with pgvector
and database creation privileges. The harness creates and drops its own database.
External Drive, embedding, and Agent responses are synthetic.
"""

import argparse
import asyncio
import base64
import json
import logging
import math
import os
import time
from collections import Counter
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from threading import Lock
from types import SimpleNamespace
from uuid import uuid4

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import create_postgres_engine, normalize_db_url
from app.models import Base, GoogleConnection, TelegramConnection, TelegramUpdate, User
from app.workspace.drive_storage import DriveLink, DriveStorage, IndexedDriveChunk, IndexedDriveFile
from app.workspace.models import DriveFileMetadata, DriveSyncCheckpoint
from app.workspace.network_slots import outbound_slot
from app.workspace.parsers import PARSER_VERSION
from app.workspace.routes import router
from app.workspace.worker import WorkspaceWorker

VECTOR = [1.0] + [0.0] * 1535
QUESTION = "Số lượng tồn kho PN22"
USERS = 50
FILE_ID = "inventory"
SYNC_FILE = "updates"


def marker(user_id):
    return f"OWN-{user_id:04d}"


def percentile(samples, fraction=0.95):
    if not samples:
        return None
    return round(sorted(samples)[math.ceil(len(samples) * fraction) - 1], 2)


class SyntheticDrive:
    def __init__(self, latency):
        self.latency = latency
        self.google = self
        self.versions = Counter()
        self.calls = Counter()
        self.lock = Lock()

    def get_user_google_connection(self, user_id):
        return SimpleNamespace(status="connected", google_sub=f"test-{user_id}", link_version=1)

    def close(self):
        """The synthetic provider has no persistent connection."""

    def metadata(self, user_id, file_id):
        version = "1" if file_id == FILE_ID else str(self.versions[user_id])
        return {
            "id": file_id, "name": f"{file_id}.txt", "mimeType": "text/plain", "version": version,
            "modifiedTime": datetime.now(UTC).isoformat(), "trashed": False,
            "capabilities": {"canDownload": True},
        }

    def read(self, user_id, service, request, *, force_rest=False):
        method, parameters = request["method"], request["parameters"]
        assert service == "drive" and force_rest
        with outbound_slot(user_id):
            time.sleep(self.latency)
            with self.lock:
                self.calls[method] += 1
                if method == "changes.list":
                    self.versions[user_id] = int(parameters["pageToken"]) + 1
                    return {"data": {
                        "newStartPageToken": str(self.versions[user_id]),
                        "changes": [{"fileId": SYNC_FILE, "file": self.metadata(user_id, SYNC_FILE)}],
                    }}
                if method == "files.list":
                    # The fallback path searches Drive before choosing at most four files.
                    # Return the seeded indexed file so the load test exercises that real path.
                    return {"data": {"files": [self.metadata(user_id, FILE_ID)]}}
                if method != "files.get":
                    raise AssertionError(f"Unexpected synthetic Drive method {method}")
                file_id = parameters["fileId"]
                if parameters.get("alt") == "media":
                    self.calls["media"] += 1
                    content = f"Biên bản đồng bộ nội bộ cho {marker(user_id)}.".encode()
                    return {"data": {"content_base64": base64.b64encode(content).decode()}}
                self.calls[f"metadata:{file_id}"] += 1
                return {"data": self.metadata(user_id, file_id)}


def seed(factory, settings):
    now = datetime.now(UTC)
    with factory.begin() as db:
        db.add_all([
            User(id=uid, tenant="load", database="load", odoo_uid=uid, login=f"load-{uid}",
                 name=f"Load {uid}", active=True)
            for uid in range(1, USERS + 1)
        ])
        db.flush()
        for uid in range(1, USERS + 1):
            db.add(GoogleConnection(
                user_id=uid, google_sub=f"test-{uid}", email=f"load-{uid}@example.org", scopes="drive",
                expires_at=now + timedelta(hours=2), status="connected", encrypted_access_token="synthetic",
            ))
            db.add(TelegramConnection(
                user_id=uid, telegram_user_id=10000 + uid, private_chat_id=10000 + uid,
                binding_id=f"load-{uid}", status="connected",
            ))
            db.add(DriveSyncCheckpoint(
                user_id=uid, google_sub=f"test-{uid}", link_version=1, scope="my_drive",
                scan_state="complete", start_page_token="0", last_full_scan_at=now,
                last_sync_at=now - timedelta(minutes=5), last_success_at=now,
            ))
    storage = DriveStorage(factory, settings.ENCRYPTION_KEY)
    for uid in range(1, USERS + 1):
        link = DriveLink(uid, f"test-{uid}", 1)
        file = IndexedDriveFile(FILE_ID, "Tồn kho.txt", "text/plain", "1", modified_time=now)
        assert storage.replace_chunks(
            link, file, [IndexedDriveChunk(f"Số lượng tồn kho PN22. Mã hồ sơ {marker(uid)}.", VECTOR, "Trang 1")],
            embedding_model=settings.EMBEDDING_MODEL, parser_version=PARSER_VERSION,
        )


class Run:
    def __init__(self, args, engine):
        self.args = args
        self.factory = sessionmaker(engine)
        self.settings = Settings(
            _env_file=None, APP_ENV="test", DATABASE_URL="sqlite://", DRIVE_INDEX_ENABLED=True,
            ENCRYPTION_KEY=Fernet.generate_key().decode(), GOOGLE_WORKSPACE_MCP_ENABLED=False,
            AGENT_TO_GATEWAY_SECRET="synthetic-gateway-secret-for-load-test",
            GATEWAY_TO_AGENT_SECRET="synthetic-agent-secret-for-load-test",
            AI_AGENT_BASE_URL="https://agent.example.invalid", EMBEDDING_API_KEY="synthetic",
        )
        self.drive = SyntheticDrive(args.drive_ms / 1000)
        self.expected = {}
        self.api = FastAPI()
        self.api.state.settings = self.settings
        self.api.state.session_factory = self.factory
        self.api.include_router(router)
        self.sync_ticks = 0
        self.sync_errors = 0
        self.failed_ownership_checks = 0
        self.retrieval_ms = []
        self.permission_ms = []
        self.embedding_ms = []

    async def external(self, request):
        if request.url.path == "/v1/embeddings":
            started = time.perf_counter()
            await asyncio.sleep(self.args.embedding_ms / 1000)
            batch = json.loads(request.content)["input"]
            self.embedding_ms.append((time.perf_counter() - started) * 1000)
            return httpx.Response(200, json={"data": [
                {"index": index, "embedding": VECTOR} for index in range(len(batch))
            ]})
        if request.url.path != "/internal/v1/workspace/job-completed":
            raise AssertionError(f"Unexpected mock transport path {request.url.path}")
        callback = json.loads(request.content)
        uid, outcome = self.expected.get(callback["request_id"], (None, None))
        if outcome is None:
            return httpx.Response(200, json={"messages": []})
        result = callback.get("result") or {}
        chunks = result.get("chunks", [])
        owned = uid == callback["user_id"] and uid is not None and all(
            marker(uid) in chunk["content"] for chunk in chunks
        )
        if not owned:
            self.failed_ownership_checks += 1
        hit = any(chunk["file_id"] == FILE_ID for chunk in chunks)
        await asyncio.sleep(self.args.agent_ms / 1000)
        reason = None
        if not owned:
            reason = "wrong_owner"
        elif callback["status"] != "completed" or result.get("status") != "ok" or not hit:
            reason = callback.get("error_code") or result.get("status") or callback["status"]
        if not outcome.done():
            outcome.set_result(reason)
        return httpx.Response(200, json={"messages": [] if reason else ["Synthetic sourced answer."]})

    def instrument(self, worker):
        runtime = worker.drive_runtime
        search = runtime.retriever.search
        permission = runtime.permissions.filter

        def retrieve(*args, **kwargs):
            started = time.perf_counter()
            try:
                return search(*args, **kwargs)
            finally:
                self.retrieval_ms.append((time.perf_counter() - started) * 1000)

        async def check(*args, **kwargs):
            started = time.perf_counter()
            try:
                return await permission(*args, **kwargs)
            finally:
                self.permission_ms.append((time.perf_counter() - started) * 1000)

        runtime.retriever.search = retrieve
        runtime.permissions.filter = check

    async def sync(self, background):
        while True:
            try:
                await background.drive_runtime.tick(background.network_slots)
                self.sync_ticks += 1
            except Exception:
                self.sync_errors += 1
                logging.exception("Background Drive tick failed")
            await asyncio.sleep(2)

    def counts(self):
        with self.factory() as db:
            return {
                "queued_deliveries": db.scalar(select(func.count()).select_from(TelegramUpdate)),
                "deadlocks": db.scalar(text("SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()")),
                "indexed_sync_files": db.scalar(select(func.count()).select_from(DriveFileMetadata).where(
                    DriveFileMetadata.file_id == SYNC_FILE, DriveFileMetadata.status == "indexed",
                )),
            }

    async def query(self, client, uid, result, status_checks):
        started = time.perf_counter()
        request_id = uuid4().hex
        outcome = asyncio.get_running_loop().create_future()
        self.expected[request_id] = uid, outcome
        endpoint = f"/internal/v1/users/{uid}/workspace/jobs"
        try:
            admission = await asyncio.wait_for(client.post(endpoint, json={
                "request_id": request_id, "service": "drive", "operation": "knowledge.search",
                "parameters": {"terms": ["tồn", "kho", "pn22"]}, "body": {"question": QUESTION},
                "wait_seconds": 0, "deadline_seconds": self.args.job_timeout,
                "telegram_user_id": 10000 + uid, "telegram_chat_id": 10000 + uid,
                "telegram_binding_id": f"load-{uid}",
            }), timeout=self.args.job_timeout)
            result["admission_ms"].append((time.perf_counter() - started) * 1000)
            if admission.status_code not in (200, 202):
                result["failures"][f"admission_http_{admission.status_code}"] += 1
                return
            job_id = admission.json()["job_id"]
            if status_checks:
                other = uid % USERS + 1
                denied = await client.get(f"/internal/v1/users/{other}/workspace/jobs/{job_id}")
                if denied.status_code != 404:
                    self.failed_ownership_checks += 1
                    result["failures"]["cross_user_job_read"] += 1
                    return
                result["cross_user_reads_rejected"] += 1
            reason = await asyncio.wait_for(outcome, timeout=self.args.job_timeout)
            result["callback_ms"].append((time.perf_counter() - started) * 1000)
            if reason:
                result["failures"][reason] += 1
            else:
                result["succeeded"] += 1
        except TimeoutError:
            result["failures"]["timeout"] += 1
        except Exception:
            result["failures"]["request_error"] += 1
            logging.exception("Load request failed")
        finally:
            self.expected.pop(request_id, None)

    async def phase(self, client, label, users, duration):
        result = {"attempted": 0, "succeeded": 0, "failures": Counter(), "cross_user_reads_rejected": 0,
                  "admission_ms": [], "callback_ms": []}
        beginning = time.perf_counter()
        cpu_start = time.process_time()
        stop = beginning + duration
        offsets = len(self.retrieval_ms), len(self.permission_ms), len(self.embedding_ms)
        drive_before = self.drive.calls.copy()

        async def user(uid):
            first = True
            while time.perf_counter() < stop:
                iteration = time.perf_counter()
                result["attempted"] += 1
                await self.query(client, uid, result, status_checks=first)
                first = False
                pause = min(stop - time.perf_counter(), self.args.interval - (time.perf_counter() - iteration))
                await asyncio.sleep(max(0, pause))

        users_task = asyncio.gather(*(user(uid) for uid in range(1, users + 1)))
        while not users_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(users_task), timeout=60)
            except TimeoutError:
                print(json.dumps({"phase": label, "elapsed_seconds": round(time.perf_counter() - beginning),
                                  "attempted": result["attempted"], "succeeded": result["succeeded"],
                                  "sync_ticks": self.sync_ticks}), flush=True)
        await users_task
        retrieval = self.retrieval_ms[offsets[0]:]
        permission = self.permission_ms[offsets[1]:]
        embedding = self.embedding_ms[offsets[2]:]
        result["success_rate"] = result["succeeded"] / result["attempted"]
        for label_key, samples in (
            ("admission", result.pop("admission_ms")), ("callback", result.pop("callback_ms")),
            ("internal_retrieval", retrieval), ("permission", permission), ("embedding_simulated", embedding),
        ):
            result[f"{label_key}_samples"] = len(samples)
            result[f"p95_{label_key}_ms"] = percentile(samples)
        result.update(
            phase=label, users=users, target_seconds=duration,
            elapsed_seconds=round(time.perf_counter() - beginning, 2),
            gateway_process_cpu_seconds=round(time.process_time() - cpu_start, 2),
            synthetic_drive_calls=dict(self.drive.calls - drive_before),
        )
        print(json.dumps(result), flush=True)
        return result

    async def execute(self):
        seed(self.factory, self.settings)
        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(self.external), timeout=10) as external,
            httpx.AsyncClient(transport=httpx.ASGITransport(self.api), base_url="http://gateway",
                              headers={"Authorization": f"Bearer {self.settings.AGENT_TO_GATEWAY_SECRET}"}) as client,
        ):
            workers = [WorkspaceWorker(self.settings, self.factory, worker_id=f"load-{idx}",
                                       poll_interval=0.1, http_client=external)
                       for idx in range(self.args.workers)]
            background = WorkspaceWorker(
                self.settings, self.factory, worker_id="load-background", poll_interval=0.1,
                http_client=external, background=True,
            )
            for worker in workers + [background]:
                worker.workspace_svc = self.drive
                if not worker.background:
                    self.instrument(worker)
                worker.start()
            sync_task = asyncio.create_task(self.sync(background))
            try:
                phases = [
                    await self.phase(client, "nominal", 25, self.args.nominal_seconds),
                    await self.phase(client, "burst", 50, self.args.burst_seconds),
                ]
                await asyncio.sleep(1)
                counts = self.counts()
                total_success = sum(phase["succeeded"] for phase in phases)
                passed = (
                    all(phase["success_rate"] >= 0.99 and phase["p95_admission_ms"] is not None
                        and phase["p95_admission_ms"] <= 3000 and phase["p95_internal_retrieval_ms"] is not None
                        and phase["p95_internal_retrieval_ms"] <= 1000 for phase in phases)
                    and counts["deadlocks"] == 0 and counts["indexed_sync_files"] > 0
                    and counts["queued_deliveries"] == total_success
                    and self.failed_ownership_checks == 0 and self.sync_errors == 0
                )
                print(json.dumps({"passed": passed, **counts,
                                  "ownership_failures": self.failed_ownership_checks,
                                  "sync_errors": self.sync_errors, "sync_ticks": self.sync_ticks}), flush=True)
                return passed
            finally:
                sync_task.cancel()
                with suppress(asyncio.CancelledError):
                    await sync_task
                await asyncio.gather(*(worker.stop() for worker in workers + [background]))


def positive(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("Must be positive.")
    return parsed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nominal-seconds", type=positive, default=1800)
    parser.add_argument("--burst-seconds", type=positive, default=300)
    parser.add_argument("--interval", type=positive, default=15)
    parser.add_argument("--workers", type=positive, default=2)
    parser.add_argument("--job-timeout", type=positive, default=60, choices=range(1, 301), metavar="1-300")
    parser.add_argument("--drive-ms", type=positive, default=30)
    parser.add_argument("--embedding-ms", type=positive, default=40)
    parser.add_argument("--agent-ms", type=positive, default=75)
    args = parser.parse_args()
    url = make_url(normalize_db_url(os.environ["STAGE4_POSTGRES_TEST_URL"]))
    if url.get_backend_name() != "postgresql" or url.host not in {
        "localhost", "127.0.0.1", "::1", "host.docker.internal",
    }:
        parser.error("Use a disposable local PostgreSQL server.")
    database = f"stage4_load_{uuid4().hex}"
    admin = create_engine(url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    with admin.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {database}"))
    connected = url.set(database=database)
    engine = create_postgres_engine(connected.render_as_string(hide_password=False))
    try:
        with engine.begin() as db:
            db.execute(text("CREATE EXTENSION vector"))
            Base.metadata.create_all(db)
            sql_version = db.scalar(text("SHOW server_version"))
        print(json.dumps({"disposable_database": database, "postgres_version": sql_version, "initial_files": USERS,
                          "transport": "ASGI + synthetic providers", "configuration": vars(args)}), flush=True)
        return 0 if asyncio.run(Run(args, engine).execute()) else 1
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE {database}"))
        admin.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
