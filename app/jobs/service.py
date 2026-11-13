import logging
import threading
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import sessionmaker

from app.calendar.reminders import ReminderService
from app.calendar.service import CalendarSyncService
from app.google.oauth import GoogleOAuthService
from app.models import GoogleConnection, LinkToken, Outbox, Session, TelegramUpdate, User
from app.workspace.network_slots import NetworkSlots, network_slots, outbound_user_id

_logger = logging.getLogger(__name__)


class JobService:
    def __init__(
        self,
        session_factory: sessionmaker,
        google_oauth: GoogleOAuthService,
        calendar_sync: CalendarSyncService,
        reminder_service: ReminderService,
    ):
        self.session_factory = session_factory
        self.google_oauth = google_oauth
        self.calendar_sync = calendar_sync
        self.reminder_service = reminder_service
        with session_factory() as db:
            self.network_slots = NetworkSlots(db.get_bind())

        self._user_locks: dict[int, threading.Lock] = {}
        self._global_lock = threading.Lock()
        self._semaphore = threading.Semaphore(4)  # Max 4 concurrent worker tasks

    def _get_user_lock(self, user_id: int) -> threading.Lock:
        with self._global_lock:
            if user_id not in self._user_locks:
                self._user_locks[user_id] = threading.Lock()
            return self._user_locks[user_id]

    def refresh_tokens_job(self) -> None:
        now = datetime.now(UTC)
        with self.session_factory() as db:
            # Active Google connections expiring within 5 minutes
            expiring = (
                db.query(GoogleConnection)
                .filter(
                    GoogleConnection.status == "connected",
                    GoogleConnection.expires_at <= now + timedelta(minutes=5),
                )
                .all()
            )
            user_ids = [c.user_id for c in expiring]

        for uid in user_ids:
            with self._semaphore, self._get_user_lock(uid):
                try:
                    token = network_slots.set(self.network_slots)
                    try:
                        self.google_oauth.get_valid_access_token(uid)
                    finally:
                        network_slots.reset(token)
                except Exception as e:
                    _logger.warning(f"Error auto-refreshing Google token for user {uid}: {e}")

    def calendar_sync_job(self) -> None:
        with self.session_factory() as db:
            users = db.query(User).filter_by(active=True).all()
            user_ids = [u.id for u in users]

        for uid in user_ids:
            with self._semaphore, self._get_user_lock(uid):
                try:
                    token = network_slots.set(self.network_slots)
                    owner_token = outbound_user_id.set(uid)
                    try:
                        self.calendar_sync.sync_user(uid)
                    finally:
                        outbound_user_id.reset(owner_token)
                        network_slots.reset(token)
                except Exception as e:
                    _logger.error(f"Error in background calendar sync for user {uid}: {e}")

    def reminders_and_outbox_job(self) -> None:
        now = datetime.now(UTC)
        try:
            self.reminder_service.enqueue_due(now)
            self.reminder_service.deliver_due(now)
        except Exception as e:
            _logger.error(f"Error in reminder and outbox delivery: {e}")

    def cleanup_old_data_job(self) -> None:
        now = datetime.now(UTC)
        thirty_days_ago = now - timedelta(days=30)

        with self.session_factory() as db:
            # 1. Delete expired sessions and link tokens
            db.query(Session).filter(Session.expires_at < now).delete()
            db.query(LinkToken).filter(LinkToken.expires_at < now).delete()

            # 2. Delete processed telegram updates older than 30 days
            db.query(TelegramUpdate).filter(
                TelegramUpdate.status == "processed",
                TelegramUpdate.created_at < thirty_days_ago,
            ).delete()

            # 3. Delete sent outbox items older than 30 days
            db.query(Outbox).filter(
                Outbox.status == "sent",
                Outbox.created_at < thirty_days_ago,
            ).delete()

            db.commit()
