"""Notion Agent: pushes formatted competitor digests into a Notion page.

Input : title + content strings per update.
Output: True on success, False on failure. If NOTION_TOKEN / NOTION_PAGE_ID
        are not configured, the agent reports itself unavailable and every
        call becomes a logged no-op (never an exception).
"""

import logging
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)

try:
    from notion_client import Client
    from notion_client.errors import APIResponseError
except ImportError:
    Client = None
    APIResponseError = Exception


class NotionAgent:
    """Append classified-update digests to the configured Notion page."""

    def __init__(self) -> None:
        self.client: Optional["Client"] = None
        self.last_error: Optional[str] = None

        if Client is None:
            self.last_error = "notion-client package is not installed"
        elif not settings.notion_configured:
            self.last_error = "NOTION_TOKEN / NOTION_PAGE_ID not set in .env"
        else:
            self.client = Client(auth=settings.notion_token)

        if self.last_error:
            logger.info("Notion agent unavailable: %s", self.last_error)

    @property
    def available(self) -> bool:
        return self.client is not None

    def append_update(self, title: str, content: str) -> bool:
        """Append a heading + paragraph block to the Notion page."""
        if not self.available:
            return False
        try:
            self.client.blocks.children.append(
                block_id=settings.notion_page_id,
                children=[
                    {
                        "object": "block",
                        "type": "heading_2",
                        "heading_2": {"rich_text": [{"type": "text", "text": {"content": title}}]},
                    },
                    {
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {"rich_text": [{"type": "text", "text": {"content": content[:2000]}}]},
                    },
                ],
            )
            return True
        except APIResponseError as exc:
            self.last_error = f"Notion API error: {exc}"
            logger.warning(self.last_error)
            return False
        except Exception as exc:  # noqa: BLE001 - network errors etc.
            self.last_error = f"Notion push failed: {exc.__class__.__name__}"
            logger.warning(self.last_error)
            return False
