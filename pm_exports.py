"""PM helpers: ticket drafts, battlecards, quarterly summaries — quota-aware."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import db
from agents.llm_client import LLMClient
import usage as usage_mod


def build_ticket_markdown(
    insight: Dict,
    *,
    competitor_name: str = "",
    detailed_extra: str = "",
) -> str:
    """Build a Jira/Linear/Notion-friendly Markdown ticket from existing fields."""
    category = insight.get("category") or "Change"
    text = (insight.get("text") or "").strip()
    severity = insight.get("severity") or "medium"
    reason = (insight.get("triage_reason") or "").strip()
    roadmap = (insight.get("roadmap_match") or "").strip()
    name = competitor_name or insight.get("competitor_name") or "Competitor"

    title = f"[Competitive] {name}: {text[:80]}{'…' if len(text) > 80 else ''}"
    why_bits = []
    if roadmap:
        why_bits.append(f"Overlaps our roadmap item “{roadmap}”.")
    if reason:
        why_bits.append(reason)
    if not why_bits:
        why_bits.append(f"Detected {category.lower()} — worth a quick look against our plans.")
    why = " ".join(why_bits)

    lines = [
        f"# {title}",
        "",
        "## Description",
        f"**Competitor:** {name}",
        f"**Category:** {category}",
        f"**Priority signal:** {severity}",
        "",
        text,
        "",
        "## Why this matters",
        why,
    ]
    if detailed_extra:
        lines.extend(["", "## Extra detail", detailed_extra.strip()])
    lines.extend([
        "",
        "---",
        "_Drafted from TrackTect — paste into Jira, Linear, or Notion._",
    ])
    return "\n".join(lines)


def enrich_ticket_with_llm(insight: Dict, competitor_name: str = "") -> Tuple[Optional[str], Optional[str]]:
    """Optional quota-consuming detailed draft. Returns (markdown, error)."""
    base = build_ticket_markdown(insight, competitor_name=competitor_name)
    llm = LLMClient()
    prompt = f"""Expand this competitive intelligence ticket for a PM.

Keep the same Markdown structure (# title, ## Description, ## Why this matters).
Add 2–3 concrete investigation or response suggestions under a ## Suggested next steps section.
Do not invent facts not supported by the source text.

Source ticket:
{base}
"""
    raw = llm.chat(
        system="You write concise PM tickets from competitor intelligence. Plain Markdown only.",
        user=prompt,
        temperature=0.3,
        max_tokens=500,
    )
    if not raw:
        return None, "Couldn’t generate a detailed draft right now. Try the basic ticket instead."
    return raw.strip(), None


def build_battlecard_markdown(snapshot: Dict, positioning: str = "") -> str:
    comp = snapshot.get("competitor") or {}
    name = comp.get("name") or "Competitor"
    url = comp.get("url") or ""
    days = snapshot.get("days", 60)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        f"# Battlecard — {name}",
        f"_Generated {today} · last {days} days_",
        "",
        f"**Website:** {url}",
        "",
        "## Positioning",
        (positioning or "_No positioning summary yet — generate one to fill this in._").strip(),
        "",
        "## Significant changes by category",
    ]
    by_cat = snapshot.get("by_category") or {}
    if not by_cat:
        lines.append("_No classified changes in this window yet._")
    else:
        for cat, items in by_cat.items():
            lines.append(f"### {cat}")
            for it in items[:4]:
                sev = it.get("severity", "low")
                lines.append(f"- [{sev}] {it.get('text', '')}")
            lines.append("")
    lines.append("## Pricing snapshot")
    pricing = snapshot.get("pricing") or []
    if not pricing:
        lines.append("_No pricing changes detected yet._")
    else:
        for p in pricing[:5]:
            lines.append(f"- {p.get('created_at', '')[:10]} — {p.get('text', '')}")
    lines.extend(["", "---", "_TrackTect battlecard_"])
    return "\n".join(lines)


def synthesize_battlecard_positioning(snapshot: Dict) -> Tuple[Optional[str], Optional[str]]:
    comp = snapshot.get("competitor") or {}
    name = comp.get("name") or "Competitor"
    bullets: List[str] = []
    for cat, items in (snapshot.get("by_category") or {}).items():
        for it in items[:3]:
            bullets.append(f"- [{cat}/{it.get('severity')}] {it.get('text')}")
    for p in (snapshot.get("pricing") or [])[:3]:
        bullets.append(f"- [Pricing] {p.get('text')}")
    if not bullets:
        return (
            f"{name} hasn’t shown clear product moves in the tracked window yet. "
            "Keep watching; treat this as an incomplete read, not a quiet competitor.",
            None,
        )
    prompt = f"""Write a 2–3 sentence competitive positioning summary for a PM battlecard about {name}.
Use only the evidence below. Be concrete and plain-language. No bullet list — just short paragraphs.

Evidence:
{chr(10).join(bullets[:20])}
"""
    llm = LLMClient()
    raw = llm.chat(
        system="You write crisp competitive positioning for product managers.",
        user=prompt,
        temperature=0.3,
        max_tokens=220,
    )
    if not raw:
        return None, "Couldn’t write the positioning summary. Try again in a moment."
    return raw.strip(), None


def synthesize_quarterly_summary(insights: List[Dict], range_start: str, range_end: str) -> Tuple[Optional[str], Optional[str]]:
    if not insights:
        return (
            "Nothing meaningful showed up across your tracked competitors in this range. "
            "That’s a quiet quarter in the data — not proof that nothing happened outside tracked sources.",
            None,
        )
    by_cat: Dict[str, int] = {}
    samples: List[str] = []
    for it in insights[:60]:
        cat = it.get("category") or "Other"
        by_cat[cat] = by_cat.get(cat, 0) + 1
        samples.append(
            f"- [{cat}] {it.get('competitor_name', '?')}: {it.get('text', '')[:160]}"
        )
    counts = ", ".join(f"{k} ({v})" for k, v in sorted(by_cat.items(), key=lambda x: -x[1]))
    prompt = f"""Write one short paragraph (4–6 sentences) summarizing the competitive landscape for a PM quarterly review.
Date range: {range_start[:10]} to {range_end[:10]}.
Category counts: {counts}.

Sample changes:
{chr(10).join(samples[:25])}

Be plain-language. Call out themes, not every item. Don’t invent facts.
"""
    llm = LLMClient()
    raw = llm.chat(
        system="You write quarterly competitive landscape summaries for product managers.",
        user=prompt,
        temperature=0.3,
        max_tokens=350,
    )
    if not raw:
        return None, "Couldn’t generate the quarterly summary right now."
    return raw.strip(), None


def consume_or_error(user_id: int) -> Optional[str]:
    ok, err = usage_mod.try_consume(user_id)
    return None if ok else (err or "No searches left.")
