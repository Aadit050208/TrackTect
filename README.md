# TrackTect — AI Competitor Intelligence Tracker

TrackTect monitors competitor websites, changelogs, Twitter/X and YouTube, uses
an LLM to summarise + classify what changed, triages each change by business
significance, and alerts you only when something high-priority happens.

## How it works

For each tracked competitor, an **orchestrator agent** first plans which
sub-agents are worth running (it auto-discovers changelog pages and social
handles, and skips agents whose inputs don't exist). Then the pipeline runs:

| Agent | Responsibility |
|---|---|
| Scraper | Fetch + clean visible page text (retries with a fallback strategy) |
| Summarizer | LLM: condense content into 3–5 bullets |
| Classifier | LLM: label each bullet (Feature Update, UI/UX, Pricing, Tone, Other) |
| Triage | Score each change high / medium / low significance |
| Landing Page Watcher | Snapshot + line-level diff of page messaging between runs |
| Twitter | Recent tweets via headless Chrome (optional) |
| YouTube | Recent videos + comments via headless Chrome (optional) |
| Notion | Push a digest to a Notion page (optional) |

Every run is stored in a local SQLite database (`data/tracktect.db`):
competitors, run history, classified insights, landing-page diffs, social
items, and alerts — so per-competitor timelines and weekly trends work.
Scheduled re-runs happen in-process via APScheduler at a per-competitor
interval you set in the UI. High-priority changes fan out through a pluggable
notifier interface (log always; generic JSON webhook if configured).

Any failing agent (unreachable site, missing Chrome, LLM down, no Notion
token) degrades to a logged warning and a status badge in the UI — it never
takes down the run.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
copy .env.example .env           # then fill in your values
```

`.env` keys (see `.env.example` for details):

- `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` — any OpenAI-compatible
  `/v1/chat/completions` provider (LM Studio, OpenAI, etc.). Without it the
  app still runs; summarize/classify steps are skipped with a warning.
- `NOTION_TOKEN`, `NOTION_PAGE_ID` — optional Notion digest push.
- `NOTIFY_WEBHOOK_URL` — optional webhook that receives high-priority alerts.
- `SECRET_KEY` — Flask session key.

Twitter/YouTube agents need Chrome installed (Selenium + webdriver-manager
handle the driver). Without Chrome those agents are skipped gracefully.

## Run

```bash
python app.py        # web UI at http://127.0.0.1:5000
python main.py URL   # one-off CLI run
```

Register an account, add competitors from the dashboard, set each one's check
frequency and agent toggles, and export a weekly Markdown digest from the nav.

## Project layout

```
app.py            Flask app: auth, dashboard, timeline, settings, export
backend_logic.py  Pipeline orchestration + persistence/alerting wrappers
scheduler.py      APScheduler interval jobs per competitor
db.py             SQLite storage layer (stdlib sqlite3)
notifiers.py      Pluggable alert channels (log, webhook)
config.py         .env loading + logging setup
agents/           Independent single-responsibility agents
templates/        Jinja templates (dark terminal aesthetic)
static/           CSS
data/             SQLite DB + landing-page snapshots (gitignored)
```
