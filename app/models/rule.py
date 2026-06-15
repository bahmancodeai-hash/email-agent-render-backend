import uuid
import enum
from datetime import datetime
from sqlalchemy import String, DateTime, Enum, ForeignKey, Text, Boolean, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID, JSONB
from app.database import Base


class RuleConditionField(str, enum.Enum):
    FROM = "from"
    TO = "to"
    SUBJECT = "subject"
    BODY = "body"
    HAS_ATTACHMENT = "has_attachment"
    DOMAIN = "domain"


class RuleConditionOp(str, enum.Enum):
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    EQUALS = "equals"
    STARTS_WITH = "starts_with"
    ENDS_WITH = "ends_with"
    IS_TRUE = "is_true"


class RuleActionType(str, enum.Enum):
    MOVE_TO_FOLDER = "move_to_folder"
    MARK_READ = "mark_read"
    FLAG = "flag"
    DELETE = "delete"
    ARCHIVE = "archive"
    LABEL = "label"
    FORWARD_TO = "forward_to"
    AUTO_REPLY = "auto_reply"
    WEBHOOK = "webhook"


class EmailRule(Base):
    __tablename__ = "email_rules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("email_accounts.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    stop_processing: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    conditions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    conditions_match: Mapped[str] = mapped_column(String(3), default="all")  # all | any
    actions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    times_triggered: Mapped[int] = mapped_column(Integer, default=0)
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
