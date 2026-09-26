"""Known brand → official homepage.

Used when live search / page fetches are blocked (e.g. Amazon HTTP 202 bot
walls). Keys are lowercase; values are canonical homepages only (not product
or regional deep links unless the brand is regional).
"""

from __future__ import annotations

from typing import Optional

# Keep this list to major, unambiguous brands. Discovery still verifies /
# soft-accepts domains for everyone else.
KNOWN_BRAND_SITES = {
    "amazon": "https://www.amazon.com",
    "amazon.com": "https://www.amazon.com",
    "amazon india": "https://www.amazon.in",
    "amazon.in": "https://www.amazon.in",
    "aws": "https://aws.amazon.com",
    "flipkart": "https://www.flipkart.com",
    "myntra": "https://www.myntra.com",
    "ajio": "https://www.ajio.com",
    "nykaa": "https://www.nykaa.com",
    "walmart": "https://www.walmart.com",
    "target": "https://www.target.com",
    "ebay": "https://www.ebay.com",
    "alibaba": "https://www.alibaba.com",
    "nike": "https://www.nike.com",
    "adidas": "https://www.adidas.com",
    "puma": "https://www.puma.com",
    "notion": "https://www.notion.com",
    "linear": "https://linear.app",
    "figma": "https://www.figma.com",
    "canva": "https://www.canva.com",
    "slack": "https://slack.com",
    "zoom": "https://www.zoom.com",
    "shopify": "https://www.shopify.com",
    "stripe": "https://stripe.com",
    "salesforce": "https://www.salesforce.com",
    "hubspot": "https://www.hubspot.com",
    "atlassian": "https://www.atlassian.com",
    "jira": "https://www.atlassian.com/software/jira",
    "asana": "https://asana.com",
    "trello": "https://trello.com",
    "google": "https://www.google.com",
    "microsoft": "https://www.microsoft.com",
    "apple": "https://www.apple.com",
    "meta": "https://www.meta.com",
    "facebook": "https://www.facebook.com",
    "instagram": "https://www.instagram.com",
    "netflix": "https://www.netflix.com",
    "spotify": "https://www.spotify.com",
    "uber": "https://www.uber.com",
    "lyft": "https://www.lyft.com",
    "airbnb": "https://www.airbnb.com",
    "zomato": "https://www.zomato.com",
    "swiggy": "https://www.swiggy.com",
    "paytm": "https://paytm.com",
    "phonepe": "https://www.phonepe.com",
    "razorpay": "https://razorpay.com",
    "meesho": "https://www.meesho.com",
    "cred": "https://cred.club",
    "blinkit": "https://blinkit.com",
    "zepto": "https://www.zeptonow.com",
    "bigbasket": "https://www.bigbasket.com",
    "snapdeal": "https://www.snapdeal.com",
    "jiomart": "https://www.jiomart.com",
    "groww": "https://groww.in",
    "zerodha": "https://zerodha.com",
    "byjus": "https://byjus.com",
    "unacademy": "https://unacademy.com",
    "dunzo": "https://www.dunzo.com",
    "ola": "https://www.olacabs.com",
    "ola cabs": "https://www.olacabs.com",
    "tesla": "https://www.tesla.com",
    "samsung": "https://www.samsung.com",
    "sony": "https://www.sony.com",
    "intel": "https://www.intel.com",
    "nvidia": "https://www.nvidia.com",
    "openai": "https://openai.com",
    "anthropic": "https://www.anthropic.com",
    "cursor": "https://cursor.com",
    "github": "https://github.com",
    "gitlab": "https://gitlab.com",
    "dropbox": "https://www.dropbox.com",
    "box": "https://www.box.com",
    "adobe": "https://www.adobe.com",
    "oracle": "https://www.oracle.com",
    "ibm": "https://www.ibm.com",
    "cisco": "https://www.cisco.com",
    "dell": "https://www.dell.com",
    "hp": "https://www.hp.com",
    "lenovo": "https://www.lenovo.com",
    "coca cola": "https://www.coca-cola.com",
    "coca-cola": "https://www.coca-cola.com",
    "pepsi": "https://www.pepsi.com",
    "starbucks": "https://www.starbucks.com",
    "mcdonalds": "https://www.mcdonalds.com",
    "mcdonald's": "https://www.mcdonalds.com",
}


def known_site_for(name: str) -> Optional[str]:
    key = " ".join((name or "").strip().lower().split())
    if key in KNOWN_BRAND_SITES:
        return KNOWN_BRAND_SITES[key]
    # strip Inc/Ltd/Corp
    for suffix in (" inc", " ltd", " llc", " corp", " co", " company"):
        if key.endswith(suffix):
            trimmed = key[: -len(suffix)].strip()
            if trimmed in KNOWN_BRAND_SITES:
                return KNOWN_BRAND_SITES[trimmed]
    return None
