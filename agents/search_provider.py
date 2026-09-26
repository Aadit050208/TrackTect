"""Swappable web search for discovery.

Configure via SEARCH_PROVIDER / SEARCH_API_KEY / SEARCH_BASE_URL in `.env`.
Agent code should only call `discover_source(query)` — never a specific vendor.

Primary strategies:
1. Clearbit company autocomplete (name → official domain) — free, no key
2. DuckDuckGo Instant Answer JSON
3. Wikipedia / Wikidata (browser User-Agent)
4. DuckDuckGo HTML lite SERP as fallback
5. Optional Brave / custom providers
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

import requests
from bs4 import BeautifulSoup

from config import settings

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_SKIP_HOSTS = (
    "wikipedia.org", "wikidata.org", "linkedin.com", "facebook.com",
    "twitter.com", "x.com", "instagram.com", "youtube.com", "crunchbase.com",
    "reddit.com", "medium.com", "duckduckgo.com", "google.com", "bing.com",
)


def discover_source(query: str, limit: int = 5) -> List[Dict]:
    """Return search hits: [{title, url, snippet}, ...]. Never raises."""
    provider = (settings.search_provider or "duckduckgo").lower()
    results: List[Dict] = []
    try:
        results.extend(_search_clearbit(query, limit=limit))
        results.extend(_search_ddg_instant(query, limit=limit))
        results.extend(_search_wikipedia(query, limit=limit))

        if provider in ("brave", "brave_api"):
            results.extend(_search_brave(query, limit=limit))
        elif provider == "custom" and settings.search_base_url:
            results.extend(_search_custom(query, limit=limit))

        if len([r for r in results if r.get("source") != "wikipedia"]) < 2:
            results.extend(_search_ddg_html(query, limit=limit))

        seen = set()
        unique: List[Dict] = []
        for item in results:
            url = (item.get("url") or "").rstrip("/")
            if not url or url in seen:
                continue
            host = urlparse(url).netloc.lower()
            src = item.get("source") or ""
            if any(skip in host for skip in _SKIP_HOSTS) and src not in ("wikipedia", "wikidata"):
                continue
            seen.add(url)
            unique.append(item)
            if len(unique) >= limit:
                break
        return unique
    except Exception as exc:  # noqa: BLE001
        logger.warning("Search failed (%s): %s", provider, exc.__class__.__name__)
        return results[:limit]


def _brand_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def _search_clearbit(query: str, limit: int = 5) -> List[Dict]:
    """Clearbit autocomplete — maps company names to official domains (no API key)."""
    q = (query or "").strip()
    q = re.sub(
        r"\b(official|website|company|corp|inc|ltd|homepage|site)\b",
        " ",
        q,
        flags=re.I,
    )
    q = " ".join(q.split())
    if len(q) < 2:
        return []
    try:
        response = requests.get(
            "https://autocomplete.clearbit.com/v1/companies/suggest",
            params={"query": q},
            headers=_HEADERS,
            timeout=10,
        )
        response.raise_for_status()
        rows = response.json()
    except (requests.exceptions.RequestException, ValueError) as exc:
        logger.info("Clearbit suggest unavailable: %s", exc.__class__.__name__)
        return []

    slug = _brand_slug(q)
    scored: List[tuple] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        domain = (row.get("domain") or "").strip().lower()
        name = (row.get("name") or "").strip()
        if not domain or "." not in domain:
            continue
        if any(skip in domain for skip in _SKIP_HOSTS):
            continue
        dslug = _brand_slug(domain.split(".")[0])
        nslug = _brand_slug(name)
        # Exact brand ↔ domain / name match
        if nslug == slug or dslug == slug:
            score = 100
        # Short names (CRED, Ola): only accept exact / near-exact — avoid Credit Karma etc.
        elif len(slug) <= 4:
            if dslug.startswith(slug) and len(dslug) <= len(slug) + 1:
                score = 80
            elif nslug.startswith(slug) and len(nslug) <= len(slug) + 2:
                score = 60
            else:
                score = 0
        elif slug and (slug in dslug or dslug in slug or slug in nslug or nslug in slug):
            score = 70
        elif slug and len(slug) >= 5 and (slug[:4] in dslug or slug[:4] in nslug):
            score = 40
        else:
            score = 10
        if score < 40:
            continue
        scored.append((score, name, domain))

    scored.sort(key=lambda t: -t[0])
    out: List[Dict] = []
    for score, name, domain in scored:
        if score < 40 and out:
            break
        for prefix in ("https://www.", "https://"):
            out.append({
                "title": f"{name} official website",
                "url": f"{prefix}{domain}",
                "snippet": f"Company domain from Clearbit ({domain})",
                "source": "clearbit",
                "score": score,
            })
        if len(out) >= limit * 2:
            break
    return out[: limit * 2]


def _search_ddg_instant(query: str, limit: int = 5) -> List[Dict]:
    try:
        response = requests.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
            headers=_HEADERS,
            timeout=12,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.exceptions.RequestException, ValueError) as exc:
        logger.info("DDG instant answer unavailable: %s", exc.__class__.__name__)
        return []

    out: List[Dict] = []
    for entry in ((data.get("Infobox") or {}).get("content") or []):
        if not isinstance(entry, dict):
            continue
        if entry.get("data_type") == "official_website" or (
            str(entry.get("label") or "").lower() in ("official website", "website")
        ):
            value = entry.get("value") or ""
            if isinstance(value, dict):
                value = value.get("url") or value.get("text") or ""
            if isinstance(value, str) and value.startswith("http"):
                out.append({
                    "title": f"{query} official website",
                    "url": value,
                    "snippet": "Official website (knowledge graph)",
                    "source": "ddg_infobox",
                })

    abstract = data.get("AbstractURL") or ""
    if abstract.startswith("http"):
        out.append({
            "title": data.get("Heading") or query,
            "url": abstract,
            "snippet": (data.get("AbstractText") or "")[:240],
            "source": "ddg_abstract",
        })

    for topic in (data.get("RelatedTopics") or [])[:limit]:
        if not isinstance(topic, dict):
            continue
        url = topic.get("FirstURL") or ""
        if url.startswith("http"):
            out.append({
                "title": (topic.get("Text") or "")[:80],
                "url": url,
                "snippet": topic.get("Text") or "",
                "source": "ddg_related",
            })
    return out[:limit]


def _search_ddg_html(query: str, limit: int = 5) -> List[Dict]:
    try:
        response = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": f"{query} official website"},
            headers=_HEADERS,
            timeout=15,
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        logger.info("DDG HTML search unavailable: %s", exc.__class__.__name__)
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    out: List[Dict] = []
    for res in soup.select(".result")[: limit + 4]:
        a = res.select_one("a.result__a")
        if not a or not a.get("href"):
            continue
        href = a["href"]
        if "uddg=" in href:
            try:
                href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
            except Exception:
                pass
        if not href.startswith("http"):
            continue
        host = urlparse(href).netloc.lower()
        if any(skip in host for skip in _SKIP_HOSTS):
            continue
        snippet_el = res.select_one(".result__snippet")
        out.append({
            "title": a.get_text(" ", strip=True)[:120],
            "url": href,
            "snippet": (snippet_el.get_text(" ", strip=True) if snippet_el else "")[:240],
            "source": "ddg_html",
        })
        if len(out) >= limit:
            break
    return out


def _search_wikipedia(query: str, limit: int = 5) -> List[Dict]:
    out: List[Dict] = []
    try:
        response = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "opensearch",
                "search": query,
                "limit": min(limit, 5),
                "namespace": 0,
                "format": "json",
            },
            headers={**_HEADERS, "Api-User-Agent": "TrackTect/1.0 (competitor research)"},
            timeout=12,
        )
        response.raise_for_status()
        payload = response.json()
        titles = payload[1] if len(payload) > 1 else []
        urls = payload[3] if len(payload) > 3 else []
    except (requests.exceptions.RequestException, ValueError, IndexError) as exc:
        logger.info("Wikipedia search unavailable: %s", exc.__class__.__name__)
        return []

    for title, url in zip(titles, urls):
        out.append({
            "title": title,
            "url": url,
            "snippet": f"Wikipedia: {title}",
            "source": "wikipedia",
        })
        official = _wikipedia_official_website(title)
        if official:
            out.insert(0, {
                "title": f"{title} official website",
                "url": official,
                "snippet": "Official website (Wikipedia / Wikidata)",
                "source": "wikidata",
            })
    return out[: limit + 2]


def _wikipedia_official_website(title: str) -> Optional[str]:
    prefer = title
    if title.lower() in ("amazon", "apple", "meta", "oracle", "target"):
        prefer = f"{title} (company)"
    try:
        response = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "prop": "pageprops",
                "titles": prefer,
                "format": "json",
                "ppprop": "wikibase_item",
                "redirects": 1,
            },
            headers={**_HEADERS, "Api-User-Agent": "TrackTect/1.0 (competitor research)"},
            timeout=10,
        )
        response.raise_for_status()
        pages = (response.json().get("query") or {}).get("pages") or {}
        qid = None
        for page in pages.values():
            qid = (page.get("pageprops") or {}).get("wikibase_item")
            if qid:
                break
        if not qid:
            return None

        wd = requests.get(
            f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json",
            headers=_HEADERS,
            timeout=10,
        )
        wd.raise_for_status()
        entity = (wd.json().get("entities") or {}).get(qid) or {}
        claims = entity.get("claims") or {}
        for claim in claims.get("P856") or []:
            value = (
                ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value")
            )
            if isinstance(value, str) and value.startswith("http"):
                return value
    except (requests.exceptions.RequestException, ValueError, KeyError) as exc:
        logger.debug("Wikidata official site lookup failed: %s", exc.__class__.__name__)
    return None


def _search_brave(query: str, limit: int = 5) -> List[Dict]:
    if not settings.search_api_key:
        logger.warning("SEARCH_PROVIDER=brave but SEARCH_API_KEY is empty")
        return []
    response = requests.get(
        "https://api.search.brave.com/res/v1/web/search",
        headers={
            **_HEADERS,
            "Accept": "application/json",
            "X-Subscription-Token": settings.search_api_key,
        },
        params={"q": query, "count": limit},
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    results = []
    for item in (data.get("web") or {}).get("results") or []:
        results.append({
            "title": item.get("title") or "",
            "url": item.get("url") or "",
            "snippet": item.get("description") or "",
            "source": "brave",
        })
        if len(results) >= limit:
            break
    return results


def _search_custom(query: str, limit: int = 5) -> List[Dict]:
    headers = {**_HEADERS, "Accept": "application/json"}
    if settings.search_api_key:
        headers["Authorization"] = f"Bearer {settings.search_api_key}"
    response = requests.get(
        settings.search_base_url,
        headers=headers,
        params={"q": query, "limit": limit},
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    items = data.get("results") or data.get("items") or []
    out = []
    for item in items[:limit]:
        if isinstance(item, dict) and item.get("url"):
            out.append({
                "title": item.get("title") or "",
                "url": item["url"],
                "snippet": item.get("snippet") or item.get("description") or "",
                "source": "custom",
            })
    return out


def guess_brand_domains(name: str) -> List[str]:
    """Heuristic official domains for a brand name."""
    slug = re.sub(r"[^a-z0-9]+", "", (name or "").lower())
    if len(slug) < 2:
        return []
    tlds = (
        ".com", ".in", ".co.in", ".co", ".io", ".ai", ".app", ".so", ".club",
        ".net", ".org", ".tech", ".store", ".co.uk",
    )
    urls = []
    for tld in tlds:
        urls.append(f"https://www.{slug}{tld}")
        urls.append(f"https://{slug}{tld}")
    if not slug.endswith("s") and len(slug) > 3:
        plural = slug + "s"
        for tld in (".com", ".in", ".co.in"):
            urls.append(f"https://www.{plural}{tld}")
            urls.append(f"https://{plural}{tld}")
    return urls
