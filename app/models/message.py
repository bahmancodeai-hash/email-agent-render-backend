import uuid
import enum
from datetime import datetime
from sqlalchemy import String, DateTime, Enum, ForeignKey, Text, Integer, Boolean, ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID, JSONB
from app.database import Base


class MessageStatus(str, enum.Enum):
    UNREAD = "unread"
    READ = "read"
    FLAGGED = "flagged"
    ANSWERED = "answered"
    DRAFT = "draft"
    DELETED = "deleted"


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("email_accounts.id"), nullable=False)
    folder_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("folders.id"), nullable=True)

    # Remote identifiers
    uid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    remote_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    message_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)  # Message-ID header
    thread_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)  # Gmail thread ID

    # Headers
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    from_address: Mapped[str] = mapped_column(Text, nullable=False)
    from_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    to_addresses: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # [{email, name}]
    cc_addresses: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    bcc_addresses: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    reply_to: Mapped[str | None] = mapped_column(Text, nullable=True)
    in_reply_to: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Content
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    preview: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Attachments metadata stored in JSONB
    attachments: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # [{name, size, content_type, storage_key}]

    # Status
    status: Mapped[MessageStatus] = mapped_column(Enum(MessageStatus), default=MessageStatus.UNREAD)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    is_draft: Mapped[bool] = mapped_column(Boolean, default=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    is_spam: Mapped[bool] = mapped_column(Boolean, default=False)

    # Scheduled send
    scheduled_send_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    snooze_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Timestamps
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    account: Mapped["EmailAccount"] = relationship("EmailAccount", back_populates="messages")
    folder: Mapped["Folder"] = relationship("Folder", back_populates="messages")
