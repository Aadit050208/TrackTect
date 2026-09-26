"""Fetch HTML when plain `requests` is blocked or the page is JS-only.

Order: curl_cffi Chrome impersonate → headless Selenium → (caller may try archive.org).
Returned HTML is still parsed with BeautifulSoup by the scraper.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

_SELENIUM_AVAILABLE = True
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
    from webdriver_manager.chrome import ChromeDriverManager
except ImportError:
    _SELENIUM_AVAILABLE = False

_CHALLENGE_MARKERS = (
    "access denied",
    "request blocked",
    "pardon our interruption",
    "checking your browser",
    "just a moment",
    "enable javascript and cookies",
    "cf-chl-",
    "cf-browser-verification",
    "attention required",
    "verify you are a human",
    "verify you are human",
    "robot or human",
    "captcha",
    "akamai",
    "refused to connect",
)

_IMPERSONATE_PROFILES = (
    "chrome",
    "chrome131",
    "chrome124",
    "safari17_0",
    "edge101",
)


def looks_like_challenge(html: str) -> bool:
    """True for bot-wall / captcha shells that are not the real site."""
    if not html or len(html) < 80:
        return True
    low = html.lower()
    if "#cmsg" in low and len(html) < 4000:
        return True
    if any(m in low for m in _CHALLENGE_MARKERS) and len(html) < 12000:
        return True
    return False


def fetch_impersonated(url: str, timeout: int = 22) -> Optional[str]:
    """TLS fingerprint like a real Chrome — beats many 403s that `requests` hits."""
    try:
        from curl_cffi import requests as creq
    except ImportError:
        logger.info("curl_cffi not installed; skip impersonated fetch")
        return None

    last_err = ""
    for profile in _IMPERSONATE_PROFILES:
        try:
            response = creq.get(
                url,
                impersonate=profile,
                timeout=timeout,
                allow_redirects=True,
                headers={
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;q=0.9,"
                        "image/avif,image/webp,*/*;q=0.8"
                    ),
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
        except Exception as exc:  # noqa: BLE001
            last_err = exc.__class__.__name__
            continue
        html = response.text or ""
        if looks_like_challenge(html):
            last_err = f"HTTP {response.status_code} challenge ({profile})"
            continue
        if response.status_code >= 400 and len(html) < 400:
            last_err = f"HTTP {response.status_code}"
            continue
        if len(html) >= 200:
            logger.info(
                "Impersonated fetch ok for %s via %s (HTTP %s, %s chars)",
                url, profile, response.status_code, len(html),
            )
            return html
        last_err = f"short body ({len(html)} chars)"
    logger.info("Impersonated fetch failed for %s: %s", url, last_err or "no profile worked")
    return None


def selenium_available() -> bool:
    return _SELENIUM_AVAILABLE


def _build_driver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1366,768")
    options.add_argument("--lang=en-US")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.page_load_strategy = "eager"
    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options,
    )
    driver.set_page_load_timeout(28)
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": (
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                )
            },
        )
    except Exception:  # noqa: BLE001
        pass
    return driver


def fetch_selenium(url: str, wait_seconds: float = 4.0) -> Optional[str]:
    """Render the page in headless Chrome (JS apps / leftover bot walls)."""
    if not _SELENIUM_AVAILABLE:
        return None
    driver = None
    try:
        driver = _build_driver()
        driver.get(url)
        try:
            WebDriverWait(driver, 12).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
        except Exception:  # noqa: BLE001
            pass
        time.sleep(max(1.0, wait_seconds))
        html = driver.page_source or ""
        if looks_like_challenge(html):
            logger.info("Selenium still hit a challenge page for %s", url)
            return None
        if len(html) < 200:
            logger.info("Selenium returned a short page for %s", url)
            return None
        logger.info("Selenium fetch ok for %s (%s chars)", url, len(html))
        return html
    except Exception as exc:  # noqa: BLE001
        logger.info("Selenium fetch failed for %s: %s", url, exc.__class__.__name__)
        return None
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:  # noqa: BLE001
                pass
