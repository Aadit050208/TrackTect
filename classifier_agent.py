import requests
import json

class ClassifierAgent:
    def __init__(self, endpoint="http://localhost:1234/v1/chat/completions"):
        self.endpoint = endpoint

    def classify(self, summary, url=None):
        prompt = f"""
You are a strict JSON generator AI.

Take the following product release note summary and return a JSON array.

Each array element must contain:
- "category" (one of: Feature Update, UI/UX Change, Pricing Change, Tone Shift, Other)
- "text" (copy the original bullet)

Respond with ONLY valid JSON. Do not explain anything.

Input:
\"\"\"{summary}\"\"\"
        """

        payload = {
            "model": "local-model",
            "messages": [
                {"role": "system", "content": "You are an expert product update classifier."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.3,
            "max_tokens": 600,
            "stream": False
        }

        try:
            response = requests.post(self.endpoint, headers={"Content-Type": "application/json"}, data=json.dumps(payload))
            result = response.json()
            return json.loads(result["choices"][0]["message"]["content"].strip())
        except Exception as e:
            print(f"[ERROR] Classification failed: {e}")
            return []


if __name__ == "__main__":
    from summarizer_agent import SummarizerAgent
    from scraper_agent import ScraperAgent
    from pathlib import Path

    scraper = ScraperAgent()
    data = scraper.run()

    summarizer = SummarizerAgent()
    classifier = ClassifierAgent()

    final_results = {}

    for url, raw_text in data.items():
        print(f"\n🔗 URL: {url}")
        summary = summarizer.summarize(raw_text, url)
        print(f"\n📝 Summary:\n{summary}")

        categories = classifier.classify(summary, url)
        print(f"\n📊 Classified Bullets:")
        for item in categories:
            print(f"- [{item['category']}] {item['text']}")

        # Store results under the domain name
        domain = url.split("//")[-1].split("/")[0].replace(".", "_")
        final_results[domain] = categories

    # 💾 Save to JSON
    output_path = Path("output/classified_results.json")
    output_path.parent.mkdir(exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(final_results, f, indent=2, ensure_ascii=False)

    print(f"\n✅ Results saved to {output_path.absolute()}")
