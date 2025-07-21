import requests
import json


class SummarizerAgent:
    def __init__(self, endpoint="http://localhost:1234/v1/chat/completions"):
        self.endpoint = endpoint

    def summarize(self, raw_text, url=None):
        prompt = f"""
You're an AI assistant for Product Managers.

Summarize the following changelog or release notes into 3–5 bullet points:
- Mention new features, UI/UX changes, pricing, and tone changes.

Source: {url or "unknown"}

\"\"\"
{raw_text[:3000]}
\"\"\"
        """

        payload = {
            "model": "local-model",  # LM Studio ignores this field
            "messages": [
                {"role": "system", "content": "You are an expert PM summarizer."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.7,
            "max_tokens": 500,
            "stream": False
        }

        try:
            response = requests.post(self.endpoint, headers={"Content-Type": "application/json"}, data=json.dumps(payload))
            result = response.json()
            return result["choices"][0]["message"]["content"].strip()
        except Exception as e:
            print(f"[ERROR] Local summarization failed: {e}")
            return "Summary not available."


if __name__ == "__main__":
    from scraper_agent import ScraperAgent
    scraper = ScraperAgent()
    data = scraper.run()

    summarizer = SummarizerAgent()
    for url, text in data.items():
        print(f"\n[SUMMARY for {url}]:\n")
        summary = summarizer.summarize(text, url)
        print(summary)
