from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import init_db


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = Settings()

    engine, session_factory = init_db(settings.DATABASE_URL)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.engine = engine
        app.state.session_factory = session_factory

        scheduler = None
        ai_worker = None
        poller_stop_event = None
        if settings.APP_ENV != "test":
            from app.calendar.reminders import ReminderService
            from app.calendar.service import CalendarSyncService
            from app.google.client import GoogleClient
            from app.google.oauth import GoogleOAuthService
            from app.jobs.scheduler import BackgroundScheduler
            from app.jobs.service import JobService
            from app.odoo.client import OdooClient
            from app.telegram.client import TelegramClient

            redirect_uri = f"{settings.PUBLIC_BASE_URL.rstrip('/')}/oauth/google/callback"
            google_oauth = GoogleOAuthService(
                session_factory=session_factory,
                client_id=settings.GOOGLE_CLIENT_ID,
                client_secret=settings.GOOGLE_CLIENT_SECRET,
                redirect_uri=redirect_uri,
                encryption_key=settings.ENCRYPTION_KEY or "dev_dummy_key_000000000000000000000=",
                allowed_domains=settings.GOOGLE_WORKSPACE_DOMAINS,
            )
            google_client = GoogleClient(google_oauth)
            odoo_client = OdooClient(url=settings.ODOO_URL, db=settings.ODOO_DB)
            tele_client = TelegramClient(bot_token=settings.TELEGRAM_BOT_TOKEN)

            cal_sync = CalendarSyncService(
                session_factory=session_factory,
                odoo_client=odoo_client,
                google_client=google_client,
                encryption_key=settings.ENCRYPTION_KEY or "dev_dummy_key_000000000000000000000=",
            )
            reminder = ReminderService(
                session_factory=session_factory,
                telegram_client=tele_client,
                encryption_key=settings.ENCRYPTION_KEY or "dev_dummy_key_000000000000000000000=",
                odoo_client=odoo_client,
                odoo_base_url=settings.ODOO_URL,
            )
            job_service = JobService(
                session_factory=session_factory,
                google_oauth=google_oauth,
                calendar_sync=cal_sync,
                reminder_service=reminder,
            )
            scheduler = BackgroundScheduler(
                job_service=job_service,
                refresh_interval=settings.REFRESH_JOB_SECONDS,
                calendar_interval=settings.CALENDAR_JOB_SECONDS,
                reminder_interval=settings.REMINDER_JOB_SECONDS,
            )
            scheduler.start()
            if settings.ENABLE_AI_AGENT_FORWARDING:
                from app.telegram.worker import TelegramAIWorker

                ai_worker = TelegramAIWorker(
                    settings=settings,
                    session_factory=session_factory,
                    telegram_client=tele_client,
                )
                ai_worker.start()

            # Automatic Telegram poller for development mode
            poller_stop_event = None
            if (
                settings.APP_ENV == "development"
                and settings.TELEGRAM_BOT_TOKEN
                and "dummy" not in settings.TELEGRAM_BOT_TOKEN
            ):
                import threading

                import httpx

                poller_stop_event = threading.Event()
                from app.telegram.service import create_telegram_service

                tele_svc = create_telegram_service(
                    settings=settings,
                    session_factory=session_factory,
                    telegram_client=tele_client,
                    odoo_client=odoo_client,
                )

                def _poll_worker():
                    offset = None
                    api_url = (
                        f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/getUpdates"
                    )
                    with httpx.Client(timeout=30.0) as client:
                        while not poller_stop_event.is_set():
                            try:
                                params = {"timeout": 20}
                                if offset is not None:
                                    params["offset"] = offset
                                resp = client.get(api_url, params=params)
                                if resp.status_code == 200:
                                    data = resp.json()
                                    if data.get("ok"):
                                        updates = data.get("result", [])
                                        for up in updates:
                                            tele_svc.ingest_update(up)
                                            offset = up["update_id"] + 1
                                        if not updates:
                                            poller_stop_event.wait(0.1)
                                else:
                                    poller_stop_event.wait(1.5)
                            except httpx.TimeoutException:
                                # Normal long-polling cycle completed without new updates
                                continue
                            except Exception:
                                poller_stop_event.wait(1.0)

                p_thread = threading.Thread(
                    target=_poll_worker, daemon=True, name="DevTelegramPoller"
                )
                p_thread.start()

        yield

        if poller_stop_event:
            poller_stop_event.set()
        if ai_worker:
            ai_worker.stop()
        if scheduler:
            scheduler.stop()
        engine.dispose()

    app = FastAPI(
        title="External Auth Gateway",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory

    def get_db() -> Session:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    @app.get("/healthz", status_code=status.HTTP_200_OK)
    def healthz(db: Session = Depends(get_db)):
        try:
            db.execute(text("SELECT 1"))
            db_status = "connected"
        except Exception:
            db_status = "disconnected"
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "error", "database": db_status},
            )
        return {"status": "ok", "database": db_status}

    from pathlib import Path

    from fastapi.staticfiles import StaticFiles

    from app.auth.routes import router as auth_router
    from app.google.routes import router as google_router
    from app.internal.routes import router as internal_router
    from app.portal.routes import router as portal_router
    from app.telegram.routes import router as telegram_router

    static_dir = Path(__file__).resolve().parent / "portal" / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    app.include_router(portal_router)
    app.include_router(auth_router)
    app.include_router(google_router)
    app.include_router(telegram_router)
    app.include_router(internal_router)

    return app


app = create_app()
