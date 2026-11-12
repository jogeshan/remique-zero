
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def new_uuid():
    return uuid.uuid4()


class TimestampMixin:
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=new_uuid,
    )
    display_name: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(
        String(20),
        default="pending",
        nullable=False,
    )
    role: Mapped[str] = mapped_column(
        String(20),
        default="user",
        nullable=False,
    )
    deleted_at: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True)
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'active', 'disabled')",
            name="valid_user_status",
        ),
        CheckConstraint(
            "role IN ('user', 'admin', 'super_admin')",
            name="valid_user_role",
        ),
    )


class TelegramAccount(Base):
    __tablename__ = "telegram_accounts"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger,
        unique=True,
        nullable=False,
    )
    chat_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )
    username: Mapped[str | None] = mapped_column(String(100))
    linked_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class UserSetting(TimestampMixin, Base):
    __tablename__ = "user_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    timezone: Mapped[str] = mapped_column(
        String(64),
        default="Asia/Dhaka",
        nullable=False,
    )
    language: Mapped[str] = mapped_column(
        String(10),
        default="en",
        nullable=False,
    )
    date_format: Mapped[str] = mapped_column(
        String(30),
        default="YYYY-MM-DD",
    )
    default_snooze_minutes: Mapped[int] = mapped_column(
        Integer,
        default=10,
    )
    quiet_hours_start: Mapped[str | None] = mapped_column(String(5))
    quiet_hours_end: Mapped[str | None] = mapped_column(String(5))


class InviteCode(Base):
    __tablename__ = "invite_codes"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=new_uuid,
    )
    code_hash: Mapped[str] = mapped_column(
        String(128),
        unique=True,
        nullable=False,
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )
    max_uses: Mapped[int] = mapped_column(
        Integer,
        default=1,
        nullable=False,
    )
    use_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    expires_at: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True)
    )
    revoked_at: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "max_uses > 0",
            name="positive_max_uses",
        ),
        CheckConstraint(
            "use_count >= 0",
            name="nonnegative_use_count",
        ),
        CheckConstraint(
            "use_count <= max_uses",
            name="uses_within_limit",
        ),
    )


class Reminder(TimestampMixin, Base):
    __tablename__ = "reminders"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=new_uuid,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(
        String(250),
        nullable=False,
    )
    description: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(
        String(64),
        default="Asia/Dhaka",
        nullable=False,
    )
    recurrence_rule: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(
        String(20),
        default="active",
        nullable=False,
    )

    __table_args__ = (
        Index(
            "ix_reminders_user_status",
            "user_id",
            "status",
        ),
        CheckConstraint(
            "status IN ('active', 'paused', 'cancelled', 'completed')",
            name="valid_reminder_status",
        ),
    )


class ReminderOccurrence(Base):
    __tablename__ = "reminder_occurrences"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=new_uuid,
    )
    reminder_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("reminders.id", ondelete="CASCADE"),
        nullable=False,
    )
    scheduled_for: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        default="scheduled",
        nullable=False,
    )
    completed_at: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "reminder_id",
            "scheduled_for",
        ),
        Index(
            "ix_occurrences_status_schedule",
            "status",
            "scheduled_for",
        ),
        CheckConstraint(
            "status IN ('scheduled', 'delivered', 'done', 'skipped')",
            name="valid_occurrence_status",
        ),
    )


class DeliveryJob(Base):
    __tablename__ = "delivery_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=new_uuid,
    )
    occurrence_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("reminder_occurrences.id", ondelete="CASCADE"),
        nullable=False,
    )
    job_type: Mapped[str] = mapped_column(
        String(30),
        default="reminder",
        nullable=False,
    )
    run_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        default="pending",
        nullable=False,
    )
    attempts: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer,
        default=5,
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(200),
        unique=True,
        nullable=False,
    )
    locked_until: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True)
    )
    locked_by: Mapped[str | None] = mapped_column(String(100))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index(
            "ix_delivery_jobs_status_run_at",
            "status",
            "run_at",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'succeeded', 'failed', 'cancelled')",
            name="valid_delivery_status",
        ),
        CheckConstraint(
            "attempts >= 0",
            name="nonnegative_attempts",
        ),
    )


class DeliveryAttempt(Base):
    __tablename__ = "delivery_attempts"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=new_uuid,
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("delivery_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempted_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    result: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )
    provider_message_id: Mapped[str | None] = mapped_column(String(100))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)


class MediaAsset(Base):
    __tablename__ = "media_assets"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=new_uuid,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    storage_key: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
    )
    media_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    processing_status: Mapped[str] = mapped_column(
        String(20),
        default="pending",
        nullable=False,
    )
    expires_at: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class AIUsageRecord(Base):
    __tablename__ = "ai_usage_records"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=new_uuid,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )
    model: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    operation: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost: Mapped[float] = mapped_column(default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    success: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index(
            "ix_ai_usage_user_created",
            "user_id",
            "created_at",
        ),
    )


class ProcessedUpdate(Base):
    __tablename__ = "processed_updates"

    update_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
    )
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger)
    processed_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=new_uuid,
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )
    target_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )
    action: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    metadata_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class Conversation(TimestampMixin, Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=new_uuid,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(
        String(150),
        default="New conversation",
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    __table_args__ = (
        Index(
            "ix_conversations_user_updated",
            "user_id",
            "updated_at",
        ),
        Index(
            "uq_conversations_one_active_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("is_active IS TRUE"),
        ),
    )


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=new_uuid,
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    model: Mapped[str | None] = mapped_column(String(100))
    input_tokens: Mapped[int] = mapped_column(
        Integer,
        default=0,
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer,
        default=0,
    )
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index(
            "ix_conversation_messages_history",
            "conversation_id",
            "created_at",
        ),
        CheckConstraint(
            "role IN ('user', 'model')",
            name="valid_conversation_message_role",
        ),
    )
