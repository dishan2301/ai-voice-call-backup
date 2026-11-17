import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict


_STANDARD_FIELDS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)


class OAuthCallbackFilter(logging.Filter):
    """Remove Microsoft authorization response query material before any log handler."""
    def filter(self, record):
        rendered=record.getMessage()
        if '/auth/microsoft/callback?' in rendered:
            record.msg=re.sub(r'(/auth/microsoft/callback)\?[^\s"\']+',r'\1?[redacted]',rendered)
            record.args=()
        if 'https://graph.microsoft.com/' in record.getMessage():
            record.msg=re.sub(r'https://graph\.microsoft\.com/[^\s\"\']+', 'https://graph.microsoft.com/[read-redacted]', record.getMessage())
            record.args=()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_FIELDS and key not in {"message", "asctime"}:
                payload[key] = value
        if record.exc_info:
            payload["error_type"] = record.exc_info[0].__name__
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    for name in ['httpx','uvicorn.access']:
        target=logging.getLogger(name)
        if not any(isinstance(item,OAuthCallbackFilter) for item in target.filters):
            target.addFilter(OAuthCallbackFilter())
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
