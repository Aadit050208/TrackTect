"""YouTube Agent: discovers a competitor's YouTube channel and scrapes videos.

- `find_youtube_channel_from_website(url)` : scan a site's links for a channel.
- `YouTubeAgent(channel_url).scrape()`     : recent videos (title, url,
  description, top comments) via headless Chrome.

Selenium/Chrome being unavailable is a supported state: `scrape()` returns []
and sets `self.last_error` instead of raising. One retry with a longer wait is
attempted when the first pass finds no videos.
"""

import logging
import re
import time
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_SELENIUM_AVAILABLE = True
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from webdriver_manager.chrome import ChromeDriverManager
except ImportError:
    _SELENIUM_AVAILABLE = False


class YouTubeAgent:
    """Scrape recent videos from a channel with headless Chrome.

    Input : channel URL, max_videos.
    Output: list of {"title", "url", "description", "comments"} dicts
            ([] on failure; see `last_error`).
    """

    def __init__(self, channel_url: str, max_videos: int = 5) -> None:
        self.channel_url = channel_url.rstrip("/")
        self.max_videos = max_videos
        self.last_error: Optional[str] = None

    def _build_driver(self):
        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

    def _scrape_once(self, wait_seconds: int) -> List[Dict]:
        driver = self._build_driver()
        try:
            driver.get(self.channel_url + "/videos")
            time.sleep(wait_seconds)

            elements = driver.find_elements(By.XPATH, '//a[@id="video-title-link" or @id="video-title"]')
            logger.info("Found %d video links on %s", len(elements), self.channel_url)

            videos: List[Dict] = []
            targets = []
            for element in elements[: self.max_videos]:
                title = element.get_attribute("title") or element.text
                href = element.get_attribute("href")
                if href:
                    targets.append((title, href))

            for title, href in targets:
                try:
                    driver.get(href)
                    time.sleep(4)

                    description = "No description found."
                    desc_elems = driver.find_elements(
                        By.XPATH, '//div[@id="description"]//yt-formatted-string | //ytd-text-inline-expander//yt-formatted-string'
                    )
                    if desc_elems:
                        description = desc_elems[0].text or description

                    comment_elems = driver.find_elements(
                        By.XPATH, '//ytd-comment-thread-renderer//yt-formatted-string[@id="content-text"]'
                    )
                    comments = [c.text for c in comment_elems[:5] if c.text]

                    videos.append({"title": title, "url": href, "description": description, "comments": comments})
                except Exception as exc:  # noqa: BLE001 - per-video failures shouldn't stop the rest
                    logger.warning("Error processing video %s: %s", href, exc.__class__.__name__)

            return videos
        finally:
            driver.quit()

    def scrape(self) -> List[Dict]:
        """Return recent video data; [] with `last_error` set on failure."""
        if not _SELENIUM_AVAILABLE:
            self.last_error = "Selenium is not installed"
            logger.warning("YouTube agent skipped: %s", self.last_error)
            return []

        try:
            videos = self._scrape_once(wait_seconds=6)
            if not videos:
                logger.info("No videos found on %s first pass; retrying with longer wait", self.channel_url)
                videos = self._scrape_once(wait_seconds=12)
            if not videos:
                self.last_error = "no videos found (channel may be empty or blocked scraping)"
            return videos
        except Exception as exc:  # noqa: BLE001 - WebDriver errors vary widely
            self.last_error = f"browser automation failed ({exc.__class__.__name__})"
            logger.warning("YouTube scrape failed for %s: %s", self.channel_url, exc)
            return []


def find_youtube_channel_from_website(url: str) -> Optional[str]:
    """Scan a website's outbound links for a YouTube channel URL."""
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        logger.warning("Could not fetch %s to discover YouTube channel: %s", url, exc.__class__.__name__)
        return None

    soup = BeautifulSoup(response.content, "html.parser")
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if any(marker in href for marker in ("youtube.com/channel/", "youtube.com/c/", "youtube.com/@", "youtube.com/user/")):
            match = re.search(r"https?://(?:www\.)?youtube\.com/[^\"'\s?#]+", href)
            if match:
                return match.group(0)
    return None
