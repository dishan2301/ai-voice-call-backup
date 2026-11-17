import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional


_MAP = json.loads((Path(__file__).with_name("confusion_map.json")).read_text(encoding="utf-8"))


@dataclass(frozen=True)
class VoiceInterpretation:
    text: str
    confidence: Optional[float]
    tier: str
    corrections: tuple[str, ...]


def _replace(text: str) -> tuple[str, tuple[str, ...]]:
    result = text
    corrections = []
    for heard, meant in sorted(_MAP.items(), key=lambda pair: len(pair[0]), reverse=True):
        pattern = re.compile(r"(?<!\w)" + re.escape(heard) + r"(?!\w)", re.IGNORECASE)
        if pattern.search(result):
            result = pattern.sub(meant, result)
            corrections.append(f"{heard} -> {meant}")
    return result, tuple(corrections)


def normalize_voice(message: str, alternatives: Optional[Iterable[Mapping[str, object]]] = None,
                    input_mode: str = "text", *, preserve_literals: bool = False) -> VoiceInterpretation:
    """Choose and repair one ASR candidate; typed input remains high confidence."""
    if input_mode == "text":
        return VoiceInterpretation(message.strip(), 1.0, "HIGH", ())
    candidates = []
    for item in alternatives or []:
        raw = item.get("text")
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            score = float(item.get("confidence"))
        except (TypeError, ValueError):
            score = -1
        confidence = score if math.isfinite(score) and 0 <= score <= 1 else None
        candidates.append((raw.strip(), confidence))
    raw, confidence = max(candidates, key=lambda item: item[1] if item[1] is not None else -1,
                          default=(message.strip(), None))
    corrected, corrections = (raw, ()) if preserve_literals else _replace(raw)
    if re.search(r"\b(sand it|argent|agent mail|us customer ko)\b", raw, re.I):
        tier = "LOW"
    elif confidence is None:
        # Live ASR supplies no calibrated score. Reversible mailbox work may proceed,
        # but UNKNOWN is never authority to send; both transports use the same policy.
        tier = "UNKNOWN"
    else:
        tier = "HIGH" if confidence >= 0.85 else "MEDIUM" if confidence >= 0.60 else "LOW" if confidence >= 0.35 else "UNUSABLE"
    return VoiceInterpretation(corrected, confidence, tier, corrections)
