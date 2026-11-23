"""Run one Workspace worker in a dedicated process."""

import argparse
import asyncio
import logging
import signal
import uuid
from contextlib import suppress

from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import create_app_engine
from app.workspace.worker import WorkspaceWorker


async def main(background=False):
    settings = Settings()
    engine = create_app_engine(settings.DATABASE_URL)
    worker = WorkspaceWorker(
        settings, sessionmaker(bind=engine), worker_id=uuid.uuid4().hex, background=background
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(name, stop.set)
        except NotImplementedError:
            signal.signal(name, lambda *_: loop.call_soon_threadsafe(stop.set))
    worker.start()
    drive_tick = None

    async def run_indexer():
        while not stop.is_set():
            try:
                await worker.drive_runtime.tick(worker.network_slots)
                await asyncio.to_thread(worker.drive_runtime.cleanup)
            except Exception as exc:
                logging.error("Drive index maintenance failed: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except TimeoutError:
                pass

    if background and settings.DRIVE_INDEX_ENABLED:
        drive_tick = asyncio.create_task(run_indexer())
    try:
        if background:
            await stop.wait()
        else:
            while not stop.is_set():
                await asyncio.to_thread(worker.notify_slow_jobs)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=5)
                except TimeoutError:
                    pass
    finally:
        if drive_tick:
            drive_tick.cancel()
            with suppress(asyncio.CancelledError):
                await drive_tick
        await worker.stop()
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--drive-indexer", action="store_true")
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main(parser.parse_args().drive_indexer))
