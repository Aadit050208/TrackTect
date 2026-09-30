"""Shared client for an OpenAI-compatible chat completions endpoint.

Respects provider rate limits (important for Groq free tier):
- global lock so only one LLM call runs at a time
- minimum spacing between calls
- automatic retry with backoff on HTTP 429

Also accumulates token usage per thread so pipeline runs can store
`usage.total_tokens` for quota calibration.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import requests

from config import settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_last_call_at = 0.0
_rate_limited_until = 0.0
_thread_tokens = threading.local()


def reset_token_usage() -> None:
    """Clear the per-thread token accumulator (call at start of a pipeline run)."""
    _thread_tokens.total = 0


def take_token_usage() -> int:
    """Return and clear accumulated tokens for this thread."""
    total = int(getattr(_thread_tokens, "total", 0) or 0)
    _thread_tokens.total = 0
    return total


def _record_tokens(data: dict) -> None:
    usage = data.get("usage") or {}
    try:
        tokens = int(usage.get("total_tokens") or 0)
    except (TypeError, ValueError):
        tokens = 0
    if tokens <= 0:
        return
    _thread_tokens.total = int(getattr(_thread_tokens, "total", 0) or 0) + tokens


class LLMClient:
    """Thin wrapper around POST {LLM_BASE_URL}/chat/completions."""

    def __init__(self) -> None:
        self.base_url = settings.llm_base_url
        self.api_key = settings.llm_api_key
        self.model = settings.llm_model
        self.min_interval = settings.llm_min_interval_seconds
        self.max_retries = settings.llm_max_retries

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def is_cooling_down(self) -> bool:
        return time.monotonic() < _rate_limited_until

    def chat(
        self,
        system: str,
        user: str,
        temperature: float = 0.4,
        max_tokens: int = 400,
        timeout: int = 90,
        json_mode: bool = False,
    ) -> Optional[str]:
        """Send one chat completion; return assistant text or None.

        json_mode: when True, request response_format json_object (OpenAI /
        Groq-compatible). If the endpoint rejects it, retry once without it.
        """
        global _last_call_at, _rate_limited_until

        if not self.configured:
            logger.warning("LLM_BASE_URL is not set; skipping LLM call.")
            return None

        if self.is_cooling_down():
            wait = max(0, _rate_limited_until - time.monotonic())
            logger.warning(
                "LLM cooling down after rate limit — skipping call (%.0fs left)", wait
            )
            return None

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        use_json_mode = bool(json_mode)

        with _lock:
            gap = self.min_interval - (time.monotonic() - _last_call_at)
            if gap > 0:
                time.sleep(gap)

            for attempt in range(1, self.max_retries + 1):
                payload = {
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "stream": False,
                }
                if use_json_mode:
                    payload["response_format"] = {"type": "json_object"}

                try:
                    response = requests.post(
                        f"{self.base_url}/chat/completions",
                        headers=headers,
                        json=payload,
                        timeout=timeout,
                    )
                    _last_call_at = time.monotonic()

                    if response.status_code == 429:
                        retry_after = response.headers.get("Retry-After")
                        try:
                            sleep_s = float(retry_after) if retry_after else (2 ** attempt) * 2
                        except ValueError:
                            sleep_s = (2 ** attempt) * 2
                        sleep_s = min(max(sleep_s, 3), 60)
                        logger.warning(
                            "LLM rate limited (429). Waiting %.0fs before retry %s/%s",
                            sleep_s, attempt, self.max_retries,
                        )
                        _rate_limited_until = time.monotonic() + sleep_s
                        time.sleep(sleep_s)
                        continue

                    if response.status_code == 400 and use_json_mode:
                        body_l = (response.text or "").lower()
                        if "response_format" in body_l or "json_validate_failed" in body_l:
                            # Endpoint rejected the format, or the model failed strict
                            # JSON validation — retry once as plain text so we can repair.
                            snippet = (response.text or "")[:400]
                            logger.warning(
                                "LLM JSON mode failed (%s); retrying without response_format. body=%s",
                                response.status_code,
                                snippet,
                            )
                            use_json_mode = False
                            # Reasoning models often need more completion room after a
                            # failed structured-output attempt.
                            if max_tokens < 2500:
                                max_tokens = 2500
                            continue

                    response.raise_for_status()
                    data = response.json()
                    _record_tokens(data)
                    message = data["choices"][0]["message"]
                    content = (message.get("content") or "").strip()
                    if not content:
                        reasoning = message.get("reasoning") or ""
                        finish = data["choices"][0].get("finish_reason")
                        usage = data.get("usage") or {}
                        details = usage.get("completion_tokens_details") or {}
                        logger.warning(
                            "LLM returned empty content (finish=%s, "
                            "completion_tokens=%s, reasoning_tokens=%s, "
                            "reasoning_preview=%r)",
                            finish,
                            usage.get("completion_tokens"),
                            details.get("reasoning_tokens"),
                            (reasoning or "")[:200],
                        )
                        # Reasoning models sometimes spend the whole budget
                        # on chain-of-thought; retry once with a larger cap
                        # if we still have retries left.
                        if (
                            details.get("reasoning_tokens")
                            and attempt < self.max_retries
                            and max_tokens < 4000
                        ):
                            max_tokens = min(4000, max(max_tokens * 2, 2500))
                            logger.warning(
                                "Retrying LLM call with max_tokens=%s after empty content",
                                max_tokens,
                            )
                            continue
                        return None
                    return content

                except requests.exceptions.ConnectionError:
                    logger.warning("LLM endpoint unreachable at %s", self.base_url)
                    return None
                except requests.exceptions.Timeout:
                    logger.warning("LLM request timed out after %ss", timeout)
                    return None
                except (KeyError, IndexError, ValueError) as exc:
                    logger.warning("Unexpected LLM response shape: %s", exc)
                    return None
                except requests.exceptions.HTTPError as exc:
                    status = exc.response.status_code if exc.response is not None else "?"
                    body = ""
                    if exc.response is not None:
                        body = (exc.response.text or "")[:400]
                    logger.warning("LLM HTTP error %s: %s %s", status, exc, body)
                    if (
                        status == 400
                        and use_json_mode
                        and (
                            "response_format" in body.lower()
                            or "json_validate_failed" in body.lower()
                        )
                    ):
                        logger.warning(
                            "LLM JSON mode failed; retrying without response_format"
                        )
                        use_json_mode = False
                        continue
                    if status in (500, 502, 503) and attempt < self.max_retries:
                        time.sleep(2 * attempt)
                        continue
                    return None
                except requests.exceptions.RequestException as exc:
                    logger.warning("LLM request failed: %s", exc)
                    return None

            _rate_limited_until = time.monotonic() + 60
            logger.warning(
                "LLM gave up after %s rate-limit retries; cooling down 60s",
                self.max_retries,
            )
            return None
