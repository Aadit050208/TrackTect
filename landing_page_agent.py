import os
import requests
import difflib
import json
from bs4 import BeautifulSoup

SNAPSHOT_FILE = "data/messaging_snapshots.json"

class LandingPageWatcherAgent:
    def __init__(self):
        if not os.path.exists("data"):
            os.makedirs("data")
        if not os.path.exists(SNAPSHOT_FILE):
            with open(SNAPSHOT_FILE, "w") as f:
                json.dump({}, f)

    def get_visible_text(self, url):
        try:
            response = requests.get(url, timeout=10)
            soup = BeautifulSoup(response.text, "html.parser")

            # Remove style/script/nav/footer
            for tag in soup(["style", "script", "nav", "footer"]):
                tag.extract()

            visible_text = soup.get_text(separator="\n")
            lines = [line.strip() for line in visible_text.splitlines() if line.strip()]
            return lines
        except Exception as e:
            print(f"[ERROR] Failed to fetch {url}: {e}")
            return []

    def load_snapshots(self):
        try:
            with open(SNAPSHOT_FILE, "r") as f:
                content = f.read().strip()
                if not content:
                    return {}
                return json.loads(content)
        except Exception as e:
            print(f"[ERROR] Failed to load snapshot file: {e}")
            return {}

    def save_snapshot(self, url, lines):
        data = self.load_snapshots()
        data[url] = lines
        with open(SNAPSHOT_FILE, "w") as f:
            json.dump(data, f, indent=2)

    def compare(self, old, new):
        diff = difflib.unified_diff(old, new, lineterm="")
        return list(diff)

    def run(self, urls):
        snapshots = self.load_snapshots()
        all_changes = {}

        for url in urls:
            print(f"\n🔍 Checking Landing Page: {url}")
            new_lines = self.get_visible_text(url)
            old_lines = snapshots.get(url, [])

            if not new_lines:
                print("❌ Skipped due to empty content.")
                all_changes[url] = {"status": "failed", "reason": "empty content"}
                continue

            changes = self.compare(old_lines, new_lines)

            if changes:
                print("🆕 Messaging Changes Detected:")
                for line in changes:
                    print(line)
                all_changes[url] = {
                    "status": "changed",
                    "diff": changes
                }
            else:
                print("✅ No messaging changes since last check.")
                all_changes[url] = {
                    "status": "no_change",
                    "diff": []
                }

            self.save_snapshot(url, new_lines)

        return all_changes

