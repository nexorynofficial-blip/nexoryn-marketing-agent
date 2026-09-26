"""Data classes mirroring the SQLite schema defined in db.py."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Platform(str, Enum):
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"


class MessageType(str, Enum):
    COMMENT = "comment"
    DM = "dm"


class Language(str, Enum):
    ENGLISH = "en"
    URDU_SCRIPT = "ur_script"
    URDU_ROMAN = "ur_roman"
    OTHER = "other"


class Classification(str, Enum):
    LEAD = "lead"
    QUESTION = "question"
    COMPLIMENT = "compliment"
    COMPLAINT = "complaint"
    SPAM = "spam"
    OTHER = "other"


class MessageStatus(str, Enum):
    PENDING_DRAFT = "pending_draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    SENT = "sent"
    REJECTED = "rejected"
    EXPIRED = "expired"
    ALREADY_HANDLED = "already_handled"
    SPAM_PENDING = "spam_pending"
    SPAM_HIDDEN = "spam_hidden"
    SPAM_KEPT = "spam_kept"


@dataclass
class Message:
    id: str
    platform: Platform
    type: MessageType
    sender_id: str
    text: str
    received_at: str
    sender_username: Optional[str] = None
    detected_language: Optional[Language] = None
    english_translation: Optional[str] = None
    classification: Optional[Classification] = None
    status: MessageStatus = MessageStatus.PENDING_DRAFT


@dataclass
class Draft:
    message_id: str
    draft_text: str
    created_at: str
    id: Optional[int] = None
    draft_language: Optional[str] = None
    is_handoff: bool = False
    slack_message_ts: Optional[str] = None
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None
    sent_at: Optional[str] = None
    edited_text: Optional[str] = None


@dataclass
class ProcessedWebhookId:
    webhook_event_id: str
    received_at: str
