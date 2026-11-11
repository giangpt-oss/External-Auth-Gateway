#!/usr/bin/env python3
"""
Development Telegram Poller
Polls Telegram updates via getUpdates and processes them locally
Allowing full Telegram bot testing on localhost without setting up a public webhook tunnel.
"""

import logging
import sys
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import init_db
from app.odoo.client import OdooClient
from app.telegram.client import TelegramClient
from app.telegram.worker import TelegramAIWorker

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
_logger = logging.getLogger("telegram_poller")


def setup_poller_worker(
    settings: Settings,
    session_factory: sessionmaker,
    telegram_client: TelegramClient,
) -> TelegramAIWorker | None:
    if settings.ENABLE_AI_AGENT_FORWARDING:
        _logger.info("Starting TelegramAIWorker for independent poller...")
        worker = TelegramAIWorker(
            settings=settings,
            session_factory=session_factory,
            telegram_client=telegram_client,
        )
        worker.start()
        return worker
    return None


def run_poller():
    settings = Settings()
    if not settings.TELEGRAM_BOT_TOKEN or "dummy" in settings.TELEGRAM_BOT_TOKEN:
        _logger.error("TELEGRAM_BOT_TOKEN is not set properly in .env")
        return

    engine, session_factory = init_db(settings.DATABASE_URL)
    telegram_client = TelegramClient(bot_token=settings.TELEGRAM_BOT_TOKEN)
    odoo_client = OdooClient(url=settings.ODOO_URL, db=settings.ODOO_DB)

    from app.telegram.service import create_telegram_service

    telegram_service = create_telegram_service(
        settings=settings,
        session_factory=session_factory,
        telegram_client=telegram_client,
        odoo_client=odoo_client,
    )

    worker = setup_poller_worker(settings, session_factory, telegram_client)

    _logger.info("Starting Telegram Poller for @%s...", settings.TELEGRAM_BOT_USERNAME)
    _logger.info("Listening for Telegram messages/clicks in real-time...")

    offset = None
    api_url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/getUpdates"

    try:
        while True:
            try:
                params = {"timeout": 10, "allowed_updates": '["message","edited_message","callback_query"]'}
                if offset is not None:
                    params["offset"] = offset

                resp = httpx.get(api_url, params=params, timeout=15.0)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("ok"):
                        for update in data.get("result", []):
                            _logger.info(
                                "Ingesting Telegram Update #%s",
                                update.get("update_id"),
                            )
                            telegram_service.ingest_update(update)
                            offset = update["update_id"] + 1
                else:
                    _logger.warning("Failed to fetch updates from Telegram: %s", resp.text)
                    time.sleep(2)
            except Exception as e:
                _logger.error("Error in poller loop: %s", e)
                time.sleep(2)
    finally:
        if worker:
            _logger.info("Stopping TelegramAIWorker...")
            worker.stop()
        _logger.info("Closing database engine...")
        engine.dispose()


if __name__ == "__main__":
    run_poller()
