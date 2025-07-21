🚀 TrackTect – AI-Powered Competitor Intelligence Toolkit

🏆 Built at the AI AGENT HACKATHON BY PRODUCT SPACE 2025💡 A smart way to monitor competitor updates across web, Twitter, YouTube, and more🧠 Powered by modular AI agents, clean UI, and real-time classification

🧠 The Problem We Solved

In today’s fast-paced market, businesses can’t keep up with how fast their competitors change — websites silently update, new products are launched, and social media strategy shifts overnight.

We asked:

❓ What if AI could act like a 24/7 analyst — tracking every signal, summarizing it, and showing you what changed?

🎯 Our Solution: TrackTect

TrackTect is an AI-powered agent framework that continuously monitors your competitors across:

🌐 Websites — Extract and summarize content

🤦‍♂️ Twitter — Scrape recent tweets (or let user input it manually)

📺 YouTube — Find and summarize recent videos

📄 Landing Pages — Detect subtle line-by-line messaging changes

🧠 Notion (optional) — Push findings into a team workspace

Everything is shown on a user-friendly web interface, with terminal-style logs for devs and clear outputs for decision makers.

🧹 Modular Agent Architecture

Agent

Responsibility

🔍 ScraperAgent

Extracts website content

📝 SummarizerAgent

Converts raw HTML to concise bullet-point summaries

📊 ClassifierAgent

Labels insights (Feature, UI/UX, Pricing, Tone, etc.)

📄 LandingPageWatcherAgent

Detects changes on landing page messaging

🤦 TwitterAgentSelenium

Scrapes recent tweets (or allows manual input)

📺 YouTubeAgent

Scrapes latest videos, titles, and top comments

🧠 NotionAgent

(Optional) Pushes updates to Notion

⚙️ Major Challenges — and Our Fixes

✅ Problem: Websites don’t offer changelogs💡 Fix: Built a summarizer-classifier combo to interpret raw changes

✅ Problem: Twitter & YouTube not always detectable💡 Fix: Added manual input fallback from frontend UI

✅ Problem: Terminal output was messy for non-tech users💡 Fix: Captured logs to browser UI in a beautiful format (with hover effects & dark mode)

✅ Problem: Diffing web content is hard💡 Fix: Created a snapshot-based difference engine that shows exact messaging changes

💡 Key Features

🧰 Modular plug-and-play agents

🔹 Flask-based web UI with live logging

🌙 Dark mode toggle

✍️ Manual input support for Twitter/YouTube if scraping fails

📝 AI-based summarization + classification

🛄 Optional Notion push

📦 Organized output structure with JSON result logs

🧪 How to Run (Locally)

⚒️ Prerequisite: Python 3.8+, Chrome installed💬 If using LLM-powered summarizer or classifier (e.g., LM Studio), ensure it's running locally

⚙️ Clone + Setup

git clone https://github.com/Aadit050208/TrackTect.git

cd TrackTect
pip install -r requirements.txt
python app.py

⚡ Start the Web App

python app.py

Go to: http://127.0.0.1:5000

🧠 Using LM Studio for Local LLMs (if needed)

If you're using LLM-based summarization/classification, install LM Studio and run a local model (like mistral or llama3).

Make sure your local server is:

Running on http://localhost:1234

Compatible with the prompt template used in the code

No API keys needed — pure local AI ⚡

📂 Project Structure

TrackTect/
├── agents/                      # All your modular AI agents
│   ├── scraper_agent.py
│   ├── summarizer_agent.py
│   ├── classifier_agent.py
│   ├── landing_page_agent.py
│   ├── notion_agent.py
│   ├── twitter_agent_selenium.py
│   └── youtube_agent.py
│
├── templates/                  # Flask HTML templates
│   └── index.html
│   └── result.html
│
├── data/                       # Store snapshots or cache (optional, .gitignore if large)
│   └── messaging_snapshots.json
│
├── output/                     # JSON logs or AI outputs
│   └── classified_results.json
│
├── app.py                      # Flask server file 
├── backend_logic.py            # logic for ui based output
├── main.py                     # cli output
├── backend_logic.py            # Logic separated from UI (calls agents)
├── requirements.txt            # Python dependencies




