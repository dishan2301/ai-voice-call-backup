from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class Importance(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    IGNORE = "IGNORE"


class Category(str, Enum):
    CUSTOMER = "CUSTOMER"
    SALES = "SALES"
    SUPPORT = "SUPPORT"
    PURCHASE_ORDER = "PURCHASE_ORDER"
    PAYMENT = "PAYMENT"
    INVOICE = "INVOICE"
    MEETING = "MEETING"
    MANAGEMENT = "MANAGEMENT"
    INTERNAL = "INTERNAL"
    VENDOR = "VENDOR"
    SECURITY = "SECURITY"
    LEGAL = "LEGAL"
    ACCOUNT = "ACCOUNT"
    PROJECT = "PROJECT"
    RECRUITMENT = "RECRUITMENT"
    NEWSLETTER = "NEWSLETTER"
    MARKETING = "MARKETING"
    AUTOMATED = "AUTOMATED"
    SPAM = "SPAM"
    PERSONAL = "PERSONAL"
    UNKNOWN = "UNKNOWN"


class Urgency(str, Enum):
    IMMEDIATE = "IMMEDIATE"
    TODAY = "TODAY"
    THIS_WEEK = "THIS_WEEK"
    LATER = "LATER"
    NONE = "NONE"


class Analysis(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    importance: Importance
    category: Category
    needs_reply: bool
    needs_action: bool
    urgency: Urgency
    sender: str
    subject: str
    summary: str
    why_important: str = ""
    requested_action: str = ""
    deadline: Optional[str] = None
    key_facts: List[str] = Field(default_factory=list)
    risk_flags: List[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
