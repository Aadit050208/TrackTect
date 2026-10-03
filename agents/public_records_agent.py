"""Public records beyond the website, Google News, and a stock quote.

Plain HTTP only, so this runs on Render without Chrome. One shared time budget
and a short timeout on every call: a blocked exchange or ad site is skipped
and the check continues.

Nothing here uses a nickname list. A result from someone else's site is kept
only when the company name is in the title. A feed, job board, repo, or store
listing is kept when the company's own site links to it, or when the feed is
on the company's own host.
"""

from __future__ import annotations

import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from agents.http_text import safe_accept_encoding
from agents.news_agent import NewsAgent, headline_is_about_company

logger = logging.getLogger(__name__)

_BUDGET_SECONDS = 22
_TIMEOUT = 5
_MAX_ITEMS = 12
_BODY_CAP = 400_000

_HEADERS = {
    "User-Agent": (
        "TrackTect/1.0 (public-page research; +https://tracktect.local)"
    ),
    "Accept": "text/html,application/json,application/rss+xml,application/atom+xml,*/*",
    "Accept-Encoding": safe_accept_encoding(),
}

# SEC asks clients to identify themselves. A default Python agent is blocked.
_SEC_HEADERS = {
    **_HEADERS,
    "User-Agent": "TrackTect research bot admin@tracktect.local",
    "Accept": "application/atom+xml,application/xml,text/xml,*/*",
}

_Fetcher = Callable[[str, float, Dict[str, str]], Optional[Tuple[int, str, str]]]


def _host(url: str) -> str:
    return (urlparse(url).netloc or "").lower().removeprefix("www.")


def _same_site(page_url: str, other_url: str) -> bool:
    a, b = _host(page_url), _host(other_url)
    if not a or not b:
        return False
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def _clip(text: str, limit: int = 240) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


class PublicRecordsAgent:
    """Collect filings, feeds, jobs, apps, and related public records."""

    def __init__(
        self, fetcher: Optional[_Fetcher] = None, budget_seconds: float = _BUDGET_SECONDS
    ) -> None:
        self._fetcher = fetcher or self._http_get
        self._budget = budget_seconds
        self.last_note: str = ""

    def collect(self, name: str, url: str) -> List[Dict]:
        """Return news-shaped items. Never raises."""
        brand = (name or "").strip()
        page = (url or "").strip()
        if not brand and not page:
            return []
        deadline = time.monotonic() + self._budget
        found: List[Dict] = []
        seen = set()

        def add(item: Optional[Dict]) -> None:
            if not item or len(found) >= _MAX_ITEMS:
                return
            title = _clip(item.get("title") or "", 240)
            if not title:
                return
            key = title.lower()
            if key in seen:
                return
            seen.add(key)
            item = dict(item)
            item["title"] = title
            item["url"] = _clip(item.get("url") or "", 500)
            item["summary"] = _clip(item.get("summary") or "", 300)
            item["signal_type"] = item.get("signal_type") or "other"
            found.append(item)

        html = ""
        final_url = page
        if page and self._remaining(deadline) > 1:
            got = self._fetch(page, deadline)
            if got:
                _status, html, final_url = got

        steps = (
            lambda: self._company_feeds(brand, page, html, deadline, add),
            lambda: self._status_page(page, html, deadline, add),
            lambda: self._job_boards(html, final_url or page, deadline, add),
            lambda: self._github_releases(html, final_url or page, deadline, add),
            lambda: self._app_stores(brand, html, final_url or page, deadline, add),
            lambda: self._sec_filings(brand, deadline, add),
            lambda: self._bse_filings(brand, deadline, add),
            lambda: self._trustpilot(brand, page, deadline, add),
            lambda: self._product_hunt(brand, html, final_url or page, deadline, add),
            lambda: self._ad_libraries(brand, deadline, add),
        )
        for step in steps:
            if len(found) >= _MAX_ITEMS:
                break
            if self._remaining(deadline) < 1:
                self.last_note = (
                    f"Public records time budget reached; proceeding with {len(found)} record(s)"
                )
                logger.info(self.last_note)
                break
            try:
                step()
            except Exception as exc:  # noqa: BLE001
                logger.info("Public record step skipped: %s", exc.__class__.__name__)

        if not self.last_note:
            self.last_note = f"{len(found)} public record(s)" if found else "no public records found"
        return found

    def _remaining(self, deadline: float) -> float:
        return deadline - time.monotonic()

    def _fetch(
        self, url: str, deadline: float, headers: Optional[Dict[str, str]] = None
    ) -> Optional[Tuple[int, str, str]]:
        left = self._remaining(deadline)
        if left < 1 or not url:
            return None
        timeout = min(_TIMEOUT, left)
        try:
            return self._fetcher(url, timeout, headers or _HEADERS)
        except Exception as exc:  # noqa: BLE001
            logger.info("Public source skipped %s: %s", url[:140], exc.__class__.__name__)
            return None

    @staticmethod
    def _http_get(url: str, timeout: float, headers: Dict[str, str]) -> Optional[Tuple[int, str, str]]:
        response = requests.get(
            url, headers=headers, timeout=timeout, allow_redirects=True, stream=True
        )
        try:
            if response.status_code >= 400:
                logger.info("Public source HTTP %s for %s", response.status_code, url[:140])
                return None
            chunks = []
            size = 0
            for chunk in response.iter_content(chunk_size=16384):
                if not chunk:
                    continue
                chunks.append(chunk)
                size += len(chunk)
                if size >= _BODY_CAP:
                    break
            raw = b"".join(chunks)
            text = raw.decode(response.encoding or "utf-8", errors="replace")
            return response.status_code, text, response.url
        finally:
            response.close()

    def _company_feeds(self, brand, page, html, deadline, add) -> None:
        feed_urls = []
        if html and page:
            soup = BeautifulSoup(html, "html.parser")
            for link in soup.find_all("link"):
                typ = (link.get("type") or "").lower()
                href = link.get("href") or ""
                if href and ("rss" in typ or "atom" in typ or href.rstrip("/").endswith(("/feed", "/rss"))):
                    feed_urls.append(urljoin(page, href))
        if page and not feed_urls and self._remaining(deadline) > 1:
            feed_urls.append(urljoin(page, "/feed"))
        for feed_url in feed_urls[:2]:
            got = self._fetch(feed_url, deadline)
            if not got:
                continue
            _status, body, final = got
            own_host = _same_site(page, final) or _same_site(page, feed_url)
            for title, link in _parse_feed(body)[:4]:
                if not own_host and not _about(brand, title):
                    logger.info("Feed item discarded (not about %s): %s", brand, title[:160])
                    continue
                signal = NewsAgent.classify_headline(title)
                add({
                    "signal_type": signal,
                    "title": title,
                    "url": link or final,
                    "summary": "Company feed",
                })

    def _status_page(self, page, html, deadline, add) -> None:
        if not html or not page:
            return
        soup = BeautifulSoup(html, "html.parser")
        status_url = ""
        for anchor in soup.find_all("a", href=True):
            href = urljoin(page, anchor["href"])
            host = _host(href)
            if "statuspage.io" in host or host.startswith("status.") or href.rstrip("/").endswith("/status"):
                status_url = href
                break
        if not status_url:
            return
        summary_url = status_url.rstrip("/") + "/api/v2/summary.json"
        if "statuspage.io" not in _host(status_url) and not _host(status_url).startswith("status."):
            summary_url = urljoin(status_url, "/api/v2/summary.json")
        got = self._fetch(summary_url, deadline)
        if not got:
            return
        try:
            payload = json.loads(got[1])
        except (TypeError, ValueError):
            return
        status = (payload.get("status") or {}).get("description") or ""
        if status:
            add({
                "signal_type": "status",
                "title": f"Status: {status}",
                "url": status_url,
                "summary": "Company status page",
            })
        for incident in (payload.get("incidents") or [])[:2]:
            name = (incident.get("name") or "").strip()
            if name:
                add({
                    "signal_type": "status",
                    "title": name,
                    "url": incident.get("shortlink") or status_url,
                    "summary": "Status page incident",
                })

    def _job_boards(self, html, page, deadline, add) -> None:
        if not html:
            return
        boards = _job_board_urls(html, page)
        for kind, api_url in boards[:2]:
            got = self._fetch(api_url, deadline)
            if not got:
                continue
            try:
                payload = json.loads(got[1])
            except (TypeError, ValueError):
                continue
            for title, link, where in _job_titles(kind, payload)[:5]:
                add({
                    "signal_type": "hiring",
                    "title": f"Open role: {title}",
                    "url": link,
                    "summary": where or kind,
                })

    def _github_releases(self, html, page, deadline, add) -> None:
        repo = _github_repo(html, page)
        if not repo:
            return
        api = f"https://api.github.com/repos/{repo}/releases?per_page=2"
        got = self._fetch(api, deadline, {**_HEADERS, "Accept": "application/vnd.github+json"})
        if not got:
            return
        try:
            payload = json.loads(got[1])
        except (TypeError, ValueError):
            return
        if not isinstance(payload, list):
            return
        for release in payload[:2]:
            if not isinstance(release, dict):
                continue
            title = (release.get("name") or release.get("tag_name") or "").strip()
            if not title:
                continue
            add({
                "signal_type": "product_launch",
                "title": title,
                "url": release.get("html_url") or "",
                "summary": "GitHub release",
            })

    def _app_stores(self, brand, html, page, deadline, add) -> None:
        linked = _store_links(html, page)
        for link in linked[:2]:
            if "apps.apple.com" in _host(link) or "itunes.apple.com" in _host(link):
                app_id = _apple_id(link)
                if not app_id:
                    continue
                got = self._fetch(f"https://itunes.apple.com/lookup?id={app_id}", deadline)
                self._keep_itunes(brand, got, add, linked_from_site=True)
            elif "play.google.com" in _host(link):
                got = self._fetch(link, deadline)
                if not got:
                    continue
                title = _og_title(got[1])
                if title and (linked or _about(brand, title)):
                    add({
                        "signal_type": "app_update",
                        "title": title,
                        "url": got[2] or link,
                        "summary": "Google Play listing linked from the company site",
                    })
        if not brand or self._remaining(deadline) < 1:
            return
        query = quote_plus(brand)
        got = self._fetch(
            f"https://itunes.apple.com/search?term={query}&entity=software&limit=5",
            deadline,
        )
        self._keep_itunes(brand, got, add, linked_from_site=False)

    def _keep_itunes(self, brand, got, add, linked_from_site: bool) -> None:
        if not got:
            return
        try:
            payload = json.loads(got[1])
        except (TypeError, ValueError):
            return
        for app in (payload.get("results") or [])[:5]:
            title = (app.get("trackName") or "").strip()
            if not title:
                continue
            if not linked_from_site and not _about(brand, title):
                logger.info("App store result discarded (not about %s): %s", brand, title[:160])
                continue
            version = (app.get("version") or "").strip()
            notes = _clip(app.get("releaseNotes") or "", 180)
            summary = "App Store"
            if version:
                summary = f"App Store version {version}"
            if notes:
                summary = f"{summary}. {notes}"
            add({
                "signal_type": "app_update",
                "title": title if not version else f"{title} {version}",
                "url": app.get("trackViewUrl") or "",
                "summary": summary,
            })
            if linked_from_site:
                break

    def _sec_filings(self, brand, deadline, add) -> None:
        if not brand:
            return
        query = quote_plus(brand)
        url = (
            "https://www.sec.gov/cgi-bin/browse-edgar"
            f"?action=getcompany&company={query}&owner=include&count=5&output=atom"
        )
        got = self._fetch(url, deadline, _SEC_HEADERS)
        if not got:
            return
        for title, link in _parse_feed(got[1])[:4]:
            if not _about(brand, title):
                logger.info("SEC filing discarded (not about %s): %s", brand, title[:160])
                continue
            add({
                "signal_type": "filing",
                "title": title,
                "url": link,
                "summary": "SEC filing",
            })

    def _bse_filings(self, brand, deadline, add) -> None:
        if not brand:
            return
        suggest = self._fetch(
            "https://api.bseindia.com/BseIndiaAPI/api/SuggestScrip/w?flag=EQ&val="
            + quote_plus(brand),
            deadline,
        )
        if not suggest:
            return
        code = _bse_scrip(brand, suggest[1])
        if not code:
            logger.info("BSE lookup discarded (no exact company match for %s)", brand)
            return
        today = time.strftime("%Y%m%d", time.gmtime())
        start = time.strftime("%Y%m%d", time.gmtime(time.time() - 21 * 86400))
        url = (
            "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w"
            f"?strCat=-1&strPrevDate={start}&strScrip={code}&strSearch=P"
            f"&strToDate={today}&strType=C"
        )
        got = self._fetch(url, deadline)
        if not got:
            return
        try:
            payload = json.loads(got[1])
        except (TypeError, ValueError):
            return
        rows = payload.get("Table") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return
        for row in rows[:3]:
            if not isinstance(row, dict):
                continue
            title = (row.get("NEWSSUB") or row.get("HEADLINE") or "").strip()
            if not title or not _about(brand, title):
                if title:
                    logger.info("BSE filing discarded (not about %s): %s", brand, title[:160])
                continue
            add({
                "signal_type": "filing",
                "title": title,
                "url": row.get("NSURL") or "",
                "summary": "BSE announcement",
            })

    def _trustpilot(self, brand, page, deadline, add) -> None:
        host = _host(page)
        if not host:
            return
        got = self._fetch(f"https://www.trustpilot.com/review/{host}", deadline)
        if not got:
            return
        if host not in (got[2] or ""):
            logger.info("Trustpilot redirected away from %s", host)
            return
        rating = _trustpilot_rating(got[1])
        if not rating:
            return
        score, count = rating
        summary = "Public rating for this company's domain. Individual review text is not stored."
        title = f"Trustpilot rating {score}"
        if count:
            title = f"{title} from {count} reviews"
        add({
            "signal_type": "review_theme",
            "title": title,
            "url": got[2],
            "summary": summary,
        })

    def _product_hunt(self, brand, html, page, deadline, add) -> None:
        link = _first_link(html, page, "producthunt.com")
        if not link:
            return
        got = self._fetch(link, deadline)
        if not got:
            return
        title = _og_title(got[1])
        if not title or not _about(brand, title):
            if title:
                logger.info("Product Hunt discarded (not about %s): %s", brand, title[:160])
            return
        add({
            "signal_type": "product_launch",
            "title": title,
            "url": got[2] or link,
            "summary": "Product Hunt listing linked from the company site",
        })

    def _ad_libraries(self, brand, deadline, add) -> None:
        if not brand:
            return
        query = quote_plus(brand)
        targets = (
            (
                "https://www.facebook.com/ads/library/?active_status=active&ad_type=all"
                f"&country=ALL&q={query}&search_type=keyword_unordered",
                "Meta Ad Library",
            ),
            (
                f"https://adstransparency.google.com/?region=anywhere&query={query}",
                "Google Ads Transparency",
            ),
        )
        for url, source in targets:
            if self._remaining(deadline) < 1:
                break
            got = self._fetch(url, deadline)
            if not got:
                continue
            body = got[1]
            if _login_wall(body):
                logger.info("%s unavailable without a browser for %s", source, brand)
                continue
            snippet = _ad_snippet(brand, body)
            if not snippet:
                logger.info("%s had no ad text naming %s", source, brand)
                continue
            add({
                "signal_type": "marketing_campaign",
                "title": snippet,
                "url": got[2] or url,
                "summary": source,
            })


def _about(brand: str, title: str) -> bool:
    ok, _reason = headline_is_about_company(brand, title)
    return ok


def _parse_feed(body: str) -> List[Tuple[str, str]]:
    items: List[Tuple[str, str]] = []
    try:
        root = ET.fromstring((body or "").strip().encode("utf-8", errors="replace"))
    except ET.ParseError:
        return items
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        title = ""
        link = ""
        for child in list(node):
            ctag = child.tag.rsplit("}", 1)[-1]
            if ctag == "title" and child.text:
                title = " ".join(child.text.split())
            elif ctag == "link":
                link = (child.get("href") or child.text or "").strip()
        if title:
            items.append((title, link))
    return items


def _job_board_urls(html: str, page: str) -> List[Tuple[str, str]]:
    found: List[Tuple[str, str]] = []
    soup = BeautifulSoup(html or "", "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = urljoin(page or "", anchor["href"])
        host = _host(href)
        path = [part for part in urlparse(href).path.split("/") if part]
        if not path:
            continue
        token = path[0]
        if token.lower() in ("embed", "job_board", "jobs", "v1"):
            continue
        if host.endswith("greenhouse.io"):
            found.append(("greenhouse", f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"))
        elif host.endswith("lever.co"):
            found.append(("lever", f"https://api.lever.co/v0/postings/{token}?mode=json"))
        elif host.endswith("ashbyhq.com"):
            found.append(("ashby", f"https://api.ashbyhq.com/posting-api/job-board/{token}"))
    deduped = []
    seen = set()
    for item in found:
        if item[1] in seen:
            continue
        seen.add(item[1])
        deduped.append(item)
    return deduped


def _job_titles(kind: str, payload) -> List[Tuple[str, str, str]]:
    rows = []
    if kind == "greenhouse":
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        for job in jobs or []:
            if not isinstance(job, dict):
                continue
            loc = job.get("location") or {}
            where = loc.get("name") if isinstance(loc, dict) else ""
            rows.append((job.get("title") or "", job.get("absolute_url") or "", where or "Greenhouse"))
    elif kind == "lever":
        for job in payload if isinstance(payload, list) else []:
            if not isinstance(job, dict):
                continue
            cats = job.get("categories") or {}
            where = cats.get("location") if isinstance(cats, dict) else ""
            rows.append((job.get("text") or "", job.get("hostedUrl") or "", where or "Lever"))
    elif kind == "ashby":
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        for job in jobs or []:
            if not isinstance(job, dict):
                continue
            rows.append((job.get("title") or "", job.get("jobUrl") or "", job.get("location") or "Ashby"))
    return [(t, u, w) for t, u, w in rows if t]


def _github_repo(html: str, page: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = urljoin(page or "", anchor["href"])
        if _host(href) != "github.com":
            continue
        parts = [p for p in urlparse(href).path.split("/") if p]
        if len(parts) >= 2 and parts[0] not in ("orgs", "topics", "features"):
            return f"{parts[0]}/{parts[1]}"
    return ""


def _store_links(html: str, page: str) -> List[str]:
    links = []
    soup = BeautifulSoup(html or "", "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = urljoin(page or "", anchor["href"])
        host = _host(href)
        if host in ("apps.apple.com", "itunes.apple.com", "play.google.com"):
            links.append(href)
    return links


def _apple_id(url: str) -> str:
    match = re.search(r"/id(\d+)", url or "")
    return match.group(1) if match else ""


def _og_title(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    tag = soup.find("meta", property="og:title") or soup.find("meta", attrs={"name": "og:title"})
    if tag and tag.get("content"):
        return " ".join(tag["content"].split())
    if soup.title and soup.title.string:
        return " ".join(soup.title.string.split())
    return ""


def _bse_scrip(brand: str, body: str) -> str:
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return ""
    rows = payload
    if isinstance(payload, dict):
        rows = payload.get("SearchResults") or payload.get("Table") or []
    if not isinstance(rows, list):
        return ""
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = (row.get("scrip_name") or row.get("Scrip_Name") or row.get("name") or "").strip()
        code = str(row.get("scrip_cd") or row.get("scrip_id") or row.get("Scrip_Cd") or "").strip()
        if code and name and _about(brand, name):
            return code
    return ""


def _trustpilot_rating(html: str) -> Optional[Tuple[str, str]]:
    for match in re.finditer(
        r'<script type="application/ld\+json">(.*?)</script>', html or "", flags=re.S
    ):
        try:
            payload = json.loads(match.group(1))
        except (TypeError, ValueError):
            continue
        blocks = payload if isinstance(payload, list) else [payload]
        for block in blocks:
            if not isinstance(block, dict):
                continue
            rating = block.get("aggregateRating") or {}
            if not isinstance(rating, dict):
                continue
            score = str(rating.get("ratingValue") or "").strip()
            count = str(rating.get("reviewCount") or rating.get("ratingCount") or "").strip()
            if score:
                return score, count
    return None


def _first_link(html: str, page: str, host_part: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = urljoin(page or "", anchor["href"])
        if host_part in _host(href):
            return href
    return ""


def _login_wall(html: str) -> bool:
    lowered = (html or "")[:4000].lower()
    return "log in to facebook" in lowered or "login" in lowered and "password" in lowered and len(html or "") < 8000


def _ad_snippet(brand: str, html: str) -> str:
    text = BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)
    text = " ".join(text.split())
    if not text or not _about(brand, text[:240]):
        # The company name may sit later in a long page. Search a window around it.
        lowered = text.lower()
        phrase = " ".join((brand or "").lower().split())
        at = lowered.find(phrase) if phrase else -1
        if at < 0:
            return ""
        window = text[max(0, at - 40): at + 180]
        if not _about(brand, window):
            return ""
        return _clip(window, 180)
    return _clip(text[:180], 180)
