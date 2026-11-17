from html.parser import HTMLParser
import re
from typing import List

from app.schemas.mail import MailMessage


MAX_BODY_CHARS = 50_000
INJECTION_PATTERNS = (
    "ignore previous instructions",
    "reveal your system prompt",
    "reveal the system prompt",
    "run this command",
    "override your instructions",
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "head"}:
            self.hidden_depth += 1
        elif not self.hidden_depth and tag in {"br", "p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "head"} and self.hidden_depth:
            self.hidden_depth -= 1
        elif not self.hidden_depth and tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)


def html_to_text(value: str) -> str:
    if "<" not in value or ">" not in value:
        return value
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    return "".join(parser.parts)


def _strip_history_and_signature(value: str) -> str:
    kept: List[str] = []
    for line in value.splitlines():
        stripped = line.strip()
        lower = stripped.lower()
        if (
            stripped.startswith(">")
            or re.match(r"^on .+ wrote:$", lower)
            or lower.startswith("-----original message-----")
            or (lower.startswith("from:") and kept)
        ):
            break
        if lower in {"--", "regards,", "best regards,", "kind regards,"}:
            break
        kept.append(line)
    return "\n".join(kept)


def normalize_body(value: str) -> str:
    text = _strip_history_and_signature(html_to_text(value))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:MAX_BODY_CHARS]


def injection_risk_flags(value: str) -> List[str]:
    lower = value.lower()
    return ["PROMPT_INJECTION_ATTEMPT"] if any(pattern in lower for pattern in INJECTION_PATTERNS) else []


def normalize_message(message: MailMessage) -> MailMessage:
    body = normalize_body(message.body_text or message.snippet)
    return message.model_copy(update={"body_text": body})
