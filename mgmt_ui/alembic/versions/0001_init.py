"""initial schema

Revision ID: 0001_init
Revises:
Create Date: 2026-05-16 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "0001_init"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------------------------
# ENUM type declarations (Postgres native enums)
# ---------------------------------------------------------------------------

user_role = postgresql.ENUM("admin", "agent", name="user_role")
ssh_auth = postgresql.ENUM("password", "pubkey", name="ssh_auth")
server_status = postgresql.ENUM("unknown", "online", "offline", name="server_status")
agent_stack_status = postgresql.ENUM(
    "provisioning", "up", "down", "deprovisioning", name="agent_stack_status"
)
customer_assignment_status = postgresql.ENUM(
    "pending", "assigned", "active", name="customer_assignment_status"
)
distribution_scope = postgresql.ENUM("global", "agent", name="distribution_scope")
distribution_policy = postgresql.ENUM(
    "manual", "round_robin", "least_customers", "broker_affinity", name="distribution_policy"
)
scheduler_job_name = postgresql.ENUM("cache_warmup", "run_trading", name="scheduler_job_name")
run_job_name = postgresql.ENUM("cache_warmup", "run_trading", name="run_job_name")
run_trigger = postgresql.ENUM("scheduled", "manual", "api", "retry", name="run_trigger")
run_status = postgresql.ENUM("running", "success", "failed", "killed", name="run_status")
stack_run_lock_kind = postgresql.ENUM("cache", "trade", name="stack_run_lock_kind")
health_severity = postgresql.ENUM(
    "info", "warning", "error", "critical", name="health_severity"
)


_ALL_ENUMS = [
    user_role,
    ssh_auth,
    server_status,
    agent_stack_status,
    customer_assignment_status,
    distribution_scope,
    distribution_policy,
    scheduler_job_name,
    run_job_name,
    run_trigger,
    run_status,
    stack_run_lock_kind,
    health_severity,
]


def upgrade() -> None:
    # pgcrypto provides gen_random_uuid()
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')

    bind = op.get_bind()
    for enum in _ALL_ENUMS:
        enum.create(bind, checkfirst=True)

    # ---------------- users ----------------
    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("username", sa.String(length=255), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "role",
            postgresql.ENUM(name="user_role", create_type=False),
            nullable=False,
        ),
        sa.Column("telegram_user_id", sa.String(length=64), nullable=True),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # ---------------- servers ----------------
    op.create_table(
        "servers",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("ssh_port", sa.Integer(), nullable=False, server_default=sa.text("22")),
        sa.Column("ssh_user", sa.String(length=255), nullable=False),
        sa.Column(
            "ssh_auth",
            postgresql.ENUM(name="ssh_auth", create_type=False),
            nullable=False,
        ),
        sa.Column("ssh_secret_ref", sa.Text(), nullable=False),
        sa.Column("host_key_pin", sa.Text(), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(name="server_status", create_type=False),
            nullable=False,
            server_default=sa.text("'unknown'"),
        ),
        sa.Column("last_seen_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "base_dir",
            sa.String(length=512),
            nullable=False,
            server_default=sa.text("'/root/seller-market/agents'"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # ---------------- server_clock_skew_samples ----------------
    op.create_table(
        "server_clock_skew_samples",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "server_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("servers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "sampled_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("delta_seconds", sa.Integer(), nullable=False),
    )

    # ---------------- settings ----------------
    op.create_table(
        "settings",
        sa.Column("key", sa.String(length=255), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column(
            "updated_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # ---------------- agent_stacks ----------------
    op.create_table(
        "agent_stacks",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "server_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("servers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("stack_dir", sa.String(length=512), nullable=False),
        sa.Column("compose_project", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="agent_stack_status", create_type=False),
            nullable=False,
        ),
        sa.Column("deployed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint("server_id", "agent_id", name="uq_agent_stacks_server_agent"),
    )

    # ---------------- customers ----------------
    op.create_table(
        "customers",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "server_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("servers.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "stack_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_stacks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "assignment_status",
            postgresql.ENUM(name="customer_assignment_status", create_type=False),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("section_name", sa.String(length=255), nullable=False, unique=True),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("password_enc", sa.LargeBinary(), nullable=False),
        sa.Column("broker", sa.String(length=255), nullable=False),
        sa.Column("isin", sa.String(length=64), nullable=False),
        sa.Column("side", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("side IN (1, 2)", name="ck_customers_side"),
        sa.UniqueConstraint(
            "agent_id",
            "username",
            "broker",
            "isin",
            "side",
            name="uq_customers_agent_account_broker_isin_side",
        ),
    )
    op.create_index("ix_customers_agent_id", "customers", ["agent_id"])
    op.create_index("ix_customers_stack_id", "customers", ["stack_id"])

    # ---------------- distribution_policies ----------------
    op.create_table(
        "distribution_policies",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "scope",
            postgresql.ENUM(name="distribution_scope", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "policy",
            postgresql.ENUM(name="distribution_policy", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "default_server_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("servers.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # ---------------- scheduler_jobs ----------------
    op.create_table(
        "scheduler_jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "stack_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_stacks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "name",
            postgresql.ENUM(name="scheduler_job_name", create_type=False),
            nullable=False,
        ),
        sa.Column("time", sa.Time(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.UniqueConstraint("stack_id", "name", name="uq_scheduler_jobs_stack_name"),
    )

    # ---------------- locust_configs ----------------
    op.create_table(
        "locust_configs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "stack_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_stacks.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("users", sa.Integer(), nullable=False),
        sa.Column("spawn_rate", sa.Integer(), nullable=False),
        sa.Column("run_time", sa.String(length=64), nullable=False),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("processes", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )

    # ---------------- runs ----------------
    op.create_table(
        "runs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "stack_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_stacks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "job_name",
            postgresql.ENUM(name="run_job_name", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "trigger",
            postgresql.ENUM(name="run_trigger", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(name="run_status", create_type=False),
            nullable=False,
        ),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("log_blob_ref", sa.Text(), nullable=True),
        sa.Column("log_blob_sha256", sa.String(length=128), nullable=True),
    )
    op.create_index("ix_runs_stack_id", "runs", ["stack_id"])

    # ---------------- stack_run_locks ----------------
    op.create_table(
        "stack_run_locks",
        sa.Column(
            "stack_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_stacks.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "kind",
            postgresql.ENUM(name="stack_run_lock_kind", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("holder", sa.String(length=255), nullable=False),
    )

    # ---------------- ingest_cursors ----------------
    op.create_table(
        "ingest_cursors",
        sa.Column(
            "stack_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_stacks.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("last_filename", sa.String(length=512), nullable=True),
        sa.Column("last_mtime", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # ---------------- trade_results ----------------
    op.create_table(
        "trade_results",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("tracking_number", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("isin", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=True),
        sa.Column("side", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(20, 4), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False),
        sa.Column("executed_volume", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.Integer(), nullable=False),
        sa.Column("state_desc", sa.Text(), nullable=False),
        sa.Column("is_done", sa.Boolean(), nullable=False),
        sa.Column("net_amount", sa.Numeric(24, 4), nullable=True),
        sa.Column("created_at_broker", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_shamsi", sa.String(length=64), nullable=True),
        sa.Column("raw_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "ingested_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_trade_results_run_id", "trade_results", ["run_id"])

    # ---------------- health_signals ----------------
    op.create_table(
        "health_signals",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "stack_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_stacks.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(length=128), nullable=False),
        sa.Column(
            "severity",
            postgresql.ENUM(name="health_severity", create_type=False),
            nullable=False,
        ),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("raw", sa.Text(), nullable=True),
        sa.Column(
            "ts",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "ack_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("ack_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )

    # ---------------- audit_log ----------------
    op.create_table(
        "audit_log",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("target_type", sa.String(length=128), nullable=False),
        sa.Column("target_id", sa.Text(), nullable=False),
        sa.Column("before_json", postgresql.JSONB(), nullable=True),
        sa.Column("after_json", postgresql.JSONB(), nullable=True),
        sa.Column(
            "ts",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_audit_log_ts_desc",
        "audit_log",
        [sa.text("ts DESC")],
    )
    op.create_index("ix_audit_log_actor_user_id", "audit_log", ["actor_user_id"])


def downgrade() -> None:
    # Drop in reverse dependency order.
    op.drop_index("ix_audit_log_actor_user_id", table_name="audit_log")
    op.drop_index("ix_audit_log_ts_desc", table_name="audit_log")
    op.drop_table("audit_log")

    op.drop_table("health_signals")

    op.drop_index("ix_trade_results_run_id", table_name="trade_results")
    op.drop_table("trade_results")

    op.drop_table("ingest_cursors")
    op.drop_table("stack_run_locks")

    op.drop_index("ix_runs_stack_id", table_name="runs")
    op.drop_table("runs")

    op.drop_table("locust_configs")
    op.drop_table("scheduler_jobs")

    op.drop_table("distribution_policies")

    op.drop_index("ix_customers_stack_id", table_name="customers")
    op.drop_index("ix_customers_agent_id", table_name="customers")
    op.drop_table("customers")

    op.drop_table("agent_stacks")
    op.drop_table("settings")
    op.drop_table("server_clock_skew_samples")
    op.drop_table("servers")
    op.drop_table("users")

    bind = op.get_bind()
    for enum in reversed(_ALL_ENUMS):
        enum.drop(bind, checkfirst=True)

    op.execute('DROP EXTENSION IF EXISTS "pgcrypto"')
