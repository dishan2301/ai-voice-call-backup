import json
import logging
import re
import time
from typing import Any, Dict, Optional

import httpx

from app.llm.base import LLMProvider, LLMProviderError

logger = logging.getLogger(__name__)


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, api_key: str, model: str, client: Optional[httpx.Client] = None,
                 sleep=time.sleep, fallback_model: str = "gemini-3.1-flash-lite"):
        if not api_key:
            raise ValueError("GEMINI_API_KEY is required for Gmail analysis")
        if not model:
            raise ValueError("GEMINI_MODEL is required for Gmail analysis")
        self.api_key = api_key
        self.model = model
        self.fallback_model = fallback_model
        self.client = client or httpx.Client(
            base_url="https://generativelanguage.googleapis.com/v1beta",
            timeout=90.0,
        )
        self.sleep = sleep

    def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        json_schema: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        generation_config: Dict[str, Any] = {
            "temperature": 0,
            "responseMimeType": "application/json",
            # Mail analysis and agent JSON are structured responses. A very large
            # output budget can make Gemini spend tens of seconds deliberating even
            # when the answer is only a few hundred tokens.
            "maxOutputTokens": 4096,
        }
        if json_schema:
            generation_config["responseJsonSchema"] = json_schema
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": generation_config,
        }
        models = list(dict.fromkeys(model for model in (self.model, self.fallback_model) if model))
        try:
            for index, model in enumerate(models):
                try:
                    model_payload = {**payload, "generationConfig": dict(generation_config)}
                    if model.startswith("gemini-3"):
                        model_payload["generationConfig"]["thinkingConfig"] = {"thinkingLevel": "LOW"}
                    elif model.startswith("gemini-2.5"):
                        model_payload["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}
                    response = self._request(model, model_payload)
                    parts = response.json()["candidates"][0]["content"]["parts"]
                    content = "".join(part.get("text", "") for part in parts if not part.get("thought"))
                    result = json.loads(content)
                    if not isinstance(result, dict):
                        raise ValueError("model result is not an object")
                    return result
                except httpx.HTTPStatusError as exc:
                    if (index + 1 >= len(models) or exc.response.status_code != 429
                            or not self._quota_exhausted(exc.response)):
                        raise
        except (httpx.HTTPError, IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LLMProviderError("Gemini did not return a usable structured response") from exc

    def _request(self, model: str, payload: Dict[str, Any]) -> httpx.Response:
        rate_limit_retry_used = False
        for attempt in range(3):
            response = self.client.post(
                f"/models/{model}:generateContent",
                headers={"x-goog-api-key": self.api_key},
                json=payload,
            )
            try:
                response.raise_for_status()
                response_data = response.json()
                candidates = (response_data.get("candidates") or []) if isinstance(response_data, dict) else []
                candidate = (candidates[0] if isinstance(candidates, list) and candidates
                             and isinstance(candidates[0], dict) else {})
                usage = (response_data.get("usageMetadata") or {}) if isinstance(response_data, dict) else {}
                logger.info(
                    "gemini_response_ok",
                    extra={
                        "model": model,
                        "finish_reason": candidate.get("finishReason", "UNKNOWN"),
                        "output_tokens": usage.get("candidatesTokenCount", -1)
                        if isinstance(usage, dict) else -1,
                    },
                )
                return response
            except httpx.HTTPStatusError as exc:
                if attempt == 2 or exc.response.status_code not in {429, 500, 502, 503, 504}:
                    raise
                if exc.response.status_code == 429:
                    if self._quota_exhausted(exc.response) or rate_limit_retry_used:
                        raise
                    rate_limit_retry_used = True
                    delay = self._retry_after(exc.response)
                    self.sleep(delay if delay is not None else (1 << attempt))
                else:
                    self.sleep(1 << attempt)
        raise AssertionError("unreachable")

    @staticmethod
    def _quota_exhausted(response: httpx.Response) -> bool:
        try:
            error = response.json().get("error", {})
            message = error.get("message", "")
            details = error.get("details", [])
        except (ValueError, TypeError, AttributeError):
            return False
        return "quota" in str(message).lower() or any(
            "quota" in str(detail.get("reason", "")).lower()
            for detail in details if isinstance(detail, dict)
        )

    @staticmethod
    def _retry_after(response: httpx.Response):
        header = response.headers.get("Retry-After", "")
        try:
            return min(max(float(header), 0.0), 60.0)
        except ValueError:
            pass
        try:
            message = response.json().get("error", {}).get("message", "")
        except (ValueError, TypeError, AttributeError):
            message = ""
        match = re.search(r"retry in ([0-9]+(?:\.[0-9]+)?)s", str(message), re.I)
        return min(float(match.group(1)), 60.0) if match else None
