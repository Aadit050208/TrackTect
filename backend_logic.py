from agents.scraper_agent import ScraperAgent
from agents.summarizer_agent import SummarizerAgent
from agents.classifier_agent import ClassifierAgent
from agents.landing_page_agent import LandingPageWatcherAgent
from agents.twitter_agent_selenium import find_twitter_username_from_website, TwitterSeleniumScraper
from agents.youtube_agent import YouTubeAgent, find_youtube_channel_from_website
from agents.notion_agent import NotionAgent

import json
from pathlib import Path

def run_all_agents_return_data(input_urls: list, twitter_fallback: dict = {}, youtube_fallback: dict = {}):

    logs = []  # to collect logs for UI
    tweets_by_url = {}
    youtube_by_url = {}

    logs.append("🚀 Starting TrackTect AI Agent Workflow...")

    # 1️⃣ Scraper
    logs.append("🔍 Running Scraper Agent...")
    scraper = ScraperAgent()
    scraper.urls = input_urls
    scraped_data = scraper.run()

    # 2️⃣ Summarizer
    logs.append("📝 Running Summarizer Agent...")
    summarizer = SummarizerAgent()
    summaries = {url: summarizer.summarize(text, url) for url, text in scraped_data.items()}

    # 3️⃣ Classifier
    logs.append("📊 Running Classifier Agent...")
    classifier = ClassifierAgent()
    classified_data = {}
    for url, summary in summaries.items():
        logs.append(f"\n🔗 {url}")
        logs.append(f"Summary:\n{summary}")
        categories = classifier.classify(summary, url)
        for item in categories:
            logs.append(f"- [{item['category']}] {item['text']}")
        domain = url.split("//")[-1].split("/")[0].replace(".", "_")
        classified_data[domain] = categories

    # Save
    output_path = Path("output/classified_results.json")
    output_path.parent.mkdir(exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(classified_data, f, indent=2, ensure_ascii=False)
    logs.append(f"✅ Classification results saved to: {output_path.resolve()}")

    # 4️⃣ Landing Page Changes

    # After landing_checker.run(...)
    logs.append("🔎 Running Landing Page Change Detector...")
    landing_checker = LandingPageWatcherAgent()
    landing_page_changes = landing_checker.run(input_urls)

    # 🪛 Add this block to log every detail
    for url, result in landing_page_changes.items():
        logs.append(f"\n🔍 Checking Landing Page: {url}")
        if result["status"] == "changed":
            logs.append("🆕 Messaging Changes Detected:")
            for line in result["diff"]:
                logs.append(line)
        elif result["status"] == "no_change":
            logs.append("✅ No messaging changes since last check.")
        else:
            logs.append(f"❌ Failed to check {url}: {result.get('reason', 'unknown error')}")



    # 5️⃣ Twitter Agent
    logs.append("🐦 Running Twitter Agent...")
    # Twitter Agent
    for url in input_urls:
        twitter_handle = twitter_fallback.get(url) or find_twitter_username_from_website(url)

        tweet_list = []
        if twitter_handle:
            logs.append(f"🔗 From {url} → Found Twitter: @{twitter_handle}")
            twitter_scraper = TwitterSeleniumScraper(twitter_handle, max_tweets=5)
            tweets = twitter_scraper.scrape()
            for i, tweet in enumerate(tweets):
                logs.append(f"📝 Tweet {i + 1}:\n{tweet}\n{'-' * 40}")
                tweet_list.append(tweet)
        else:
            logs.append(f"❌ No Twitter handle found on {url}")
        tweets_by_url[url] = tweet_list

    # 6️⃣ YouTube Agent
    logs.append("📺 Running YouTube Agent...")
    for url in input_urls:
        yt_channel_url = youtube_fallback.get(url) or find_youtube_channel_from_website(url)

        video_list = []
        if yt_channel_url:
            logs.append(f"🔗 From {url} → Found YouTube: {yt_channel_url}")
        else:
            logs.append(f"❌ No YouTube channel found on {url}")
            yt_channel_url = None  # fallback skip in web UI
        if yt_channel_url:
            yt_scraper = YouTubeAgent(yt_channel_url, max_videos=5)
            videos = yt_scraper.scrape()
            for i, video in enumerate(videos):
                logs.append(f"\n🎬 Video {i + 1}: {video['title']}")
                logs.append(f"🔗 {video['url']}")
                logs.append(f"📝 Description: {video['description'][:200]}...")
                logs.append("💬 Comments:")
                for c in video['comments']:
                    logs.append(f" - {c}")
                logs.append("-" * 40)
                video_list.append(video)
        youtube_by_url[url] = video_list

    # 7️⃣ Push to Notion
    logs.append("🧠 Pushing insights to Notion...")
    notion = NotionAgent()
    for domain, items in classified_data.items():
        summary_lines = [f"{domain.replace('_', '.')} — Latest classified updates:"]
        for item in items:
            summary_lines.append(f"- [{item['category']}] {item['text']}")
        formatted = "\n".join(summary_lines)
        notion.append_update(title=domain, content=formatted)
    logs.append("✅ Updates pushed to Notion!")

    logs.append("🎉 All agents executed successfully!")

    # ✅ Final structured return for frontend
    return {
        "log_lines": logs,
        "classified": classified_data,
        "landing_changes": landing_page_changes,
        "tweets": tweets_by_url,
        "youtube": youtube_by_url,
        "notion_status": "Success"
    }
