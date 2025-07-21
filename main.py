from agents.scraper_agent import ScraperAgent
from agents.summarizer_agent import SummarizerAgent
from agents.classifier_agent import ClassifierAgent
from agents.landing_page_agent import LandingPageWatcherAgent
from agents.twitter_agent_selenium import find_twitter_username_from_website, TwitterSeleniumScraper
from agents.youtube_agent import YouTubeAgent, find_youtube_channel_from_website
from agents.notion_agent import NotionAgent
from textwrap import dedent

import json
from pathlib import Path


def run_all_agents():
    print("\n🚀 Starting TrackTect AI Agent Workflow...\n")

    # 🔹 Ask user to input one or more URLs (comma-separated)
    user_input = input("🌐 Enter one or more competitor website URLs (comma-separated):\n").strip()
    input_urls = [url.strip() for url in user_input.split(",") if url.strip()]

    # 1️⃣ Scrape Website Content
    print("\n🔍 Running Scraper Agent...")
    scraper = ScraperAgent()
    scraper.urls = input_urls
    scraped_data = scraper.run()

    # 2️⃣ Summarize
    print("\n📝 Running Summarizer Agent...")
    summarizer = SummarizerAgent()
    summaries = {url: summarizer.summarize(text, url) for url, text in scraped_data.items()}

    # 3️⃣ Classify
    print("\n📊 Running Classifier Agent...")
    classifier = ClassifierAgent()
    classified_data = {}
    for url, summary in summaries.items():
        print(f"\n🔗 {url}")
        print(f"Summary:\n{summary}")
        categories = classifier.classify(summary, url)
        for item in categories:
            print(f"- [{item['category']}] {item['text']}")
        domain = url.split("//")[-1].split("/")[0].replace(".", "_")
        classified_data[domain] = categories

    # 💾 Save classification output
    output_path = Path("output/classified_results.json")
    output_path.parent.mkdir(exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(classified_data, f, indent=2, ensure_ascii=False)
    print(f"\n✅ Classification results saved to: {output_path.resolve()}")

    # 4️⃣ Check Landing Page Messaging Changes
    print("\n🔎 Running Landing Page Change Detector...")
    landing_checker = LandingPageWatcherAgent()
    landing_checker.run(input_urls)

    # 5️⃣ Twitter Handle Detection + Tweet Scraping
    print("\n🐦 Running Twitter Agent...")
    for url in input_urls:
        twitter_handle = find_twitter_username_from_website(url)
        if twitter_handle:
            print(f"\n🔗 From {url} → Found Twitter: @{twitter_handle}")
            twitter_scraper = TwitterSeleniumScraper(twitter_handle, max_tweets=5)
            tweets = twitter_scraper.scrape()
            for i, tweet in enumerate(tweets):
                print(f"\n📝 Tweet {i + 1}:\n{tweet}\n{'-' * 40}")
        else:
            print(f"❌ No Twitter handle found on {url}")
            fallback = input("🔁 Do you want to manually enter a Twitter handle? (yes/no): ").strip().lower()
            if fallback == "yes":
                manual_handle = input("✏️ Enter handle (without @): ").strip()
                twitter_scraper = TwitterSeleniumScraper(manual_handle, max_tweets=5)
                tweets = twitter_scraper.scrape()
                for i, tweet in enumerate(tweets):
                    print(f"\n📝 Tweet {i + 1}:\n{tweet}\n{'-' * 40}")

    # 6️⃣ YouTube Channel Detection + Video Scraping
    print("\n📺 Running YouTube Agent...")

    for url in input_urls:
        yt_channel_url = find_youtube_channel_from_website(url)

        if yt_channel_url:
            print(f"\n🔗 From {url} → Found YouTube: {yt_channel_url}")
        else:
            print(f"❌ No YouTube channel found on {url}")
            fallback = input("🔁 Do you want to manually enter the YouTube channel URL? (yes/no): ").strip().lower()
            if fallback == "yes":
                yt_channel_url = input("✏️ Enter the YouTube channel URL: ").strip()

        if yt_channel_url:
            yt_scraper = YouTubeAgent(yt_channel_url, max_videos=5)
            videos = yt_scraper.scrape()

            for i, video in enumerate(videos):
                print(f"\n🎬 Video {i + 1}: {video['title']}")
                print(f"🔗 {video['url']}")
                print(f"📝 Description: {video['description'][:200]}...")
                print("💬 Comments:")
                for c in video['comments']:
                    print(f" - {c}")
                print("-" * 40)


    print("\n🧠 Pushing insights to Notion...")
    notion = NotionAgent()
    for domain, items in classified_data.items():

        summary_lines = [f"{domain.replace('_', '.')} — Latest classified updates:"]
        for item in items:
            summary_lines.append(f"- [{item['category']}] {item['text']}")
        formatted_summary = "\n".join(summary_lines)
        notion.append_update(title=domain, content=formatted_summary)


    print("✅ Updates pushed to Notion!")

    print("\n🎉 All agents executed successfully!\n")




if __name__ == "__main__":
    run_all_agents()
