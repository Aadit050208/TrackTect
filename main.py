"""CLI entry point: run the full TrackTect pipeline once for one or more URLs.

Usage:
    python main.py https://competitor.com [https://other.com ...]
    python main.py            (prompts for URLs interactively)
"""

import sys

from backend_logic import run_adhoc
from config import setup_logging


def main() -> None:
    setup_logging()

    if len(sys.argv) > 1:
        urls = sys.argv[1:]
    else:
        raw = input("🌐 Enter one or more competitor website URLs (comma-separated):\n").strip()
        urls = [u.strip() for u in raw.split(",") if u.strip()]

    if not urls:
        print("No URLs given; exiting.")
        return

    urls = [u if u.startswith(("http://", "https://")) else "https://" + u for u in urls]
    result = run_adhoc(urls)

    print("\n" + "=" * 60)
    for line in result["logs"]:
        print(line)
    print("=" * 60)

    for entry in result["results"]:
        print(f"\n🔗 {entry['url']}")
        if entry["insights"]:
            for item in entry["insights"]:
                print(f"  - [{item['severity'].upper()}] [{item['category']}] {item['text']}")
        else:
            print("  (no classified insights — is the LLM endpoint configured in .env?)")


if __name__ == "__main__":
    main()
