"""Run one Workspace worker in a dedicated process."""

import asyncio
import argparse
import logging
import signal
import uuid

from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import create_app_engine
from app.workspace.worker import WorkspaceWorker


async def main(background=False):
    settings = Settings()
    engine = create_app_engine(settings.DATABASE_URL)
    worker = WorkspaceWorker(settings, sessionmaker(bind=engine), worker_id=uuid.uuid4().hex,
                             background=background)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(name, stop.set)
        except NotImplementedError:
            signal.signal(name, lambda *_: loop.call_soon_threadsafe(stop.set))
    worker.start()
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
        await worker.stop()
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--drive-indexer", action="store_true")
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main(parser.parse_args().drive_indexer))
