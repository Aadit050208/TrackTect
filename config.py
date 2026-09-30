"""Central configuration for TrackTect.

Loads environment variables from `.env` (never hardcode secrets) and sets up
application-wide logging. Import `settings` anywhere config is needed.
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"
DATA_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


class Settings:
    """Read-once snapshot of environment configuration."""

    def __init__(self) -> None:
        # LLM (OpenAI-compatible chat completions API)
        self.llm_base_url: str = _env("LLM_BASE_URL", "http://localhost:1234/v1").rstrip("/")
        self.llm_api_key: str = _env("LLM_API_KEY")
        self.llm_model: str = _env("LLM_MODEL", "local-model")

        # Notion (optional)
        self.notion_token: str = _env("NOTION_TOKEN")
        self.notion_page_id: str = _env("NOTION_PAGE_ID")

        # Notifications (optional)
        self.notify_webhook_url: str = _env("NOTIFY_WEBHOOK_URL")

        # Optional SMTP email notifier (used for digests / alerts when set)
        self.smtp_host: str = _env("SMTP_HOST")
        self.smtp_port: int = int(_env("SMTP_PORT", "587") or "587")
        self.smtp_user: str = _env("SMTP_USER")
        self.smtp_password: str = _env("SMTP_PASSWORD")
        self.smtp_from: str = _env("SMTP_FROM")
        self.smtp_to: str = _env("SMTP_TO")

        # LLM rate limiting (Groq free tier is strict — space calls out)
        try:
            self.llm_min_interval_seconds: float = max(
                0.5, float(_env("LLM_MIN_INTERVAL_SECONDS", "3") or "3")
            )
        except ValueError:
            self.llm_min_interval_seconds = 3.0
        try:
            self.llm_max_retries: int = max(1, int(_env("LLM_MAX_RETRIES", "4") or "4"))
        except ValueError:
            self.llm_max_retries = 4
        # Prefer rule-based triage to save API quota (summarize+classify still use LLM)
        self.llm_triage_enabled: bool = _env("LLM_TRIAGE_ENABLED", "0") == "1"

        # Per-user search quota (one unit per LLM pipeline run / discovery)
        try:
            self.default_user_quota: int = max(0, int(_env("DEFAULT_USER_QUOTA", "2") or "2"))
        except ValueError:
            self.default_user_quota = 2
        # Comma-separated usernames that get is_admin=1 on registration (or promote later)
        self.admin_usernames: set[str] = {
            u.strip().lower()
            for u in _env("ADMIN_USERNAMES", "").split(",")
            if u.strip()
        }

        # Search / discovery provider (Section 2) — swappable backend
        self.search_provider: str = _env("SEARCH_PROVIDER", "duckduckgo").lower()
        self.search_api_key: str = _env("SEARCH_API_KEY")
        self.search_base_url: str = _env("SEARCH_BASE_URL").rstrip("/")

        # Self-healing: escalate fetch fallbacks after this many consecutive failures
        try:
            self.fetch_failure_threshold: int = max(1, int(_env("FETCH_FAILURE_THRESHOLD", "3")))
        except ValueError:
            self.fetch_failure_threshold: int = 3

        # Longer pipeline runs (broader discovery) need a higher worker timeout in prod.
        # Example: gunicorn -b 0.0.0.0:$PORT -t $GUNICORN_TIMEOUT app:app
        try:
            self.gunicorn_timeout: int = max(60, int(_env("GUNICORN_TIMEOUT", "180") or "180"))
        except ValueError:
            self.gunicorn_timeout = 180

        # Flask
        self.secret_key: str = _env("SECRET_KEY", "dev-insecure-secret-change-me")
        self.debug: bool = _env("FLASK_DEBUG", "0") == "1"
        # Set SESSION_COOKIE_SECURE=1 when serving over HTTPS in production.
        self.session_cookie_secure: bool = _env("SESSION_COOKIE_SECURE", "0") == "1"

        # Storage paths
        self.db_path: Path = DATA_DIR / "tracktect.db"
        self.snapshot_file: Path = DATA_DIR / "messaging_snapshots.json"

        # Hosted SQLite (Turso / libSQL). Both must be set to use the remote DB.
        # Local `python app.py` without these still uses data/tracktect.db.
        self.turso_database_url: str = _env("TURSO_DATABASE_URL")
        self.turso_auth_token: str = _env("TURSO_AUTH_TOKEN")
        # Render sets RENDER=true. Never use the wipeable local file there.
        self.on_render: bool = bool(_env("RENDER") or _env("RENDER_SERVICE_ID"))
        self.require_remote_db: bool = (
            _env("REQUIRE_TURSO", "0") == "1" or self.on_render
        )

    @property
    def turso_configured(self) -> bool:
        return bool(self.turso_database_url and self.turso_auth_token)

    @property
    def use_turso(self) -> bool:
        return self.turso_configured

    @property
    def notion_configured(self) -> bool:
        return bool(self.notion_token and self.notion_page_id)

    @property
    def email_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_from and self.smtp_to)


settings = Settings()


def setup_logging() -> None:
    """Configure root logging once for the whole app."""
    # Windows consoles often default to cp1252, which cannot render the
    # emoji used in agent logs; force UTF-8 with replacement instead of
    # letting print()/logging crash.
    import sys
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet down noisy third-party loggers.
    for noisy in ("urllib3", "werkzeug", "apscheduler", "WDM"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


setup_logging()
