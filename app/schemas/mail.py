from datetime import datetime
from typing import List

from pydantic import BaseModel, ConfigDict, Field


class AttachmentMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    filename: str = ""
    mime_type: str = "application/octet-stream"
    size: int = Field(default=0, ge=0)
    attachment_id: str = Field(default="", exclude=True)


class MailMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    message_id: str
    thread_id: str
    sender_name: str = ""
    sender_email: str = ""
    recipient_emails: List[str] = Field(default_factory=list)
    cc_emails: List[str] = Field(default_factory=list)
    subject: str = ""
    received_at: datetime
    body_text: str = ""
    snippet: str = ""
    labels: List[str] = Field(default_factory=list)
    gmail_history_id: str = ""
    has_attachments: bool = False
    attachments: List[AttachmentMetadata] = Field(default_factory=list)


class ThreadCard(BaseModel):
    model_config = ConfigDict(frozen=True)

    thread_id: str
    newest: MailMessage
    context: List[MailMessage] = Field(default_factory=list)
    body_hash: str
    normalization_version: str = "v1"
    risk_flags: List[str] = Field(default_factory=list)
