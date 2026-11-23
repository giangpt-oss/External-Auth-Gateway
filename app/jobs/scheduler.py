import logging
import threading
import time

from app.jobs.service import JobService

_logger = logging.getLogger(__name__)


class BackgroundScheduler:
    def __init__(
        self,
        job_service: JobService,
        refresh_interval: int = 60,
        reminder_interval: int = 60,
        cleanup_interval: int = 3600,
    ):
        self.job_service = job_service
        self.refresh_interval = refresh_interval
        self.reminder_interval = reminder_interval
        self.cleanup_interval = cleanup_interval

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="AuthGatewayScheduler", daemon=True
        )
        self._thread.start()
        _logger.info("BackgroundScheduler started successfully.")

    def stop(self, timeout: float = 5.0) -> None:
        if not self._thread or not self._thread.is_alive():
            return

        self._stop_event.set()
        self._thread.join(timeout=timeout)
        _logger.info("BackgroundScheduler stopped gracefully.")

    def _run_loop(self) -> None:
        last_refresh = 0.0
        last_reminder = 0.0
        last_cleanup = 0.0

        while not self._stop_event.is_set():
            now_mono = time.monotonic()

            # 1. Refresh Tokens (every 60s)
            if now_mono - last_refresh >= self.refresh_interval:
                try:
                    self.job_service.refresh_tokens_job()
                except Exception as e:
                    _logger.error(f"Error in refresh_tokens_job: {e}")
                last_refresh = now_mono

            # 2. Reminders & Outbox (every 60s)
            if now_mono - last_reminder >= self.reminder_interval:
                try:
                    self.job_service.reminders_and_outbox_job()
                except Exception as e:
                    _logger.error(f"Error in reminders_and_outbox_job: {e}")
                last_reminder = now_mono

            # 3. Cleanup old data (every 1h)
            if now_mono - last_cleanup >= self.cleanup_interval:
                try:
                    self.job_service.cleanup_old_data_job()
                except Exception as e:
                    _logger.error(f"Error in cleanup_old_data_job: {e}")
                last_cleanup = now_mono

            # Sleep short slice (1 sec) to remain responsive to stop_event
            self._stop_event.wait(timeout=1.0)
