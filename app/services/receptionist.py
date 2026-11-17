"""Restricted receptionist prototype; no personal assistant tools are exposed."""
import re
from dataclasses import dataclass, field

_DIGITS = re.compile(r"\D+")

def normalize_phone(value: str) -> str | None:
    digits = _DIGITS.sub("", value or "")
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    if len(digits) == 11 and digits.startswith("0") and digits[1] in "6789":
        return "+91" + digits[1:]
    if len(digits) == 12 and digits.startswith("91"):
        return "+" + digits
    if len(digits) >= 8 and len(digits) <= 15:
        return "+" + digits
    return None

def normalize_allowlist(values: list[str]) -> list[str]:
    return sorted({item for value in values if (item := normalize_phone(value))})

def caller_allowed(enabled: bool, caller_id: str | None, allowlist: list[str]) -> bool:
    normalized = normalize_phone(caller_id or "")
    return bool(enabled and normalized and normalized in set(normalize_allowlist(allowlist)))

@dataclass
class ReceptionistSession:
    caller_id: str
    session_key: str
    transcript: list[str] = field(default_factory=list)
    closed: bool = False

    def prompt(self) -> str:
        return ("You are a restricted receptionist. Do not access Gmail, Calendar, WhatsApp, "
                "private messages, accounts, or tools. Collect a short name, reason for calling, "
                "and callback request. Say you will pass the message to the owner.")
