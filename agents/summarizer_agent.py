"""Summarizer Agent: condenses raw scraped page text into 3-5 bullet points.

Input : raw text (str) + source URL.
Output: bullet-point summary string, or None if the LLM is unavailable.
"""

import logging
from typing import Optional

from agents.http_text import looks_like_mojibake
from agents.llm_client import LLMClient

logger = logging.getLogger(__name__)


class SummarizerAgent:
    """Summarise competitor page content via the configured LLM endpoint."""

    def __init__(self, llm: Optional[LLMClient] = None) -> None:
        self.llm = llm or LLMClient()

    def summarize(self, raw_text: str, url: Optional[str] = None) -> Optional[str]:
        """Return a 3-5 bullet summary of `raw_text`, or None on failure."""
        if self.llm.is_cooling_down():
            logger.warning("Skipping summarizer — LLM still cooling down after rate limit")
            return None

        if looks_like_mojibake(raw_text or ""):
            logger.warning("Skipping summarizer for %s — scraped text looks corrupted", url)
            return None

        # Keep prompts short so Groq free-tier max_tokens / TPM stay under limit.
        clipped = (raw_text or "")[:1800]
        prompt = f"""Summarize this competitor page into 3-5 short bullets.
Focus on features, UI/UX, pricing, and tone. Skip generic marketing.

Source: {url or "unknown"}

\"\"\"
{clipped}
\"\"\"
"""
        result = self.llm.chat(
            system="You are a concise PM summarizer. Output only bullet points.",
            user=prompt,
            temperature=0.4,
            max_tokens=350,
        )
        if result is None:
            logger.warning("Summarization unavailable for %s (LLM call failed)", url)
        elif looks_like_mojibake(result):
            logger.warning("Summarizer returned corrupted text for %s — discarding", url)
            return None
        return result
