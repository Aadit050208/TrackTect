"""Live market + publisher news for Find (and discovery enrichment).

Sources (free, no API key):
  - Google News RSS (via NewsAgent) — works for public and private brands
  - Yahoo Finance via yfinance — ticker quote + related news when a symbol resolves

Sentiment:
  - VADER (vaderSentiment) on headlines — included only when the library loads
    and scoring succeeds. Otherwise sentiment fields are omitted entirely.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Curated brand → primary equity ticker (US unless noted).
# Used when Yahoo Search returns nothing useful for short brand queries.
KNOWN_TICKERS = {
    "apple": "AAPL",
    "microsoft": "MSFT",
    "amazon": "AMZN",
    "google": "GOOGL",
    "alphabet": "GOOGL",
    "meta": "META",
    "facebook": "META",
    "netflix": "NFLX",
    "nvidia": "NVDA",
    "intel": "INTC",
    "amd": "AMD",
    "tesla": "TSLA",
    "nike": "NKE",
    "adidas": "ADDYY",
    "walmart": "WMT",
    "target": "TGT",
    "costco": "COST",
    "starbucks": "SBUX",
    "mcdonalds": "MCD",
    "mcdonald's": "MCD",
    "coca cola": "KO",
    "coca-cola": "KO",
    "pepsi": "PEP",
    "pepsico": "PEP",
    "disney": "DIS",
    "spotify": "SPOT",
    "uber": "UBER",
    "lyft": "LYFT",
    "airbnb": "ABNB",
    "shopify": "SHOP",
    "salesforce": "CRM",
    "adobe": "ADBE",
    "oracle": "ORCL",
    "ibm": "IBM",
    "cisco": "CSCO",
    "samsung": "005930.KS",
    "sony": "SONY",
    "paypal": "PYPL",
    "block": "SQ",
    "square": "SQ",
    "visa": "V",
    "mastercard": "MA",
    "jpmorgan": "JPM",
    "jp morgan": "JPM",
    "goldman sachs": "GS",
    "reliance": "RELIANCE.NS",
    "tcs": "TCS.NS",
    "infosys": "INFY",
    "wipro": "WIPRO.NS",
    "hdfc bank": "HDB",
    "zoho": None,  # private — skip ticker
    "notion": None,
    "figma": None,
    "openai": None,
    "anthropic": None,
    "flipkart": None,
    "swiggy": None,
    "zomato": "ZOMATO.NS",
}

_SENTIMENT_OK = False
_analyzer = None
try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

    _analyzer = SentimentIntensityAnalyzer()
    # Smoke-check: known polarity directions must hold
    _pos = _analyzer.polarity_scores("strong growth and outstanding profits")["compound"]
    _neg = _analyzer.polarity_scores("fraud lawsuit bankruptcy collapse")["compound"]
    if _pos > 0.2 and _neg < -0.2:
        _SENTIMENT_OK = True
    else:
        logger.warning("VADER smoke-check failed; sentiment disabled")
        _analyzer = None
except Exception as exc:  # pragma: no cover
    logger.info("Sentiment unavailable (%s); Find will omit scores", exc.__class__.__name__)
    _analyzer = None
    _SENTIMENT_OK = False


def sentiment_available() -> bool:
    return bool(_SENTIMENT_OK and _analyzer is not None)


def score_sentiment(text: str) -> Optional[Dict[str, Any]]:
    """Return label + compound if VADER is working; else None."""
    if not sentiment_available() or not (text or "").strip():
        return None
    try:
        scores = _analyzer.polarity_scores(text.strip())
        compound = float(scores.get("compound", 0.0))
    except Exception:
        return None
    if compound >= 0.05:
        label = "positive"
    elif compound <= -0.05:
        label = "negative"
    else:
        label = "neutral"
    return {
        "label": label,
        "compound": round(compound, 3),
        "positive": round(float(scores.get("pos", 0)), 3),
        "negative": round(float(scores.get("neg", 0)), 3),
        "neutral": round(float(scores.get("neu", 0)), 3),
    }


def _normalize_query(q: str) -> str:
    return re.sub(r"\s+", " ", (q or "").strip().lower())


def resolve_ticker(query: str) -> Optional[Dict[str, str]]:
    """Map a brand/ticker query to a Yahoo equity symbol when possible."""
    q = (query or "").strip()
    if not q:
        return None
    key = _normalize_query(q)

    if key in KNOWN_TICKERS:
        sym = KNOWN_TICKERS[key]
        if not sym:
            return None
        return {"symbol": sym, "name": q, "match": "known"}

    # Bare ticker-like token
    if re.fullmatch(r"[A-Za-z]{1,5}(\.[A-Za-z]{1,4})?", q.replace(" ", "")):
        return {"symbol": q.replace(" ", "").upper(), "name": q, "match": "symbol"}

    try:
        import yfinance as yf
        from yfinance import Search

        # Prefer "Brand Inc" style for short names that Search mishandles
        candidates = [q]
        if " " not in q and len(q) <= 12:
            candidates.append(f"{q} Inc")
            candidates.append(f"{q} Corp")

        for cand in candidates:
            try:
                result = Search(cand, max_results=8)
            except Exception:
                continue
            quotes = getattr(result, "quotes", None) or []
            equities = [
                x for x in quotes
                if str(x.get("quoteType") or "").upper() in ("EQUITY", "ETF")
                and x.get("symbol")
                and not str(x.get("symbol")).endswith("=F")
            ]
            if not equities:
                continue
            # Prefer US common stock when available
            pick = equities[0]
            for eq in equities:
                sym = str(eq.get("symbol") or "")
                exch = str(eq.get("exchange") or eq.get("exchDisp") or "")
                if "." not in sym and ("NMS" in exch or "NYQ" in exch or "NGM" in exch or not exch):
                    pick = eq
                    break
            return {
                "symbol": str(pick.get("symbol")),
                "name": str(pick.get("shortname") or pick.get("longname") or q),
                "match": "search",
            }
    except Exception as exc:
        logger.warning("Ticker resolve failed for %s: %s", q, exc.__class__.__name__)
    return None


def _yf_item_fields(raw: Dict) -> Optional[Dict[str, str]]:
    content = raw.get("content") if isinstance(raw.get("content"), dict) else raw
    if not isinstance(content, dict):
        return None
    title = (content.get("title") or raw.get("title") or "").strip()
    if not title:
        return None
    provider = content.get("provider") or {}
    source = ""
    if isinstance(provider, dict):
        source = (provider.get("displayName") or "").strip()
    url = ""
    for key in ("canonicalUrl", "clickThroughUrl"):
        block = content.get(key) or {}
        if isinstance(block, dict) and block.get("url"):
            url = block["url"]
            break
    if not url:
        url = (raw.get("link") or raw.get("url") or "").strip()
    published = (content.get("pubDate") or content.get("displayTime") or "").strip()
    summary = (content.get("summary") or content.get("description") or "").strip()
    return {
        "title": title[:240],
        "url": url,
        "source": source or "Yahoo Finance",
        "published": published,
        "summary": summary[:280],
        "channel": "yahoo_finance",
    }


def fetch_yahoo_news(symbol: str, limit: int = 8) -> List[Dict]:
    try:
        import yfinance as yf
    except Exception:
        return []
    try:
        ticker = yf.Ticker(symbol)
        raw_news = list(ticker.news or [])
    except Exception as exc:
        logger.warning("yfinance news failed for %s: %s", symbol, exc.__class__.__name__)
        return []
    out: List[Dict] = []
    for raw in raw_news:
        if not isinstance(raw, dict):
            continue
        item = _yf_item_fields(raw)
        if item:
            out.append(item)
        if len(out) >= limit:
            break
    return out


def fetch_quote(symbol: str) -> Optional[Dict[str, Any]]:
    try:
        import yfinance as yf
    except Exception:
        return None
    try:
        t = yf.Ticker(symbol)
        price = None
        currency = None
        change_pct = None
        try:
            fi = t.fast_info
            price = getattr(fi, "last_price", None) or getattr(fi, "lastPrice", None)
            currency = getattr(fi, "currency", None)
            prev = getattr(fi, "previous_close", None) or getattr(fi, "previousClose", None)
            if price is not None and prev:
                change_pct = ((float(price) - float(prev)) / float(prev)) * 100.0
        except Exception:
            pass
        info = {}
        try:
            info = t.info or {}
        except Exception:
            info = {}
        name = info.get("shortName") or info.get("longName") or symbol
        if price is None and info.get("currentPrice") is not None:
            price = info.get("currentPrice")
        if currency is None:
            currency = info.get("currency")
        if change_pct is None and info.get("regularMarketChangePercent") is not None:
            change_pct = float(info["regularMarketChangePercent"])
        if price is None:
            return {"symbol": symbol, "name": name, "price": None, "currency": currency, "change_pct": None}
        return {
            "symbol": symbol,
            "name": name,
            "price": round(float(price), 2),
            "currency": currency or "",
            "change_pct": round(float(change_pct), 2) if change_pct is not None else None,
        }
    except Exception as exc:
        logger.warning("Quote fetch failed for %s: %s", symbol, exc.__class__.__name__)
        return None


def _dedupe_news(items: List[Dict]) -> List[Dict]:
    seen = set()
    out = []
    for item in items:
        key = (item.get("title") or "").lower().strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _attach_sentiment(items: List[Dict]) -> Tuple[List[Dict], Optional[Dict[str, Any]]]:
    if not sentiment_available():
        for item in items:
            item.pop("sentiment", None)
        return items, None

    compounds = []
    for item in items:
        text = " ".join(
            p for p in (item.get("title"), item.get("summary")) if p
        )
        sent = score_sentiment(text)
        if sent:
            item["sentiment"] = sent
            compounds.append(sent["compound"])
        else:
            item.pop("sentiment", None)

    if not compounds:
        return items, None

    avg = sum(compounds) / len(compounds)
    if avg >= 0.05:
        label = "positive"
    elif avg <= -0.05:
        label = "negative"
    else:
        label = "neutral"
    return items, {
        "label": label,
        "compound": round(avg, 3),
        "count": len(compounds),
    }


def lookup_market_intel(query: str, max_news: int = 10) -> Dict[str, Any]:
    """Combined live intel for a Find query. Safe to call on every search."""
    q = (query or "").strip()
    result: Dict[str, Any] = {
        "query": q,
        "ticker": None,
        "news": [],
        "sentiment_enabled": sentiment_available(),
        "overall_sentiment": None,
        "sources_used": [],
        "note": None,
    }
    if not q:
        return result

    # 1) Google News RSS — always try (private brands included)
    google_items: List[Dict] = []
    try:
        from agents.news_agent import NewsAgent

        agent = NewsAgent(max_items=max_news)
        for raw in agent.fetch(competitor_name=q, url=""):
            google_items.append({
                "title": raw.get("title") or "",
                "url": raw.get("url") or "",
                "source": raw.get("source") or "Google News",
                "published": "",
                "summary": raw.get("summary") or "",
                "signal_type": raw.get("signal_type") or "other",
                "signal_label": raw.get("signal_label") or "News",
                "channel": "google_news",
            })
        if google_items:
            result["sources_used"].append("google_news")
    except Exception:
        logger.exception("Google News lookup failed for %s", q)

    # 2) Yahoo Finance when a public ticker resolves
    resolved = resolve_ticker(q)
    yahoo_items: List[Dict] = []
    if resolved:
        symbol = resolved["symbol"]
        quote = fetch_quote(symbol)
        result["ticker"] = {
            "symbol": symbol,
            "name": (quote or {}).get("name") or resolved.get("name") or symbol,
            "price": (quote or {}).get("price"),
            "currency": (quote or {}).get("currency") or "",
            "change_pct": (quote or {}).get("change_pct"),
            "match": resolved.get("match"),
        }
        yahoo_items = fetch_yahoo_news(symbol, limit=max_news)
        for item in yahoo_items:
            item.setdefault("signal_type", "other")
            item.setdefault("signal_label", "Market news")
        if yahoo_items:
            result["sources_used"].append("yahoo_finance")
    else:
        result["note"] = (
            "No public stock ticker matched this name — showing publisher news only."
        )

    merged = _dedupe_news(yahoo_items + google_items)[:max_news]
    merged, overall = _attach_sentiment(merged)
    result["news"] = merged
    result["overall_sentiment"] = overall

    if not merged and not result["ticker"]:
        result["note"] = "No recent public news found for this query."
    return result


# In-process cache so repeat Find queries stay snappy (TTL ~20 minutes).
_INTEL_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_INTEL_TTL_SEC = 20 * 60


def lookup_market_intel_cached(query: str, max_news: int = 10) -> Dict[str, Any]:
    import time

    key = _normalize_query(query) + f"|{max_news}"
    now = time.time()
    hit = _INTEL_CACHE.get(key)
    if hit and (now - hit[0]) < _INTEL_TTL_SEC:
        cached = dict(hit[1])
        cached["cached"] = True
        return cached
    fresh = lookup_market_intel(query, max_news=max_news)
    fresh["cached"] = False
    _INTEL_CACHE[key] = (now, fresh)
    # Bound cache size
    if len(_INTEL_CACHE) > 64:
        oldest = sorted(_INTEL_CACHE.items(), key=lambda kv: kv[1][0])[:16]
        for k, _ in oldest:
            _INTEL_CACHE.pop(k, None)
    return fresh
