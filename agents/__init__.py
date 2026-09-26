"""TrackTect agent package.

Each agent is an independent, single-responsibility module:

- ScraperAgent          : fetch + clean visible text from competitor URLs
- SummarizerAgent       : LLM summary of scraped content (3-5 bullets)
- ClassifierAgent       : LLM categorisation of summary bullets
- LandingPageWatcherAgent: snapshot + diff of landing-page messaging
- TwitterSeleniumScraper: recent tweets via Selenium (optional)
- YouTubeAgent          : recent videos/comments via Selenium (optional)
- NotionAgent           : push digests to a Notion page (optional)
- OrchestratorAgent     : plan which agents are worth running for an input
- TriageAgent           : score classified changes by severity
"""
