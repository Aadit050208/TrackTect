"""Orchestrator Agent: plans which sub-agents are worth running for an input.

Input : competitor config (url, optional handles, per-agent toggles).
Output: a Plan dict describing what to run, with human-readable reasons, e.g.

    {
      "scrape_targets": [homepage, changelog?],
      "changelog_url": str | None,
      "run_twitter": bool, "twitter_handle": str | None,
      "run_youtube": bool, "youtube_url": str | None,
      "run_notion": bool,
      "reasons": ["..."],
    }

Currently rule-based (fast, deterministic); the decision points are isolated
here so the logic can be swapped for LLM-based reasoning later without
touching the pipeline.
"""

import logging
from typing import Dict, List, Optional
from urllib.parse import urlparse

import requests

from config import settings
from agents.twitter_agent_selenium import find_twitter_username_from_website, selenium_available
from agents.youtube_agent import find_youtube_channel_from_website

logger = logging.getLogger(__name__)

# Common changelog/release-notes paths, tried in order.
_CHANGELOG_PATHS = ("/changelog", "/release-notes", "/releases", "/whats-new", "/blog/changelog", "/updates")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}


class OrchestratorAgent:
    """Decide which agents to run and where to point them, before the pipeline starts."""

    def discover_changelog(self, url: str) -> Optional[str]:
        """Probe common changelog/release-notes paths on the site's domain."""
        parsed = urlparse(url)
        root = f"{parsed.scheme}://{parsed.netloc}"
        for path in _CHANGELOG_PATHS:
            candidate = root + path
            try:
                response = requests.get(
                    candidate, headers=_HEADERS, timeout=8, allow_redirects=True, stream=True
                )
                content_type = response.headers.get("Content-Type", "")
                response.close()
                if response.status_code == 200 and "text/html" in content_type:
                    # Redirect back to the homepage means the path doesn't really exist.
                    if urlparse(response.url).path.rstrip("/") not in ("", "/"):
                        return candidate
            except requests.exceptions.RequestException:
                continue
        return None

    def plan(
        self,
        url: str,
        twitter_handle: Optional[str] = None,
        youtube_url: Optional[str] = None,
        enable_twitter: bool = True,
        enable_youtube: bool = True,
        enable_notion: bool = True,
    ) -> Dict:
        """Build an execution plan for one competitor URL."""
        reasons: List[str] = []
        plan: Dict = {
            "scrape_targets": [url],
            "changelog_url": None,
            "run_twitter": False,
            "twitter_handle": None,
            "run_youtube": False,
            "youtube_url": None,
            "run_notion": False,
            "run_news": True,
            "reasons": reasons,
        }

        # 1. Prefer a changelog/release-notes page over just the homepage.
        changelog = self.discover_changelog(url)
        if changelog and changelog.rstrip("/") != url.rstrip("/"):
            plan["changelog_url"] = changelog
            plan["scrape_targets"].append(changelog)
            reasons.append(f"Discovered changelog page: {changelog} (will scrape and watch it too)")
        else:
            reasons.append("No changelog/release-notes page discovered; watching the given URL only")

        # 2. Twitter: run only if a handle is given or discoverable AND Selenium exists.
        if not enable_twitter:
            reasons.append("Twitter agent disabled in competitor settings; skipping")
        elif not selenium_available():
            reasons.append("Selenium not installed; skipping Twitter agent")
        else:
            handle = (twitter_handle or "").strip() or find_twitter_username_from_website(url)
            if handle:
                plan["run_twitter"] = True
                plan["twitter_handle"] = handle.lstrip("@")
                source = "provided manually" if twitter_handle else "auto-discovered on site"
                reasons.append(f"Twitter handle @{plan['twitter_handle']} {source}; will scrape tweets")
            else:
                reasons.append("No Twitter handle provided or found on site; skipping Twitter agent")

        # 3. YouTube: same logic.
        if not enable_youtube:
            reasons.append("YouTube agent disabled in competitor settings; skipping")
        elif not selenium_available():
            reasons.append("Selenium not installed; skipping YouTube agent")
        else:
            channel = (youtube_url or "").strip() or find_youtube_channel_from_website(url)
            if channel:
                plan["run_youtube"] = True
                plan["youtube_url"] = channel
                source = "provided manually" if youtube_url else "auto-discovered on site"
                reasons.append(f"YouTube channel {channel} {source}; will scrape videos")
            else:
                reasons.append("No YouTube channel provided or found on site; skipping YouTube agent")

        # 4. Notion: only if configured.
        if not enable_notion:
            reasons.append("Notion push disabled in competitor settings; skipping")
        elif settings.notion_configured:
            plan["run_notion"] = True
            reasons.append("Notion configured; will push digest")
        else:
            reasons.append("Notion not configured (NOTION_TOKEN/NOTION_PAGE_ID unset); skipping Notion push")

        # 5. News / funding / campaigns — always useful for PMs; no API key required.
        plan["run_news"] = True
        reasons.append("News agent enabled; will pull recent funding / campaign / partnership headlines")

        return plan
