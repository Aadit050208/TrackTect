"""Twitter Agent: discovers a competitor's Twitter/X handle and scrapes tweets.

- `find_twitter_username_from_website(url)` : scan a site's links for a handle.
- `TwitterSeleniumScraper(handle).scrape()`  : recent tweets via headless Chrome.

Selenium/Chrome being unavailable is a supported state: `scrape()` returns []
and sets `self.last_error` instead of raising. One retry with a longer wait is
attempted when the first pass finds no tweets.
"""

import logging
import re
import time
from typing import List, Optional

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


def selenium_available() -> bool:
    """Whether the Selenium stack is importable (Chrome may still be missing)."""
    return _SELENIUM_AVAILABLE


class TwitterSeleniumScraper:
    """Scrape recent tweets from a public profile with headless Chrome.

    Input : handle (without @), max_tweets.
    Output: list of tweet text strings ([] on failure; see `last_error`).
    """

    def __init__(self, username: str, max_tweets: int = 5) -> None:
        self.username = username.lstrip("@")
        self.max_tweets = max_tweets
        self.last_error: Optional[str] = None

    def _build_driver(self):
        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

    def _scrape_once(self, wait_seconds: int) -> List[str]:
        driver = self._build_driver()
        try:
            driver.get(f"https://twitter.com/{self.username}")
            time.sleep(wait_seconds)
            elements = driver.find_elements(By.XPATH, '//article[@role="article"]')
            logger.info("Found %d tweet elements on @%s", len(elements), self.username)
            tweets = []
            for element in elements[: self.max_tweets]:
                try:
                    if element.text.strip():
                        tweets.append(element.text)
                except Exception:  # noqa: BLE001 - stale elements are expected
                    continue
            return tweets
        finally:
            driver.quit()

    def scrape(self) -> List[str]:
        """Return recent tweet texts; [] with `last_error` set on failure."""
        if not _SELENIUM_AVAILABLE:
            self.last_error = "Selenium is not installed"
            logger.warning("Twitter agent skipped: %s", self.last_error)
            return []

        try:
            tweets = self._scrape_once(wait_seconds=5)
            if not tweets:
                logger.info("No tweets found for @%s on first pass; retrying with longer wait", self.username)
                tweets = self._scrape_once(wait_seconds=10)
            if not tweets:
                self.last_error = "no tweets found (profile may be empty, protected, or blocked scraping)"
            return tweets
        except Exception as exc:  # noqa: BLE001 - WebDriver errors vary widely
            self.last_error = f"browser automation failed ({exc.__class__.__name__})"
            logger.warning("Twitter scrape failed for @%s: %s", self.username, exc)
            return []


def find_twitter_username_from_website(url: str) -> Optional[str]:
    """Scan a website's outbound links for a twitter.com/x.com profile handle."""
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        logger.warning("Could not fetch %s to discover Twitter handle: %s", url, exc.__class__.__name__)
        return None

    soup = BeautifulSoup(response.content, "html.parser")
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if ("twitter.com" in href or "x.com" in href) and not any(
            skip in href for skip in ("intent", "share", "search", "hashtag")
        ):
            match = re.search(r"(?:twitter|x)\.com/([A-Za-z0-9_]{1,15})(?:[/?#]|$)", href)
            if match and match.group(1).lower() not in ("home", "login", "i"):
                return match.group(1)
    return None
