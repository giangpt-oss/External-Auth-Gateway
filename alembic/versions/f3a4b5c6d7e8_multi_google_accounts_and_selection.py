"""Multi Google accounts selection and workspace activity tracking schema.

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-11-18 11:45:00
"""

import sqlalchemy as sa

from alembic import op

revision = "f3a4b5c6d7e8"
down_revision = "e2f3a4b5c6d7"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    dialect_name = bind.dialect.name
    tables = set(insp.get_table_names())

    # 1. Modify google_connections: remove UNIQUE constraint on user_id
    if "google_connections" in tables:
        if dialect_name == "sqlite":
            # Check if user_id is unique
            uqs = insp.get_unique_constraints("google_connections")
            has_unique_user_id = any(uq.get("column_names") == ["user_id"] for uq in uqs)
            if has_unique_user_id:
                # In SQLite, recreate table without user_id unique
                op.execute(sa.text("DROP TABLE IF EXISTS google_connections_new"))
                op.execute(sa.text("""
                    CREATE TABLE google_connections_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        google_sub VARCHAR(255) NOT NULL UNIQUE,
                        email VARCHAR(255) NOT NULL,
                        scopes TEXT NOT NULL,
                        encrypted_access_token TEXT NOT NULL,
                        encrypted_refresh_token TEXT,
                        expires_at DATETIME NOT NULL,
                        status VARCHAR(32) NOT NULL DEFAULT 'connected',
                        connection_version INTEGER NOT NULL DEFAULT 1,
                        link_version INTEGER NOT NULL DEFAULT 1,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                """))
                op.execute(sa.text("""
                    INSERT INTO google_connections_new (
                        id, user_id, google_sub, email, scopes, encrypted_access_token,
                        encrypted_refresh_token, expires_at, status, connection_version,
                        link_version, created_at, updated_at
                    )
                    SELECT id, user_id, google_sub, email, scopes, encrypted_access_token,
                           encrypted_refresh_token, expires_at, status, connection_version,
                           link_version, created_at, updated_at
                    FROM google_connections
                """))
                op.execute(sa.text("DROP TABLE google_connections"))
                op.execute(sa.text("ALTER TABLE google_connections_new RENAME TO google_connections"))
                op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_google_connections_user_id ON google_connections(user_id)"))
                op.execute(sa.text("CREATE UNIQUE INDEX IF NOT EXISTS ix_google_connections_google_sub ON google_connections(google_sub)"))
        else:
            # Postgres / other
            uqs = insp.get_unique_constraints("google_connections")
            for uq in uqs:
                if uq.get("column_names") == ["user_id"] and uq.get("name"):
                    op.drop_constraint(uq["name"], "google_connections", type_="unique")
            op.create_index("ix_google_connections_user_id", "google_connections", ["user_id"], if_not_exists=True)

    # 2. Modify oauth_refresh_leases: add google_account_id, remove UNIQUE on user_id
    if "oauth_refresh_leases" in tables:
        cols = {c["name"] for c in insp.get_columns("oauth_refresh_leases")}
        if dialect_name == "sqlite":
            uqs = insp.get_unique_constraints("oauth_refresh_leases")
            has_unique_user_id = any(uq.get("column_names") == ["user_id"] for uq in uqs)
            if has_unique_user_id or "google_account_id" not in cols:
                op.execute(sa.text("DROP TABLE IF EXISTS oauth_refresh_leases_new"))
                op.execute(sa.text("""
                    CREATE TABLE oauth_refresh_leases_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL REFERENCES users(id),
                        google_account_id INTEGER UNIQUE REFERENCES google_connections(id) ON DELETE CASCADE,
                        lease_holder VARCHAR(64),
                        lease_expires_at DATETIME,
                        connection_version INTEGER NOT NULL DEFAULT 1,
                        updated_at DATETIME NOT NULL
                    )
                """))
                op.execute(sa.text("""
                    INSERT INTO oauth_refresh_leases_new (id, user_id, lease_holder, lease_expires_at, connection_version, updated_at)
                    SELECT id, user_id, lease_holder, lease_expires_at, connection_version, updated_at
                    FROM oauth_refresh_leases
                """))
                op.execute(sa.text("DROP TABLE oauth_refresh_leases"))
                op.execute(sa.text("ALTER TABLE oauth_refresh_leases_new RENAME TO oauth_refresh_leases"))
                op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_oauth_refresh_leases_user_id ON oauth_refresh_leases(user_id)"))
                op.execute(sa.text("CREATE UNIQUE INDEX IF NOT EXISTS ix_oauth_refresh_leases_google_account_id ON oauth_refresh_leases(google_account_id)"))
                op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_oauth_refresh_leases_lease_expires_at ON oauth_refresh_leases(lease_expires_at)"))
        else:
            if "google_account_id" not in cols:
                with op.batch_alter_table("oauth_refresh_leases") as table:
                    table.add_column(
                        sa.Column("google_account_id", sa.Integer(), sa.ForeignKey("google_connections.id", ondelete="CASCADE"), nullable=True)
                    )
                    table.create_unique_constraint("uq_oauth_refresh_leases_google_account_id", ["google_account_id"])
            uqs = insp.get_unique_constraints("oauth_refresh_leases")
            for uq in uqs:
                if uq.get("column_names") == ["user_id"] and uq.get("name"):
                    op.drop_constraint(uq["name"], "oauth_refresh_leases", type_="unique")

    # 3. Create google_account_selections table
    if "google_account_selections" not in tables:
        op.create_table(
            "google_account_selections",
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("active_account_id", sa.Integer(), sa.ForeignKey("google_connections.id", ondelete="SET NULL"), nullable=True, index=True),
            sa.Column("selection_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("state", sa.String(32), nullable=False, server_default="accepting"),
            sa.Column("draining_since", sa.DateTime(), nullable=True),
            sa.Column("target_account_id", sa.Integer(), sa.ForeignKey("google_connections.id", ondelete="SET NULL"), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )

    # 4. Create workspace_activities table
    if "workspace_activities" not in tables:
        op.create_table(
            "workspace_activities",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("google_account_id", sa.Integer(), sa.ForeignKey("google_connections.id", ondelete="CASCADE"), nullable=True, index=True),
            sa.Column("activity_type", sa.String(64), nullable=False, index=True),
            sa.Column("reference_id", sa.String(128), nullable=False, index=True),
            sa.Column("status", sa.String(32), nullable=False, server_default="active", index=True),
            sa.Column("has_outbound_write", sa.Boolean(), nullable=False, server_default=sa.text("0" if dialect_name == "sqlite" else "false")),
            sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        op.create_index(
            "ix_workspace_activities_user_account",
            "workspace_activities",
            ["user_id", "google_account_id", "status"],
        )
        op.create_index(
            "ix_workspace_activities_ref",
            "workspace_activities",
            ["user_id", "reference_id"],
        )

    # 5. Add google_account_id and selection_version to workspace_jobs
    if "workspace_jobs" in tables:
        job_cols = {c["name"] for c in insp.get_columns("workspace_jobs")}
        with op.batch_alter_table("workspace_jobs") as table:
            if "google_account_id" not in job_cols:
                table.add_column(sa.Column("google_account_id", sa.Integer(), nullable=True))
                table.create_index("ix_workspace_jobs_google_account_id", ["google_account_id"])
            if "selection_version" not in job_cols:
                table.add_column(
                    sa.Column("selection_version", sa.Integer(), nullable=True, server_default="1")
                )

    # 6. Add google_account_id to action_operations
    if "action_operations" in tables:
        op_cols = {c["name"] for c in insp.get_columns("action_operations")}
        if "google_account_id" not in op_cols:
            with op.batch_alter_table("action_operations") as table:
                table.add_column(sa.Column("google_account_id", sa.Integer(), nullable=True))
                table.create_index("ix_action_operations_google_account_id", ["google_account_id"])

    # 7. Add google_account_id to calendar_pairs and update unique constraint
    if "calendar_pairs" in tables:
        cal_cols = {c["name"] for c in insp.get_columns("calendar_pairs")}
        if "google_account_id" not in cal_cols:
            with op.batch_alter_table("calendar_pairs") as table:
                table.add_column(sa.Column("google_account_id", sa.Integer(), nullable=True))
                table.create_index("ix_calendar_pairs_google_account_id", ["google_account_id"])

    # 8. Add google_account_id to sync_intents
    if "sync_intents" in tables:
        sync_cols = {c["name"] for c in insp.get_columns("sync_intents")}
        if "google_account_id" not in sync_cols:
            with op.batch_alter_table("sync_intents") as table:
                table.add_column(sa.Column("google_account_id", sa.Integer(), nullable=True))
                table.create_index("ix_sync_intents_google_account_id", ["google_account_id"])

    # 9. Add google_account_id to telegram_updates
    if "telegram_updates" in tables:
        tg_cols = {c["name"] for c in insp.get_columns("telegram_updates")}
        if "google_account_id" not in tg_cols:
            with op.batch_alter_table("telegram_updates") as table:
                table.add_column(sa.Column("google_account_id", sa.Integer(), nullable=True))
                table.create_index("ix_telegram_updates_google_account_id", ["google_account_id"])

    # 10. Backfill data
    # A. Backfill google_account_selections for existing connections
    op.execute(sa.text("""
        INSERT INTO google_account_selections (user_id, active_account_id, selection_version, state, updated_at)
        SELECT gc.user_id, gc.id, 1, 'accepting', CURRENT_TIMESTAMP
        FROM google_connections gc
        WHERE gc.id IN (
            SELECT MIN(id) FROM google_connections GROUP BY user_id
        )
        AND gc.user_id NOT IN (
            SELECT user_id FROM google_account_selections
        )
    """))

    # B. Backfill google_account_id on existing records
    if "oauth_refresh_leases" in tables:
        op.execute(sa.text("""
            UPDATE oauth_refresh_leases
            SET google_account_id = (
                SELECT id FROM google_connections
                WHERE google_connections.user_id = oauth_refresh_leases.user_id
                LIMIT 1
            )
            WHERE google_account_id IS NULL
            AND user_id IN (SELECT user_id FROM google_connections)
        """))

    if "workspace_jobs" in tables:
        op.execute(sa.text("""
            UPDATE workspace_jobs
            SET google_account_id = (
                SELECT id FROM google_connections
                WHERE google_connections.user_id = workspace_jobs.user_id
                LIMIT 1
            )
            WHERE google_account_id IS NULL
            AND user_id IN (SELECT user_id FROM google_connections)
        """))

    if "action_operations" in tables:
        op.execute(sa.text("""
            UPDATE action_operations
            SET google_account_id = (
                SELECT id FROM google_connections
                WHERE google_connections.user_id = action_operations.user_id
                LIMIT 1
            )
            WHERE google_account_id IS NULL
            AND user_id IN (SELECT user_id FROM google_connections)
        """))

    if "calendar_pairs" in tables:
        op.execute(sa.text("""
            UPDATE calendar_pairs
            SET google_account_id = (
                SELECT id FROM google_connections
                WHERE google_connections.user_id = calendar_pairs.user_id
                LIMIT 1
            )
            WHERE google_account_id IS NULL
            AND user_id IN (SELECT user_id FROM google_connections)
        """))

    if "sync_intents" in tables:
        op.execute(sa.text("""
            UPDATE sync_intents
            SET google_account_id = (
                SELECT id FROM google_connections
                WHERE google_connections.user_id = sync_intents.user_id
                LIMIT 1
            )
            WHERE google_account_id IS NULL
            AND user_id IN (SELECT user_id FROM google_connections)
        """))


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    if "workspace_activities" in tables:
        op.drop_table("workspace_activities")

    if "google_account_selections" in tables:
        op.drop_table("google_account_selections")

    if "telegram_updates" in tables:
        cols = {c["name"] for c in insp.get_columns("telegram_updates")}
        if "google_account_id" in cols:
            indexes = {idx["name"] for idx in insp.get_indexes("telegram_updates")}
            with op.batch_alter_table("telegram_updates") as table:
                if "ix_telegram_updates_google_account_id" in indexes:
                    table.drop_index("ix_telegram_updates_google_account_id")
                table.drop_column("google_account_id")

    if "sync_intents" in tables:
        cols = {c["name"] for c in insp.get_columns("sync_intents")}
        if "google_account_id" in cols:
            indexes = {idx["name"] for idx in insp.get_indexes("sync_intents")}
            with op.batch_alter_table("sync_intents") as table:
                if "ix_sync_intents_google_account_id" in indexes:
                    table.drop_index("ix_sync_intents_google_account_id")
                table.drop_column("google_account_id")

    if "calendar_pairs" in tables:
        cols = {c["name"] for c in insp.get_columns("calendar_pairs")}
        if "google_account_id" in cols:
            indexes = {idx["name"] for idx in insp.get_indexes("calendar_pairs")}
            with op.batch_alter_table("calendar_pairs") as table:
                if "ix_calendar_pairs_google_account_id" in indexes:
                    table.drop_index("ix_calendar_pairs_google_account_id")
                table.drop_column("google_account_id")

    if "action_operations" in tables:
        cols = {c["name"] for c in insp.get_columns("action_operations")}
        if "google_account_id" in cols:
            indexes = {idx["name"] for idx in insp.get_indexes("action_operations")}
            with op.batch_alter_table("action_operations") as table:
                if "ix_action_operations_google_account_id" in indexes:
                    table.drop_index("ix_action_operations_google_account_id")
                table.drop_column("google_account_id")

    if "workspace_jobs" in tables:
        cols = {c["name"] for c in insp.get_columns("workspace_jobs")}
        indexes = {idx["name"] for idx in insp.get_indexes("workspace_jobs")}
        with op.batch_alter_table("workspace_jobs") as table:
            if "ix_workspace_jobs_google_account_id" in indexes:
                table.drop_index("ix_workspace_jobs_google_account_id")
            if "selection_version" in cols:
                table.drop_column("selection_version")
            if "google_account_id" in cols:
                table.drop_column("google_account_id")

    if "oauth_refresh_leases" in tables:
        cols = {c["name"] for c in insp.get_columns("oauth_refresh_leases")}
        indexes = {idx["name"] for idx in insp.get_indexes("oauth_refresh_leases")}
        if "google_account_id" in cols:
            with op.batch_alter_table("oauth_refresh_leases") as table:
                if "ix_oauth_refresh_leases_google_account_id" in indexes:
                    table.drop_index("ix_oauth_refresh_leases_google_account_id")
                table.drop_column("google_account_id")

    if "google_connections" in tables:
        with op.batch_alter_table("google_connections") as table:
            table.create_unique_constraint("uq_google_connections_user_id", ["user_id"])
