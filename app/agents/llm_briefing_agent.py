import json
import time
from pathlib import Path
from typing import Callable, Iterable

from pydantic import ValidationError

from app.llm.base import LLMProvider, LLMProviderError
from app.schemas.analysis import Analysis
from app.schemas.briefing import SynthesizedBrief


PROMPT_DIR = Path(__file__).resolve().parents[1] / "prompts"
PROMPT_VERSION = "v1"


class LLMBriefingError(RuntimeError):
    pass


class LLMBriefingSynthesizer:
    def __init__(
        self,
        provider: LLMProvider,
        max_retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.provider = provider
        self.max_retries = max_retries
        self.sleep = sleep
        self.system_prompt = "\n\n".join(
            (PROMPT_DIR / name).read_text(encoding="utf-8")
            for name in ("system_safety.md", "briefing_synthesizer.md")
        )

    def synthesize(self, analyses: Iterable[Analysis]) -> SynthesizedBrief:
        payload = [analysis.model_dump(mode="json") for analysis in analyses]
        user_prompt = "Create both briefing forms from these validated analyses only:\n" + json.dumps(payload)
        last_error: Exception = LLMBriefingError("No synthesis attempt was made")
        for attempt in range(self.max_retries + 1):
            try:
                raw = self.provider.generate_json(
                    self.system_prompt,
                    user_prompt,
                    SynthesizedBrief.model_json_schema(),
                )
                return SynthesizedBrief.model_validate(raw)
            except (LLMProviderError, ValidationError) as exc:
                last_error = exc
                if attempt < self.max_retries:
                    self.sleep(0.25 * (2**attempt))
        raise LLMBriefingError(
            f"Brief synthesis remained unavailable after {self.max_retries + 1} attempts"
        ) from last_error
