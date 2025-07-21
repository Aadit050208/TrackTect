# Create a starter version of youtube_agent.py based on the requirements
from pathlib import Path


import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse
import re
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
import time


class YouTubeAgent:
    def __init__(self, channel_url, max_videos=5):
        self.channel_url = channel_url
        self.max_videos = max_videos

    def scrape(self):
        options = Options()
        options.add_argument("--headless=new")  # Can be disabled for debugging
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")

        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

        try:
            driver.get(self.channel_url + "/videos")
            time.sleep(6)

            videos = []
            video_elements = driver.find_elements(By.ID, "video-title")

            print(f"🔍 Found {len(video_elements)} video titles on channel")

            for i in range(min(self.max_videos, len(video_elements))):
                try:
                    video_elements = driver.find_elements(By.ID, "video-title")  # Refresh elements after each nav
                    video = video_elements[i]
                    title = video.get_attribute("title")

                    print(f"\n➡️ Clicking video {i + 1}: {title}")
                    driver.execute_script("arguments[0].click();", video)
                    time.sleep(5)

                    video_url = driver.current_url

                    # Get description
                    try:
                        desc_elem = driver.find_element(By.XPATH, '//div[@id="description"]//yt-formatted-string')
                        description = desc_elem.text
                    except:
                        description = "No description found."

                    # Get comments
                    comments = []
                    try:
                        comment_elements = driver.find_elements(By.XPATH,
                                                                '//ytd-comment-thread-renderer//yt-formatted-string[@id="content-text"]')
                        comments = [c.text for c in comment_elements[:5]]
                    except:
                        comments = []

                    videos.append({
                        "title": title,
                        "url": video_url,
                        "description": description,
                        "comments": comments
                    })

                    driver.back()
                    time.sleep(4)
                except Exception as e:
                    print(f"⚠️ Error processing video {i}: {e}")
                    driver.back()
                    time.sleep(3)

            print(f"\n✅ Scraped {len(videos)} videos from channel")
            return videos

        finally:
            driver.quit()



def find_youtube_channel_from_website(url):
    try:
        response = requests.get(url, timeout=10)
        soup = BeautifulSoup(response.content, 'html.parser')
        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            if "youtube.com/channel/" in href or "youtube.com/c/" in href or "youtube.com/@":
                match = re.search(r"(https?://)?(www\\.)?youtube\\.com[^\\\"\\s]+", href)
                if match:
                    return match.group(0)
        return None
    except Exception as e:
        print(f"❌ Error while fetching or parsing website: {e}")
        return None


if __name__ == "__main__":
    website_url = input("🌐 Enter a website URL to search for their YouTube channel: ").strip()
    yt_channel_url = find_youtube_channel_from_website(website_url)

    if yt_channel_url:
        print(f"📺 Found YouTube channel: {yt_channel_url}")
    else:
        print("❌ No YouTube channel found on the website.")
        fallback = input("🔁 Want to manually enter the YouTube channel URL? (yes/no): ").strip().lower()
        if fallback == "yes":
            yt_channel_url = input("✏️ Enter the YouTube channel URL: ").strip()

    if yt_channel_url:
        agent = YouTubeAgent(yt_channel_url, max_videos=5)
        videos = agent.scrape()

        for i, video in enumerate(videos):
            print(f"\\n🎬 Video {i+1}: {video['title']}")
            print(f"🔗 {video['url']}")
            print(f"📝 Description: {video['description'][:200]}...")
            print("💬 Comments:")
            for c in video['comments']:
                print(f" - {c}")
            print("-" * 40)


# Save to youtube_agent.py
agent_path = Path("agents/youtube_agent.py")
agent_path.parent.mkdir(exist_ok=True)



"✅ `youtube_agent.py` created successfully with YouTube scraping logic using Selenium and fallback input."


