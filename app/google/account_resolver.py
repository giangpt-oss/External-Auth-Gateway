"""Google Account Resolver for Multi-Account Workspace Architecture.

Provides unified resolution of active, pinned, and owned Google connections
across REST, MCP, Background Scheduler, and Worker pipelines.
"""


from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models import GoogleAccountSelection, GoogleConnection, User


class GoogleAccountResolver:
    @staticmethod
    def get_selection(db: Session, user_id: int, for_update: bool = False) -> GoogleAccountSelection | None:
        """Fetch the GoogleAccountSelection row for the user, optionally with row-level lock."""
        query = db.query(GoogleAccountSelection).filter_by(user_id=user_id)
        if for_update:
            if db.get_bind().dialect.name == "sqlite":
                # SQLite ignores FOR UPDATE. A no-op update reserves the write transaction.
                db.execute(
                    update(GoogleAccountSelection)
                    .where(GoogleAccountSelection.user_id == user_id)
                    .values(updated_at=GoogleAccountSelection.updated_at)
                    .execution_options(synchronize_session=False)
                )
            else:
                query = query.with_for_update()
            query = query.populate_existing()
        return query.first()

    @staticmethod
    def _first_connection(db: Session, user_id: int) -> GoogleConnection | None:
        return db.query(GoogleConnection).filter_by(user_id=user_id).order_by(GoogleConnection.id).first()

    @classmethod
    def get_active_account(cls, db: Session, user_id: int) -> GoogleConnection | None:
        """Read the selection, with a compatibility fallback for unmigrated users."""
        connection, _ = cls.get_active_with_selection(db, user_id)
        return connection

    @classmethod
    def get_active_with_selection(
        cls, db: Session, user_id: int,
    ) -> tuple[GoogleConnection | None, GoogleAccountSelection | None]:
        selection = cls.get_selection(db, user_id)
        if selection is None:
            return cls._first_connection(db, user_id), None
        if selection.active_account_id is None:
            return None, selection
        return cls.get_account(db, user_id, selection.active_account_id), selection

    @classmethod
    def lock_selection(cls, db: Session, user_id: int) -> GoogleAccountSelection | None:
        """Lock the selection in the caller's transaction, initializing legacy users once."""
        selection = cls.get_selection(db, user_id, for_update=True)
        if selection is not None:
            return selection

        # Concurrent initializers serialize on the parent because there is no selection row yet.
        user = db.query(User.id).filter_by(id=user_id).with_for_update().first()
        if user is None:
            return None
        selection = cls.get_selection(db, user_id, for_update=True)
        if selection is None:
            account = cls._first_connection(db, user_id)
            if account is None:
                return None
            selection = GoogleAccountSelection(user_id=user_id, active_account_id=account.id)
            db.add(selection)
            db.flush()
        return selection

    @staticmethod
    def get_account(db: Session, user_id: int, account_id: int) -> GoogleConnection | None:
        """Fetch a specific GoogleConnection and strictly verify ownership by user_id."""
        return db.query(GoogleConnection).filter_by(
            id=account_id,
            user_id=user_id,
        ).first()

    @staticmethod
    def resolve_for_task(
        db: Session,
        user_id: int,
        pinned_account_id: int | None = None,
    ) -> GoogleConnection | None:
        """Resolve the GoogleConnection for a task:

        - If pinned_account_id is provided, resolve and verify ownership.
        - Otherwise, fallback to the user's active account.
        """
        if pinned_account_id is not None:
            return GoogleAccountResolver.get_account(db, user_id, pinned_account_id)

        return GoogleAccountResolver.get_active_account(db, user_id)

    @staticmethod
    def get_user_connections(db: Session, user_id: int) -> list[GoogleConnection]:
        """List all Google connections belonging to the specified user."""
        return (
            db.query(GoogleConnection)
            .filter_by(user_id=user_id)
            .order_by(GoogleConnection.id.asc())
            .all()
        )
