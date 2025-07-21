import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager
import time
import re


class TwitterSeleniumScraper:
    def __init__(self, username, max_tweets=5):
        self.username = username
        self.max_tweets = max_tweets

    def scrape(self):
        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--disable-blink-features=AutomationControlled")
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

        try:
            url = f"https://twitter.com/{self.username}"
            driver.get(url)
            time.sleep(5)

            tweets = []
            tweet_elements = driver.find_elements(By.XPATH, '//article[@role="article"]')

            print(f"🔍 Found {len(tweet_elements)} tweet elements on @{self.username}'s timeline")

            for i, element in enumerate(tweet_elements[:self.max_tweets]):
                try:
                    content = element.text
                    tweets.append(content)
                except Exception as e:
                    print(f"⚠️ Error parsing tweet {i}: {e}")

            print(f"✅ Scraped {len(tweets)} tweets from @{self.username}")
            return tweets

        finally:
            driver.quit()


def find_twitter_username_from_website(url):
    try:
        response = requests.get(url, timeout=10)
        soup = BeautifulSoup(response.content, 'html.parser')

        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            if "twitter.com" in href and not any(x in href for x in ["intent", "share", "search"]):
                match = re.search(r"twitter\.com/([A-Za-z0-9_]{1,15})", href)
                if match:
                    return match.group(1)  # Return the first Twitter handle found

        return None
    except Exception as e:
        print(f"❌ Error while fetching or parsing website: {e}")
        return None


if __name__ == "__main__":
    website_url = input("🌐 Enter any website URL (e.g. https://www.notion.so): ").strip()

    twitter_username = find_twitter_username_from_website(website_url)

    if twitter_username:
        print(f"🔗 Found Twitter handle: @{twitter_username}")
        scraper = TwitterSeleniumScraper(twitter_username, max_tweets=5)
        tweets = scraper.scrape()

        for i, tweet in enumerate(tweets):
            print(f"\n📝 Tweet {i + 1}:\n{tweet}\n{'-' * 40}")
    else:
            print("❌ No Twitter handle found on the given URL.")
            fallback = input("🔁 Want to manually enter the Twitter handle? (yes/no): ").strip().lower()
            if fallback == "yes":
                manual_handle = input("✏️ Enter Twitter handle (without @): ").strip()
                scraper = TwitterSeleniumScraper(manual_handle, max_tweets=5)
                tweets = scraper.scrape()

                for i, tweet in enumerate(tweets):
                    print(f"\n📝 Tweet {i + 1}:\n{tweet}\n{'-' * 40}")
