"""Head-to-head compare analysis: one JSON LLM call, cached, with an honest fallback."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from agents.llm_client import LLMClient, reset_token_usage, take_token_usage

logger = logging.getLogger(__name__)

DIMENSIONS = [
    "Pricing & offers",
    "Products & features",
    "UX & messaging",
    "Marketing & campaigns",
    "Partnerships",
    "Hiring & careers",
]

VALID_EDGES = {"A", "B", "even", "insufficient_data", "not_directly_comparable"}
VALID_CONFIDENCE = {"low", "medium", "high"}

_CAT_TO_DIM = {
    "Pricing Change": "Pricing & offers",
    "Discount / Offer": "Pricing & offers",
    "Feature Update": "Products & features",
    "New Product / Launch": "Products & features",
    "UI/UX Change": "UX & messaging",
    "Tone Shift": "UX & messaging",
    "Marketing Campaign": "Marketing & campaigns",
    "Funding / Partnership": "Partnerships",
    "Partnership / Collab": "Partnerships",
    "Careers": "Hiring & careers",
    # Section brief labels
    "Pricing & Packaging": "Pricing & offers",
    "Products & Features": "Products & features",
    "UX & Messaging": "UX & messaging",
    "Marketing & Campaigns": "Marketing & campaigns",
    "Funding & Partnerships": "Partnerships",
    "Careers & Hiring": "Hiring & careers",
    # News signal types (same pool as insights — no bypass)
    "funding": "Partnerships",
    "partnership": "Partnerships",
    "marketing_campaign": "Marketing & campaigns",
    "product_launch": "Products & features",
    "hiring": "Hiring & careers",
    "app_update": "Products & features",
}

# Meta / non-comparable phrasing (pricing hidden, "no changes", thin notes).
_META_PATTERNS = (
    r"\bpricing not (shown|public|available|disclosed|listed)\b",
    r"\bprice(s|ing)? (not|aren't|isn't|not shown|not public)\b",
    r"\bnot (shown|public|disclosed|available) in (the )?excerpt\b",
    r"\blikely displayed on\b",
    r"\bno (clear )?(pricing|price|offer|discount)\b",
    r"\bno .{0,40} changes?\b",
    r"\bnot enough .{0,30} (data|signal|history)\b",
    r"\binsufficient\b",
    r"\bn/?a\b",
    r"\bunavailable\b",
    r"\bcontact (us |sales )?for (pricing|a quote)\b",
    r"\brequest (a )?demo\b.*\bpric",
)

# Actual commercial/pricing signal (numbers, plans, discounts).
_SUBSTANTIVE_PRICING = (
    r"\$\s?\d",
    r"₹\s?\d",
    r"€\s?\d",
    r"\b\d+\s?%\s?(off|discount)\b",
    r"\b(mrp|rrp|list price|starting at|from \$|plans? (start|from)|tier|subscription)\b",
    r"\b(discount|coupon|promo code|sale|offer|deal)\b",
    r"\b(raised|cut|lowered|increased|changed) .{0,20}price",
)

_LLM_TIMEOUT = 60
_LLM_MAX_TOKENS = 2500

_SYSTEM = (
    "You compare two competitors for a product manager using only the supplied "
    "evidence. Reply with one JSON object and nothing else."
)

# Flat schema keyed by dimension name; refs as space-separated strings.
# Keep the prompt short — reasoning models spend completion budget on chain-of-thought.
_USER_TEMPLATE = """Compare A={name_a} vs B={name_b}. Evidence is the only source of truth.

JSON shape (one object):
{{
  "verdict": "2-3 sentences",
  "pm_takeaways": ["...", "...", "..."],
  "signals": [{{"hypothesis":"...","confidence":"low|medium|high","refs":"i-1"}}],
  "dims": {{
    "Pricing & offers": {{"a":[],"b":[],"edge":"insufficient_data","refs":""}},
    "Products & features": {{"a":[],"b":[],"edge":"insufficient_data","refs":""}},
    "UX & messaging": {{"a":[],"b":[],"edge":"insufficient_data","refs":""}},
    "Marketing & campaigns": {{"a":[],"b":[],"edge":"insufficient_data","refs":""}},
    "Partnerships": {{"a":[],"b":[],"edge":"insufficient_data","refs":""}},
    "Hiring & careers": {{"a":[],"b":[],"edge":"insufficient_data","refs":""}}
  }}
}}

edge must be one of: A, B, even, insufficient_data, not_directly_comparable.
Rules:
- No invention. Every point and every edge must be grounded in the Evidence refs below.
- Never mark A or B ahead unless you cite at least one real Evidence ref id (like i-12 or s-3) in that dim's "refs".
- If a dimension has no usable Evidence items, edge must be insufficient_data (empty a/b is fine).
- Use not_directly_comparable when sides are unlike (e.g. "pricing not public" vs marketing copy).
- Never compare a disclosed metric against an undisclosed one as if both are known values — say clearly when one side has no data for that metric (e.g. funding, pricing, headcount).
- Max 3 signals (or []); each signal needs real Evidence refs. Include all six dims keys.
Window: {days} days.
Evidence:
{payload}
"""

_RETRY_SUFFIX = (
    "\n\nPrevious reply was not valid JSON. Return only one JSON object matching the schema. "
    "No markdown, no commentary."
)


def _truncate_for_log(text: str, limit: int = 500) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"


def _strip_fences(text: str) -> str:
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned.strip())
    return cleaned.strip()


def _repair_json_text(blob: str) -> str:
    """Fix common model JSON slips: trailing commas, odd quotes, truncated braces."""
    s = blob.strip()
    # Smart quotes → ASCII
    s = s.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    # Trailing commas before } or ]
    s = re.sub(r",\s*([}\]])", r"\1", s)
    # Single-quoted keys → double (simple cases, no variable-width lookbehind)
    s = re.sub(r"'([^'\\]+)'\s*:", r'"\1":', s)
    # Truncated mid-string
    if s.count('"') % 2 == 1:
        s += '"'
    opens = s.count("{") - s.count("}")
    if opens > 0:
        s += "}" * opens
    opens_b = s.count("[") - s.count("]")
    if opens_b > 0:
        s += "]" * opens_b
    # Drop dangling comma after last repair
    s = re.sub(r",\s*([}\]])", r"\1", s)
    return s


def parse_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Strip fences, repair common errors, parse first JSON object. None if unusable."""
    if not text or not str(text).strip():
        return None
    cleaned = _strip_fences(text)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    blob = cleaned[start : end + 1]
    for candidate in (blob, _repair_json_text(blob)):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def parse_json_array(text: str) -> Optional[List[Any]]:
    """Parse a JSON array, or an object wrapping one under a common key."""
    if not text or not str(text).strip():
        return None
    cleaned = _strip_fences(text)
    obj = parse_json_object(cleaned)
    if isinstance(obj, dict):
        for key in ("sections", "items", "data", "result", "results"):
            val = obj.get(key)
            if isinstance(val, list):
                return val
        # Single-section object mistaken for array wrapper
        if "section" in obj:
            return [obj]
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start < 0 or end <= start:
        return None
    blob = cleaned[start : end + 1]
    for candidate in (blob, _repair_json_text(blob)):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            return data
    return None


def log_unusable_json(label: str, raw: Optional[str]) -> None:
    logger.warning(
        "%s returned unusable JSON (raw[:500]=%r)",
        label,
        _truncate_for_log(raw or ""),
    )


def _str_list(value: Any, cap: int = 6) -> List[str]:
    if isinstance(value, str) and value.strip():
        # Space-separated refs string
        if " " in value and all(len(p) < 40 for p in value.split()):
            parts = [p.strip() for p in value.split() if p.strip()]
            if parts and all(re.match(r"^[a-z]-?\d+$", p, re.I) or p.startswith(("i-", "s-", "n-", "m-")) for p in parts):
                return parts[:cap]
        return [value.strip()][:cap]
    if not isinstance(value, list):
        return []
    out: List[str] = []
    for item in value:
        text = str(item).strip() if item is not None else ""
        if text:
            out.append(text)
        if len(out) >= cap:
            break
    return out


def _refs_list(value: Any, cap: int = 8) -> List[str]:
    if isinstance(value, str):
        return [p for p in value.replace(",", " ").split() if p.strip()][:cap]
    return _str_list(value, cap)


def _empty_dimension(name: str) -> Dict[str, Any]:
    return {
        "name": name,
        "a_stronger_points": [],
        "b_stronger_points": [],
        "edge": "insufficient_data",
        "evidence": [],
    }


def _normalize_dims_map(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Accept either dims{} map (preferred) or dimensions[] array from the model."""
    by_name: Dict[str, Dict[str, Any]] = {}

    dims_map = data.get("dims")
    if isinstance(dims_map, dict):
        for key, item in dims_map.items():
            if not isinstance(item, dict):
                continue
            name = str(key).strip()
            if name not in DIMENSIONS:
                lowered = name.lower()
                for canonical in DIMENSIONS:
                    if canonical.lower() in lowered or lowered in canonical.lower():
                        name = canonical
                        break
            if name not in DIMENSIONS:
                continue
            edge = str(item.get("edge") or "insufficient_data").strip()
            if edge not in VALID_EDGES:
                edge = "insufficient_data"
            by_name[name] = {
                "name": name,
                "a_stronger_points": _str_list(item.get("a") or item.get("a_stronger_points"), 5),
                "b_stronger_points": _str_list(item.get("b") or item.get("b_stronger_points"), 5),
                "edge": edge,
                "evidence": _refs_list(item.get("refs") or item.get("evidence"), 8),
            }

    incoming = data.get("dimensions")
    if isinstance(incoming, list):
        for item in incoming:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if name not in DIMENSIONS:
                lowered = name.lower()
                for canonical in DIMENSIONS:
                    if canonical.lower() in lowered or lowered in canonical.lower():
                        name = canonical
                        break
            if name not in DIMENSIONS or name in by_name:
                continue
            edge = str(item.get("edge") or "insufficient_data").strip()
            if edge not in VALID_EDGES:
                edge = "insufficient_data"
            by_name[name] = {
                "name": name,
                "a_stronger_points": _str_list(item.get("a_stronger_points") or item.get("a"), 5),
                "b_stronger_points": _str_list(item.get("b_stronger_points") or item.get("b"), 5),
                "edge": edge,
                "evidence": _refs_list(item.get("evidence") or item.get("refs"), 8),
            }

    return [by_name.get(name) or _empty_dimension(name) for name in DIMENSIONS]


def validate_analysis(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Coerce parsed JSON into the render shape. Missing keys get safe defaults."""
    data = raw if isinstance(raw, dict) else {}
    dimensions = _normalize_dims_map(data)

    verdict = str(data.get("verdict") or "").strip()
    if not verdict:
        verdict = (
            "There isn’t enough overlapping signal in this window for a firm overall call. "
            "Treat the dimension notes as a starting point, not a ranking."
        )

    takeaways = _str_list(data.get("pm_takeaways"), 5)
    if len(takeaways) < 3:
        for extra in (
            "Watch the dimensions marked “not enough data” after the next completed check.",
            "Use the raw changes below to verify anything you might act on.",
            "Re-run this comparison after both companies have a recent check.",
        ):
            if extra not in takeaways:
                takeaways.append(extra)
            if len(takeaways) >= 3:
                break

    signals: List[Dict[str, Any]] = []
    raw_signals = data.get("signals")
    if isinstance(raw_signals, list):
        for item in raw_signals[:3]:
            if not isinstance(item, dict):
                continue
            hypothesis = str(item.get("hypothesis") or "").strip()
            if not hypothesis:
                continue
            confidence = str(item.get("confidence") or "low").strip().lower()
            if confidence not in VALID_CONFIDENCE:
                confidence = "low"
            evidence = _refs_list(item.get("evidence") or item.get("refs"), 6)
            signals.append({
                "hypothesis": hypothesis,
                "confidence": confidence,
                "evidence": evidence,
            })

    return {
        "dimensions": dimensions,
        "verdict": verdict,
        "pm_takeaways": takeaways[:5],
        "signals": signals,
        "fallback": bool(data.get("fallback")),
        "notice": str(data.get("notice") or "").strip(),
        "a_id": data.get("a_id"),
        "b_id": data.get("b_id"),
    }


def known_evidence_refs(*packs: Dict[str, Any]) -> set:
    """All evidence ref ids that were (or could be) supplied to the model."""
    refs: set = set()
    for pack in packs:
        for item in pack.get("items") or []:
            ref = (item.get("ref") or "").strip()
            if ref:
                refs.add(ref)
    return refs


def ground_analysis(
    analysis: Dict[str, Any],
    known_refs: set,
) -> Dict[str, Any]:
    """Reject A/B edges and signals that lack real evidence citations.

    Prompt rules alone are not enough — this is a hard post-process gate.
    """
    known = {str(r).strip() for r in (known_refs or set()) if str(r).strip()}
    out = dict(analysis)
    dims = []
    for dim in analysis.get("dimensions") or []:
        item = dict(dim)
        raw_evidence = list(item.get("evidence") or [])
        valid = [r for r in raw_evidence if r in known]
        dropped = [r for r in raw_evidence if r not in known]
        if dropped:
            logger.warning(
                "Dropped unknown evidence refs for dimension %r: %s",
                item.get("name"),
                dropped,
            )
        item["evidence"] = valid
        edge = item.get("edge")
        if edge in ("A", "B") and not valid:
            logger.warning(
                "Discarded ungrounded edge claim for dimension %r "
                "(edge=%s, no valid evidence)",
                item.get("name"),
                edge,
            )
            item["edge"] = "insufficient_data"
            # Do not leave fabricated strength bullets on screen.
            item["a_stronger_points"] = []
            item["b_stronger_points"] = []
        dims.append(item)
    out["dimensions"] = dims

    kept_signals = []
    for sig in analysis.get("signals") or []:
        raw_ev = list(sig.get("evidence") or [])
        valid = [r for r in raw_ev if r in known]
        if not valid:
            logger.warning(
                "Discarded ungrounded signal hypothesis %r (no valid evidence)",
                (sig.get("hypothesis") or "")[:120],
            )
            continue
        kept = dict(sig)
        kept["evidence"] = valid
        kept_signals.append(kept)
    out["signals"] = kept_signals
    return out


def _pack_for_prompt(pack: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = []
    for item in pack.get("items") or []:
        row = {
            "ref": item.get("ref"),
            "kind": item.get("kind"),
            "category": item.get("category"),
            "severity": item.get("severity"),
            "text": item.get("text"),
        }
        reason = (item.get("reason") or "").strip()
        if reason:
            row["reason"] = reason
        items.append(row)
    return items


def item_dimension(item: Dict[str, Any]) -> Optional[str]:
    """Map a packed compare item to one of the six head-to-head dimensions."""
    cat = item.get("category") or ""
    dim = _CAT_TO_DIM.get(cat)
    if dim:
        return dim
    kind = item.get("kind") or ""
    lowered = cat.lower()
    text_l = (item.get("text") or "").lower()
    blob = f"{lowered} {text_l}"
    if kind == "messaging" or "ux" in lowered or "messag" in lowered:
        return "UX & messaging"
    if "pric" in blob or "offer" in blob or "discount" in blob:
        return "Pricing & offers"
    if "product" in lowered or "feature" in lowered or "launch" in lowered:
        return "Products & features"
    if "market" in lowered or "campaign" in lowered:
        return "Marketing & campaigns"
    if "partner" in blob or "fund" in blob or "invest" in blob:
        return "Partnerships"
    if (
        "career" in blob
        or "hiring" in blob
        or "hire" in blob
        or "open role" in blob
        or "job opening" in blob
        or re.search(r"\bjobs?\b", blob)
    ):
        return "Hiring & careers"
    return None


# Back-compat alias used by fallback scoring.
_item_dimension = item_dimension


def _sev_rank(item: Dict[str, Any]) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(
        (item.get("severity") or "low").lower(), 3
    )


def select_balanced_items(
    candidates: List[Dict[str, Any]],
    *,
    budget: int = 15,
    per_dimension: int = 2,
    company_label: str = "",
) -> List[Dict[str, Any]]:
    """Pick a representative set: guarantee top signals per dimension, then fill.

    News / sections / insights compete in the same pool — nothing bypasses the cap.
    """
    budget = max(1, int(budget))
    per_dimension = max(1, int(per_dimension))
    by_dim: Dict[str, List[Dict[str, Any]]] = {d: [] for d in DIMENSIONS}
    other: List[Dict[str, Any]] = []
    for item in candidates:
        dim = item_dimension(item)
        if dim:
            by_dim[dim].append(item)
        else:
            other.append(item)

    def _rank_pool(pool: List[Dict[str, Any]]) -> None:
        # High severity first; within a severity, newest first (stable sorts).
        pool.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        pool.sort(key=_sev_rank)

    for dim in DIMENSIONS:
        _rank_pool(by_dim[dim])
    _rank_pool(other)

    selected: List[Dict[str, Any]] = []
    seen_refs = set()
    included_counts: Dict[str, int] = {d: 0 for d in DIMENSIONS}

    def _take(item: Dict[str, Any], dim: Optional[str]) -> bool:
        ref = item.get("ref") or id(item)
        if ref in seen_refs or len(selected) >= budget:
            return False
        seen_refs.add(ref)
        selected.append(item)
        if dim:
            included_counts[dim] = included_counts.get(dim, 0) + 1
        return True

    # Pass 1: up to per_dimension from each compare dimension
    for dim in DIMENSIONS:
        for item in by_dim[dim][:per_dimension]:
            _take(item, dim)

    # Pass 2: fill remaining budget with leftover highest-severity across dims, then other
    leftovers: List[Tuple[Optional[str], Dict[str, Any]]] = []
    for dim in DIMENSIONS:
        for item in by_dim[dim][per_dimension:]:
            leftovers.append((dim, item))
    for item in other:
        leftovers.append((None, item))
    leftovers.sort(key=lambda pair: pair[1].get("created_at") or "", reverse=True)
    leftovers.sort(key=lambda pair: _sev_rank(pair[1]))
    for dim, item in leftovers:
        if len(selected) >= budget:
            break
        _take(item, dim)

    label = company_label or "company"
    for dim in DIMENSIONS:
        available = len(by_dim[dim])
        kept = included_counts.get(dim, 0)
        logger.info(
            "Compare select [%s] %s: %s signal(s) included (%s available)",
            label, dim, kept, available,
        )
    return selected


def _is_meta_text(text: str) -> bool:
    lower = (text or "").lower()
    if not lower.strip():
        return True
    return any(re.search(p, lower) for p in _META_PATTERNS)


def _is_substantive_pricing(text: str) -> bool:
    lower = (text or "").lower()
    if _is_meta_text(lower):
        return False
    return any(re.search(p, lower) for p in _SUBSTANTIVE_PRICING)


def _signal_quality(item: Dict[str, Any], dim: str) -> str:
    """Return 'substantive', 'meta', or 'thin' for edge decisions."""
    text = (item.get("text") or "").strip()
    if not text or _is_meta_text(text):
        return "meta"
    if dim == "Pricing & offers":
        if _is_substantive_pricing(text):
            return "substantive"
        # Pricing-labelled but just marketing / "not public" adjacent
        if any(w in text.lower() for w in ("price", "pricing", "plan", "tier", "discount", "offer", "$", "₹")):
            # Mentions pricing language without numbers/discounts → weak
            if _is_meta_text(text):
                return "meta"
            return "thin"
        return "thin"
    if dim == "Partnerships":
        lower = text.lower()
        if any(w in lower for w in ("partner", "funding", "raised", "series", "invest", "collab", "acquisition")):
            return "substantive"
        return "thin"
    # Other dimensions: non-meta insight/news/section counts as substantive enough to observe
    kind = item.get("kind") or ""
    if kind in ("insight", "news") and len(text) >= 25:
        return "substantive"
    if kind == "section" and len(text) >= 25:
        return "thin"
    return "thin" if len(text) >= 20 else "meta"


def _dim_buckets(pack: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Per-dimension items classified by quality."""
    out: Dict[str, Dict[str, Any]] = {
        name: {"substantive": [], "thin": [], "meta": [], "refs": []}
        for name in DIMENSIONS
    }
    for item in pack.get("items") or []:
        dim = _item_dimension(item)
        if not dim:
            continue
        quality = _signal_quality(item, dim)
        out[dim][quality].append(item)
        ref = (item.get("ref") or "").strip()
        if ref and ref not in out[dim]["refs"] and len(out[dim]["refs"]) < 6:
            out[dim]["refs"].append(ref)
    return out


def _sample_texts(items: List[Dict[str, Any]], cap: int = 3) -> List[str]:
    out = []
    for item in items:
        text = (item.get("text") or "").strip()
        if text and text not in out:
            out.append(text)
        if len(out) >= cap:
            break
    return out


def fallback_analysis(
    pack_a: Dict[str, Any],
    pack_b: Dict[str, Any],
    name_a: str,
    name_b: str,
    days: int,
) -> Dict[str, Any]:
    """Honest count/observation summary — never invents an 'ahead' from unlike evidence."""
    ba = _dim_buckets(pack_a)
    bb = _dim_buckets(pack_b)
    dimensions = []
    comparable_edges: List[str] = []
    incomparable: List[str] = []

    for name in DIMENSIONS:
        a_sub = ba[name]["substantive"]
        b_sub = bb[name]["substantive"]
        a_meta = ba[name]["meta"]
        b_meta = bb[name]["meta"]
        a_thin = ba[name]["thin"]
        b_thin = bb[name]["thin"]
        a_any = a_sub or a_thin or a_meta
        b_any = b_sub or b_thin or b_meta

        a_pts: List[str] = []
        b_pts: List[str] = []
        edge = "insufficient_data"
        evidence = (ba[name]["refs"] + bb[name]["refs"])[:8]

        if not a_any and not b_any:
            dimensions.append(_empty_dimension(name))
            incomparable.append(name)
            continue

        # Pricing: meta "not public" vs marketing copy → not comparable
        if name == "Pricing & offers":
            a_has_real = bool(a_sub)
            b_has_real = bool(b_sub)
            a_only_meta = bool(a_meta) and not a_sub and not a_thin
            b_only_meta = bool(b_meta) and not b_sub and not b_thin
            a_weak = bool(a_thin or a_meta) and not a_sub
            b_weak = bool(b_thin or b_meta) and not b_sub

            if a_has_real and b_has_real:
                # Both have real pricing/discount moves — still don't crown a winner on counts alone
                edge = "even"
                a_pts = _sample_texts(a_sub)
                b_pts = _sample_texts(b_sub)
                comparable_edges.append(name)
            elif a_has_real and not b_has_real:
                if b_only_meta or b_weak or not b_any:
                    edge = "not_directly_comparable"
                    a_pts = _sample_texts(a_sub)
                    if b_meta or b_weak:
                        b_pts = [
                            f"{name_b} doesn't show comparable public pricing in this window "
                            f"({_sample_texts(b_meta or b_thin, 1)[0] if (b_meta or b_thin) else 'no pricing signal'})."
                        ]
                    else:
                        b_pts = [f"No public pricing change stored for {name_b} in this window."]
                    incomparable.append(name)
                else:
                    edge = "insufficient_data"
                    a_pts = _sample_texts(a_sub)
                    b_pts = _sample_texts(b_thin or b_meta) or [
                        f"No clear pricing change for {name_b}."
                    ]
                    incomparable.append(name)
            elif b_has_real and not a_has_real:
                if a_only_meta or a_weak or not a_any:
                    edge = "not_directly_comparable"
                    b_pts = _sample_texts(b_sub)
                    if a_meta or a_weak:
                        a_pts = [
                            f"{name_a} doesn't show comparable public pricing in this window "
                            f"({_sample_texts(a_meta or a_thin, 1)[0] if (a_meta or a_thin) else 'no pricing signal'})."
                        ]
                    else:
                        a_pts = [f"No public pricing change stored for {name_a} in this window."]
                    incomparable.append(name)
                else:
                    edge = "insufficient_data"
                    b_pts = _sample_texts(b_sub)
                    a_pts = _sample_texts(a_thin or a_meta) or [
                        f"No clear pricing change for {name_a}."
                    ]
                    incomparable.append(name)
            else:
                # Neither has substantive pricing — observe only
                edge = "not_directly_comparable" if (a_meta or b_meta or a_thin or b_thin) else "insufficient_data"
                a_pts = _sample_texts(a_thin or a_meta) or [
                    f"No pricing change stored for {name_a}."
                ]
                b_pts = _sample_texts(b_thin or b_meta) or [
                    f"No pricing change stored for {name_b}."
                ]
                incomparable.append(name)

            dimensions.append({
                "name": name,
                "a_stronger_points": a_pts[:3],
                "b_stronger_points": b_pts[:3],
                "edge": edge,
                "evidence": evidence,
            })
            continue

        # Partnerships: require partnership-like substantive on both sides to say even;
        # never crown a winner from funding vs unrelated product copy.
        if name == "Partnerships":
            if a_sub and b_sub:
                edge = "even"
                a_pts = _sample_texts(a_sub)
                b_pts = _sample_texts(b_sub)
                comparable_edges.append(name)
            elif a_sub and not b_sub:
                edge = "not_directly_comparable" if (b_thin or b_meta or not b_any) else "insufficient_data"
                a_pts = _sample_texts(a_sub)
                b_pts = _sample_texts(b_thin or b_meta) or [
                    f"No partnership/funding signal stored for {name_b}."
                ]
                incomparable.append(name)
            elif b_sub and not a_sub:
                edge = "not_directly_comparable" if (a_thin or a_meta or not a_any) else "insufficient_data"
                b_pts = _sample_texts(b_sub)
                a_pts = _sample_texts(a_thin or a_meta) or [
                    f"No partnership/funding signal stored for {name_a}."
                ]
                incomparable.append(name)
            else:
                edge = "insufficient_data"
                a_pts = _sample_texts(a_thin or a_meta)
                b_pts = _sample_texts(b_thin or b_meta)
                incomparable.append(name)
            dimensions.append({
                "name": name,
                "a_stronger_points": a_pts[:3],
                "b_stronger_points": b_pts[:3],
                "edge": edge,
                "evidence": evidence,
            })
            continue

        # Other dimensions: only "even" when both have substantive signal;
        # never declare A/B ahead from raw counts.
        if a_sub and b_sub:
            edge = "even"
            a_pts = _sample_texts(a_sub)
            b_pts = _sample_texts(b_sub)
            comparable_edges.append(name)
        elif a_sub and not b_sub:
            edge = "insufficient_data"
            a_pts = _sample_texts(a_sub)
            b_pts = _sample_texts(b_thin or b_meta) or [
                f"No clear {name.lower()} signal for {name_b} in this window."
            ]
            incomparable.append(name)
        elif b_sub and not a_sub:
            edge = "insufficient_data"
            b_pts = _sample_texts(b_sub)
            a_pts = _sample_texts(a_thin or a_meta) or [
                f"No clear {name.lower()} signal for {name_a} in this window."
            ]
            incomparable.append(name)
        else:
            edge = "insufficient_data"
            a_pts = _sample_texts(a_thin or a_meta)
            b_pts = _sample_texts(b_thin or b_meta)
            incomparable.append(name)

        dimensions.append({
            "name": name,
            "a_stronger_points": a_pts[:3],
            "b_stronger_points": b_pts[:3],
            "edge": edge,
            "evidence": evidence,
        })

    a_n = int(pack_a.get("insight_count") or 0)
    b_n = int(pack_b.get("insight_count") or 0)
    a_high = sum(v.get("high", 0) for v in (pack_a.get("counts") or {}).values())
    b_high = sum(v.get("high", 0) for v in (pack_b.get("counts") or {}).values())

    solid_bits = [
        f"{name_a}: {a_n} classified change(s) ({a_high} high-priority)",
        f"{name_b}: {b_n} classified change(s) ({b_high} high-priority)",
    ]
    if comparable_edges:
        solid_bits.append(
            "Dimensions with overlapping signal (observation only, not a ranking): "
            + ", ".join(comparable_edges)
            + "."
        )
    if incomparable:
        solid_bits.append(
            "No fair head-to-head on: " + ", ".join(incomparable[:4])
            + ("…" if len(incomparable) > 4 else "")
            + "."
        )

    if a_n == 0 and b_n == 0:
        verdict = (
            f"Neither {name_a} nor {name_b} has classified changes in the last {days} days. "
            "Wait for completed checks before treating this as a comparison."
        )
    else:
        verdict = (
            f"Over the last {days} days — {solid_bits[0]}; {solid_bits[1]}. "
            + (" ".join(solid_bits[2:]) if len(solid_bits) > 2 else "")
            + " This is a data snapshot, not a winner/loser scorecard."
        )

    takeaways = [
        "Treat dimensions marked “not enough data” or “not directly comparable” as open questions, not losses.",
        "Use the raw side-by-side below to verify any observation before acting.",
    ]
    if a_high or b_high:
        focus = name_a if a_high >= b_high else name_b
        takeaways.insert(
            0,
            f"Start with {focus}'s high-priority items — that's where the stored urgency is concentrated.",
        )
    takeaways.append(
        "Re-run after both companies have a fresh check if you need a fairer read on thin dimensions."
    )

    return validate_analysis({
        "dimensions": dimensions,
        "verdict": verdict,
        "pm_takeaways": takeaways[:5],
        "signals": [],
        "fallback": True,
        "notice": (
            "Using a data-based snapshot of stored changes (counts, categories, and what’s "
            "actually comparable). Dimensions without a fair like-for-like signal are marked "
            "accordingly — not as wins or losses."
        ),
    })


def _chat(llm: LLMClient, user: str, system: str = _SYSTEM) -> Optional[str]:
    return llm.chat(
        system=system,
        user=user,
        temperature=0.15,
        max_tokens=_LLM_MAX_TOKENS,
        timeout=_LLM_TIMEOUT,
        json_mode=True,
    )


def generate_analysis(
    pack_a: Dict[str, Any],
    pack_b: Dict[str, Any],
    *,
    name_a: str,
    name_b: str,
    a_id: int,
    b_id: int,
    days: int,
) -> Dict[str, Any]:
    """One LLM call (plus one JSON retry). Falls back to honest counts if that fails."""
    reset_token_usage()
    # Keep a similar token budget, but preserve per-dimension coverage (don't
    # re-starve careers with a naive first-N slice after balanced packing).
    def _slim(items):
        balanced = select_balanced_items(
            list(items),
            budget=14,
            per_dimension=2,
            company_label="prompt",
        )
        out = []
        for item in balanced:
            out.append({
                "ref": item.get("ref"),
                "cat": item.get("category"),
                "sev": item.get("severity"),
                "text": (item.get("text") or "")[:140],
            })
        return out

    payload = {
        "A": {"name": name_a, "items": _slim(_pack_for_prompt(pack_a))},
        "B": {"name": name_b, "items": _slim(_pack_for_prompt(pack_b))},
    }
    user = _USER_TEMPLATE.format(
        name_a=name_a,
        name_b=name_b,
        days=days,
        payload=json.dumps(payload, ensure_ascii=False),
    )

    llm = LLMClient()
    parsed: Optional[Dict[str, Any]] = None
    raw = _chat(llm, user) if llm.configured and not llm.is_cooling_down() else None
    if raw:
        parsed = parse_json_object(raw)
        if parsed is None:
            log_unusable_json("Compare analysis", raw)
            logger.warning("Compare analysis JSON unusable; retrying once with stricter instruction")
            raw = _chat(llm, user + _RETRY_SUFFIX)
            parsed = parse_json_object(raw) if raw else None
            if parsed is None:
                log_unusable_json("Compare analysis (retry)", raw)
    else:
        logger.warning("Compare analysis LLM call returned no text")

    tokens = take_token_usage()
    if tokens:
        logger.info(
            "Compare analysis tokens=%s a=%s b=%s days=%s",
            tokens, a_id, b_id, days,
        )

    if parsed is None:
        analysis = fallback_analysis(pack_a, pack_b, name_a, name_b, days)
        analysis["source"] = "fallback"
    else:
        analysis = validate_analysis(parsed)
        analysis["source"] = "ai"
        analysis["fallback"] = False
        analysis["notice"] = ""
    # Always gate A/B edges and signals on real supplied refs (AI and fallback).
    analysis = ground_analysis(analysis, known_evidence_refs(pack_a, pack_b))
    analysis["a_id"] = int(a_id)
    analysis["b_id"] = int(b_id)
    return analysis


def orient_analysis(analysis: Dict[str, Any], a_id: int, b_id: int) -> Dict[str, Any]:
    """Swap A/B columns so they match the current selection order."""
    stored_a = analysis.get("a_id")
    stored_b = analysis.get("b_id")
    try:
        stored_a = int(stored_a) if stored_a is not None else None
        stored_b = int(stored_b) if stored_b is not None else None
    except (TypeError, ValueError):
        stored_a = stored_b = None
    if stored_a == int(a_id) and stored_b == int(b_id):
        return analysis
    if stored_a != int(b_id) or stored_b != int(a_id):
        analysis["a_id"] = int(a_id)
        analysis["b_id"] = int(b_id)
        return analysis
    swapped = dict(analysis)
    dims = []
    for dim in analysis.get("dimensions") or []:
        item = dict(dim)
        item["a_stronger_points"], item["b_stronger_points"] = (
            list(dim.get("b_stronger_points") or []),
            list(dim.get("a_stronger_points") or []),
        )
        edge = dim.get("edge")
        if edge == "A":
            item["edge"] = "B"
        elif edge == "B":
            item["edge"] = "A"
        dims.append(item)
    swapped["dimensions"] = dims
    swapped["a_id"] = int(a_id)
    swapped["b_id"] = int(b_id)
    return swapped


def evidence_index(*packs: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Map item refs to display metadata for evidence links."""
    index: Dict[str, Dict[str, Any]] = {}
    for pack in packs:
        comp = pack.get("competitor") or {}
        cid = comp.get("id")
        name = comp.get("name") or ""
        for item in pack.get("items") or []:
            ref = item.get("ref")
            if not ref:
                continue
            index[ref] = {
                "ref": ref,
                "text": item.get("text") or ref,
                "kind": item.get("kind") or "",
                "competitor_id": cid,
                "competitor_name": name,
                "insight_id": item.get("insight_id"),
            }
    return index
