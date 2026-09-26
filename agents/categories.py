"""Shared PM-facing change categories used across classify / triage / UI."""

from __future__ import annotations

# Ordered for filters and compare columns.
INSIGHT_CATEGORIES = [
    "Feature Update",
    "New Product / Launch",
    "UI/UX Change",
    "Pricing Change",
    "Discount / Offer",
    "Marketing Campaign",
    "Funding / Partnership",
    "Partnership / Collab",
    "User Engagement",
    "Careers",
    "Impact / CSR",
    "Tone Shift",
    "Other",
]

VALID_CATEGORIES = set(INSIGHT_CATEGORIES)

# Map classifier categories → PM briefing sections.
CATEGORY_TO_SECTION = {
    "Feature Update": "Products & Features",
    "New Product / Launch": "Products & Features",
    "UI/UX Change": "UX & Messaging",
    "Pricing Change": "Pricing & Packaging",
    "Discount / Offer": "Pricing & Packaging",
    "Marketing Campaign": "Marketing & Campaigns",
    "Funding / Partnership": "Funding & Partnerships",
    "Partnership / Collab": "Funding & Partnerships",
    "User Engagement": "User Engagement",
    "Careers": "Careers & Hiring",
    "Impact / CSR": "Impact & CSR",
    "Tone Shift": "UX & Messaging",
    "Other": "Other signals",
}

PM_SECTIONS = [
    "Products & Features",
    "Pricing & Packaging",
    "Marketing & Campaigns",
    "UX & Messaging",
    "Funding & Partnerships",
    "User Engagement",
    "Careers & Hiring",
    "Impact & CSR",
    "Other signals",
]

NEWS_SIGNAL_TYPES = [
    "funding",
    "marketing_campaign",
    "partnership",
    "product_launch",
    "engagement",
    "other",
]

SIGNAL_LABELS = {
    "funding": "Funding",
    "marketing_campaign": "Marketing campaign",
    "partnership": "Partnership",
    "product_launch": "Product launch",
    "engagement": "User engagement",
    "other": "News",
}
