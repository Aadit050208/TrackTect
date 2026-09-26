"""TrackTect pipeline orchestration.

`PipelineRunner.run()` executes the agent pipeline for one competitor:

    orchestrator (plan) -> scraper -> summarizer -> classifier -> triage
                        -> landing-page watcher -> twitter -> youtube -> notion
                        -> pattern detection / source discovery / adaptive frequency
                          (tracked competitors only, via run_and_store)

Every agent failure degrades gracefully into a logged warning and a per-agent
status the UI can render — one failing agent never takes down the run.
"""

import logging
from typing import Dict, List, Optional

import db
from agents.adaptive_frequency_agent import AdaptiveFrequencyAgent
from agents.classifier_agent import ClassifierAgent
from agents.deep_scan_agent import DeepScanAgent
from agents.landing_page_agent import LandingPageWatcherAgent
from agents.news_agent import NewsAgent
from agents.notion_agent import NotionAgent
from agents.orchestrator_agent import OrchestratorAgent
from agents.pattern_agent import PatternDetectionAgent
from agents.scraper_agent import ScraperAgent
from agents.section_analyzer_agent import SectionAnalyzerAgent
from agents.source_discovery_agent import SourceDiscoveryAgent
from agents.summarizer_agent import SummarizerAgent
from agents.triage_agent import TriageAgent
from agents.twitter_agent_selenium import TwitterSeleniumScraper
from agents.youtube_agent import YouTubeAgent
from notifiers import notify_all
from url_utils import end_run, normalize_url, try_begin_run
from agents.llm_client import reset_token_usage, take_token_usage
import usage as usage_mod

logger = logging.getLogger(__name__)


class PipelineRunner:
    """Run the full agent pipeline for a single competitor URL."""

    def __init__(self) -> None:
        self.orchestrator = OrchestratorAgent()
        self.scraper = ScraperAgent()
        self.summarizer = SummarizerAgent()
        self.classifier = ClassifierAgent()
        self.triage = TriageAgent()
        self.section_analyzer = SectionAnalyzerAgent()
        self.deep_scan = DeepScanAgent()
        self.landing_watcher = LandingPageWatcherAgent()
        self.news_agent = NewsAgent()
        self.pattern_agent = PatternDetectionAgent()
        self.source_discovery = SourceDiscoveryAgent()
        self.adaptive_frequency = AdaptiveFrequencyAgent()

    def run(
        self,
        url: str,
        twitter_handle: str = "",
        youtube_url: str = "",
        enable_twitter: bool = True,
        enable_youtube: bool = True,
        enable_notion: bool = True,
        competitor_name: Optional[str] = None,
        extra_sources: Optional[List[str]] = None,
        consecutive_failures: int = 0,
        roadmap_items: Optional[List[str]] = None,
    ) -> Dict:
        """Execute the pipeline; never raises."""
        logs: List[str] = []
        status: Dict[str, Dict] = {}
        url = normalize_url(url) or url
        name = competitor_name or url

        def log(line: str) -> None:
            logs.append(line)
            logger.info("[pipeline:%s] %s", name, line)

        log(f"Starting TrackTect agent pipeline for {name}")

        # 0) Orchestrator
        log("Orchestrator: planning which agents to run...")
        try:
            plan = self.orchestrator.plan(
                url, twitter_handle, youtube_url,
                enable_twitter=enable_twitter,
                enable_youtube=enable_youtube,
                enable_notion=enable_notion,
            )
            # Normalize planner targets + extras so tracking junk never hits the scraper twice.
            plan["scrape_targets"] = [
                normalize_url(t) or t for t in plan.get("scrape_targets") or [url]
            ]
            for source in extra_sources or []:
                clean_source = normalize_url(source) or source
                if clean_source and clean_source not in plan["scrape_targets"]:
                    plan["scrape_targets"].append(clean_source)
                    plan["reasons"].append(f"Extra tracked source: {clean_source}")
            for reason in plan["reasons"]:
                log(f"   • {reason}")
            status["planner"] = {
                "status": "ok",
                "message": f"{len(plan['scrape_targets'])} target(s) planned",
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("Orchestrator failed")
            plan = {
                "scrape_targets": [url] + [
                    normalize_url(s) or s for s in (extra_sources or [])
                ],
                "changelog_url": None, "run_twitter": False, "twitter_handle": None,
                "run_youtube": False, "youtube_url": None, "run_notion": False,
                "run_news": True, "reasons": [],
            }
            status["planner"] = {
                "status": "error",
                "message": f"planner failed ({exc.__class__.__name__}); using defaults",
            }
            log("Orchestrator failed; falling back to default plan")

        # 1) Scraper (with cross-run self-healing when failures accumulate)
        log("Scraper: fetching page content...")
        scraped = self.scraper.run(plan["scrape_targets"], consecutive_failures=consecutive_failures)
        for note in self.scraper.attempts_log:
            log(f"   ↻ {note}")
        for target, reason in self.scraper.errors.items():
            log(f"Couldn't scrape {target}: {reason}")
        if scraped:
            status["scraper"] = {
                "status": "ok",
                "message": f"scraped {len(scraped)}/{len(plan['scrape_targets'])} page(s)",
            }
        else:
            status["scraper"] = {"status": "error", "message": "couldn't reach any page"}
            log("No pages could be scraped; skipping summarize/classify steps")

        # 2–4) Summarizer / Classifier / Triage
        insights: List[Dict] = []
        summary_text: Optional[str] = None
        if scraped:
            preferred = plan.get("changelog_url")
            source_url = preferred if preferred in scraped else next(iter(scraped))
            log(f"Summarizer: condensing content from {source_url}...")
            summary_text = self.summarizer.summarize(scraped[source_url], source_url)
            if summary_text:
                status["summarizer"] = {"status": "ok", "message": "summary generated"}
                log("Classifier: categorising changes...")
                classified = self.classifier.classify(summary_text, source_url)
                if classified:
                    status["classifier"] = {
                        "status": "ok",
                        "message": f"{len(classified)} item(s) classified",
                    }
                    log("Triage: scoring significance of each change...")
                    insights = self.triage.triage(classified, roadmap=roadmap_items or [])
                    highs = sum(1 for i in insights if i["severity"] == "high")
                    review = sum(1 for i in insights if i.get("needs_review"))
                    roadmap_hits = sum(1 for i in insights if i.get("roadmap_match"))
                    status["triage"] = {
                        "status": "ok",
                        "message": (
                            f"{highs} high-priority · {review} need review"
                            + (f" · {roadmap_hits} roadmap overlap" if roadmap_hits else "")
                        ),
                    }
                    for item in insights:
                        reason = item.get("triage_reason", "")
                        flag = " [needs review]" if item.get("needs_review") else ""
                        road = f" · roadmap: {item['roadmap_match']}" if item.get("roadmap_match") else ""
                        log(f"   - [{item['severity']}] [{item['category']}] {item['text']}{flag}{road}")
                        if reason:
                            log(f"       ↳ {reason}")
                else:
                    status["classifier"] = {
                        "status": "warn",
                        "message": "LLM unavailable or returned no valid items",
                    }
                    status["triage"] = {"status": "skipped", "message": "nothing to triage"}
                    log("Classification produced no items (is the LLM endpoint running?)")
            else:
                status["summarizer"] = {"status": "warn", "message": "LLM unavailable; no summary"}
                status["classifier"] = {"status": "skipped", "message": "no summary to classify"}
                status["triage"] = {"status": "skipped", "message": "nothing to triage"}
                log("Summarization unavailable (check LLM_BASE_URL in .env); continuing without insights")

            # Deep scan across ALL scraped pages (products, discounts, collabs, careers, CSR)
            try:
                log(f"Deep scan: mining {len(scraped)} page(s) for products / offers / collabs / careers / impact...")
                deep_hits = self.deep_scan.scan(scraped, competitor_name=name)
                if deep_hits:
                    # Merge without exact text dupes
                    existing_text = { (i.get("text") or "").lower() for i in insights }
                    added = 0
                    for hit in deep_hits:
                        key = (hit.get("text") or "").lower()
                        if key in existing_text:
                            continue
                        insights.append(hit)
                        existing_text.add(key)
                        added += 1
                    status["deep_scan"] = {
                        "status": "ok",
                        "message": f"{added} site finding(s) from deep pages",
                    }
                    for hit in deep_hits[:8]:
                        log(f"   ◆ [{hit['severity']}] [{hit['category']}] {hit['text'][:140]}")
                else:
                    status["deep_scan"] = {
                        "status": "warn",
                        "message": "no structured findings on scraped pages",
                    }
                    log("Deep scan: no product/offer/collab/career/impact lines matched")
            except Exception as exc:  # noqa: BLE001
                logger.exception("Deep scan failed")
                status["deep_scan"] = {
                    "status": "error",
                    "message": f"deep scan crashed ({exc.__class__.__name__})",
                }
                log("Deep scan failed; continuing")
        else:
            status["summarizer"] = {"status": "skipped", "message": "nothing scraped"}
            status["classifier"] = {"status": "skipped", "message": "nothing scraped"}
            status["triage"] = {"status": "skipped", "message": "nothing scraped"}
            status["deep_scan"] = {"status": "skipped", "message": "nothing scraped"}

        # 4b) Section-wise PM brief — use combined text from all scraped pages
        sections: List[Dict] = []
        page_for_sections = ""
        if scraped:
            chunks = []
            for page_url, page_text in scraped.items():
                chunks.append(f"## {page_url}\n{(page_text or '')[:6000]}")
            page_for_sections = "\n\n".join(chunks)[:24000]
        try:
            log("Section analyzer: building PM section-wise brief from all pages...")
            sections = self.section_analyzer.analyze(
                insights=insights,
                page_text=page_for_sections,
                competitor_name=name,
                url=url,
            )
            if sections:
                status["sections"] = {
                    "status": "ok",
                    "message": f"{len(sections)} section(s) filled",
                }
                for sec in sections:
                    log(f"   ▸ {sec['section']}: {sec.get('summary', '')}")
                    for bullet in (sec.get("bullets") or [])[:3]:
                        log(f"       • {bullet}")
            else:
                status["sections"] = {
                    "status": "warn",
                    "message": "no section signals yet",
                }
                log("Section analyzer: nothing section-worthy this run")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Section analyzer failed")
            status["sections"] = {
                "status": "error",
                "message": f"section analyzer crashed ({exc.__class__.__name__})",
            }
            log("Section analyzer failed; continuing")

        # 5) Landing page watcher (reuse scraped text when available — no second fetch)
        log("Landing page watcher: diffing messaging against last snapshot...")
        landing_changes: Dict[str, Dict] = {}
        try:
            landing_changes = self.landing_watcher.run(
                plan["scrape_targets"], prefetched=scraped or {},
            )
            changed = [u for u, r in landing_changes.items() if r["status"] == "changed"]
            failed = [u for u, r in landing_changes.items() if r["status"] == "failed"]
            for target, result in landing_changes.items():
                if result["status"] == "changed":
                    log(f"Messaging changes detected on {target}:")
                    for line in result["diff"][:40]:
                        log(f"   {line}")
                    if len(result["diff"]) > 40:
                        log(f"   ... ({len(result['diff']) - 40} more diff lines)")
                elif result["status"] == "first_snapshot":
                    log(f"First snapshot saved for {target} (diffs start next run)")
                elif result["status"] == "no_change":
                    log(f"No messaging changes on {target}")
                else:
                    log(f"Couldn't check {target}: {result.get('reason', 'unknown error')}")
            if failed and not changed:
                status["landing"] = {
                    "status": "warn",
                    "message": f"couldn't reach {len(failed)} page(s)",
                }
            else:
                status["landing"] = {"status": "ok", "message": f"{len(changed)} page(s) changed"}
        except Exception as exc:  # noqa: BLE001
            logger.exception("Landing page watcher failed")
            status["landing"] = {
                "status": "error",
                "message": f"watcher crashed ({exc.__class__.__name__})",
            }
            log("Landing page watcher failed; continuing")

        # 6) Twitter
        tweets: List[str] = []
        if plan["run_twitter"]:
            log(f"Twitter: scraping recent tweets from @{plan['twitter_handle']}...")
            scraper = TwitterSeleniumScraper(plan["twitter_handle"], max_tweets=5)
            tweets = scraper.scrape()
            if tweets:
                status["twitter"] = {"status": "ok", "message": f"{len(tweets)} tweet(s) scraped"}
                for i, tweet in enumerate(tweets, 1):
                    log(f"   Tweet {i}: {tweet[:200]}")
            else:
                status["twitter"] = {
                    "status": "warn",
                    "message": scraper.last_error or "no tweets found",
                }
                log(f"Twitter: {scraper.last_error or 'no tweets found'}")
        else:
            status["twitter"] = {"status": "skipped", "message": "skipped by planner"}

        # 7) YouTube
        videos: List[Dict] = []
        if plan["run_youtube"]:
            log(f"YouTube: scraping recent videos from {plan['youtube_url']}...")
            yt = YouTubeAgent(plan["youtube_url"], max_videos=5)
            videos = yt.scrape()
            if videos:
                status["youtube"] = {"status": "ok", "message": f"{len(videos)} video(s) scraped"}
                for video in videos:
                    log(f"   {video['title']} — {video['url']}")
            else:
                status["youtube"] = {
                    "status": "warn",
                    "message": yt.last_error or "no videos found",
                }
                log(f"YouTube: {yt.last_error or 'no videos found'}")
        else:
            status["youtube"] = {"status": "skipped", "message": "skipped by planner"}

        # 7b) News — funding, campaigns, partnerships, engagement headlines
        news_items: List[Dict] = []
        if plan.get("run_news", True):
            log(f"News: searching recent headlines for {name}...")
            try:
                news_items = self.news_agent.fetch(competitor_name=name, url=url)
                if news_items:
                    status["news"] = {
                        "status": "ok",
                        "message": f"{len(news_items)} headline(s)",
                    }
                    for item in news_items[:6]:
                        log(
                            f"   📰 [{item.get('signal_label', item.get('signal_type'))}] "
                            f"{item.get('title', '')[:120]}"
                        )
                else:
                    status["news"] = {
                        "status": "warn",
                        "message": self.news_agent.last_error or "no headlines found",
                    }
                    log(f"News: {self.news_agent.last_error or 'no headlines found'}")
            except Exception as exc:  # noqa: BLE001
                logger.exception("News agent failed")
                status["news"] = {
                    "status": "error",
                    "message": f"news crashed ({exc.__class__.__name__})",
                }
                log("News agent failed; continuing")
        else:
            status["news"] = {"status": "skipped", "message": "skipped by planner"}

        # 8) Notion
        if plan["run_notion"] and (insights or sections or news_items):
            log("Notion: pushing digest...")
            notion = NotionAgent()
            lines = [f"{name} — Latest PM intelligence:"]
            if sections:
                lines.append("Sections:")
                for sec in sections:
                    lines.append(f"- {sec['section']}: {sec.get('summary', '')}")
            lines += [
                f"- [{i['severity']}] [{i['category']}] {i['text']}"
                + (f" ({i.get('triage_reason', '')})" if i.get("triage_reason") else "")
                for i in insights
            ]
            if news_items:
                lines.append("News:")
                for n in news_items[:5]:
                    lines.append(f"- [{n.get('signal_label', 'News')}] {n.get('title', '')}")
            if notion.append_update(title=name, content="\n".join(lines)):
                status["notion"] = {"status": "ok", "message": "digest pushed"}
                log("Digest pushed to Notion")
            else:
                status["notion"] = {
                    "status": "warn",
                    "message": notion.last_error or "push failed",
                }
                log(f"Notion push failed: {notion.last_error}")
        else:
            reason = "skipped by planner" if not plan["run_notion"] else "no insights to push"
            status["notion"] = {"status": "skipped", "message": reason}

        log("Pipeline finished")
        return {
            "logs": logs,
            "plan": plan,
            "summary": summary_text,
            "insights": insights,
            "sections": sections,
            "news_items": news_items,
            "landing_changes": landing_changes,
            "tweets": tweets,
            "videos": videos,
            "agent_status": status,
            "scrape_ok": bool(scraped),
        }


def run_and_store(
    competitor: Dict,
    trigger: str = "manual",
    *,
    consume_quota: bool = True,
) -> int:
    """Run the pipeline for a tracked competitor, persist everything, and send alerts.

    consume_quota: when True (default for scheduled jobs), spends one user search
    unit before running. HTTP handlers that already called usage.try_consume should
    pass consume_quota=False to avoid double-charging.
    """
    competitor_id = competitor["id"]
    user_id = competitor.get("user_id")

    if consume_quota and user_id:
        ok, err = usage_mod.try_consume(int(user_id))
        if not ok:
            logger.warning("Quota exhausted for user %s — skipping run", user_id)
            run_id = db.create_run(competitor_id, trigger=trigger)
            db.finish_run(
                run_id,
                "error",
                [err or "Quota exhausted"],
                {"pipeline": {"status": "skipped", "message": "quota exhausted"}},
                tokens_used=0,
            )
            return run_id

    # Deduplicate overlapping manual clicks / scheduler overlap for the same competitor.
    if not try_begin_run(competitor_id):
        logger.warning(
            "Skipping run for competitor %s — another run is already in progress",
            competitor.get("name") or competitor_id,
        )
        run_id = db.create_run(competitor_id, trigger=trigger)
        db.finish_run(
            run_id,
            "error",
            ["Skipped: another check is already running for this competitor (protects API rate limits)"],
            {"pipeline": {"status": "skipped", "message": "already running"}},
            tokens_used=0,
        )
        return run_id

    # Persist a cleaned URL (strip Google Ads / utm junk) so future runs stay lean.
    clean_url = normalize_url(competitor.get("url") or "")
    if clean_url and clean_url != competitor.get("url"):
        try:
            db.update_competitor_config(competitor_id, url=clean_url)
            competitor = {**competitor, "url": clean_url}
            logger.info("Normalized competitor %s URL → %s", competitor_id, clean_url)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to persist normalized URL")

    run_id = db.create_run(competitor_id, trigger=trigger)
    runner = PipelineRunner()
    extra = []
    try:
        import json
        extra = json.loads(competitor.get("extra_sources") or "[]")
    except (TypeError, ValueError):
        extra = []

    # Discover deep pages (pricing/products/careers/partners/offers/impact) and
    # auto-attach before scrape so this same run covers them.
    try:
        offered = runner.source_discovery.discover(competitor_id, competitor["url"])
        extra = db.get_extra_sources(competitor_id)
        if offered:
            auto_n = sum(1 for o in offered if o.get("auto_tracked"))
            logger.info(
                "Source discovery for %s: %s found (%s auto-tracked)",
                competitor.get("name"), len(offered), auto_n,
            )
    except Exception:  # noqa: BLE001
        logger.exception("Pre-run source discovery failed")

    reset_token_usage()
    try:
        roadmap = db.get_roadmap_items(int(user_id)) if user_id else []
        try:
            result = runner.run(
                url=competitor["url"],
                twitter_handle=competitor.get("twitter_handle") or "",
                youtube_url=competitor.get("youtube_url") or "",
                enable_twitter=bool(competitor.get("enable_twitter", 1)),
                enable_youtube=bool(competitor.get("enable_youtube", 1)),
                enable_notion=bool(competitor.get("enable_notion", 1)),
                competitor_name=competitor.get("name"),
                extra_sources=extra,
                consecutive_failures=int(competitor.get("consecutive_failures") or 0),
                roadmap_items=roadmap,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Pipeline crashed for competitor %s", competitor.get("name"))
            tokens = take_token_usage()
            db.finish_run(
                run_id, "error",
                [f"Pipeline crashed: {exc.__class__.__name__}"],
                {},
                tokens_used=tokens,
            )
            db.bump_failure(competitor_id)
            return run_id

        tokens = take_token_usage()
        if tokens:
            result["logs"].append(f"LLM tokens used this run: {tokens}")

        # Track consecutive scrape failures for health + self-healing.
        if result.get("scrape_ok"):
            db.reset_failures(competitor_id)
        else:
            failures = db.bump_failure(competitor_id)
            result["logs"].append(
                f"Consecutive fetch failures for this competitor: {failures}"
            )

        if result["insights"]:
            db.add_insights(run_id, competitor_id, result["insights"])
        if result.get("sections"):
            db.add_section_briefs(run_id, competitor_id, result["sections"])
        if result.get("news_items"):
            db.add_news_signals(run_id, competitor_id, result["news_items"])
        for url, change in result["landing_changes"].items():
            db.add_diff(run_id, competitor_id, url, change["status"], change.get("diff", []))
        if result["tweets"]:
            db.add_social_items(
                run_id, competitor_id, "twitter",
                [{"content": t} for t in result["tweets"]],
            )
        if result["videos"]:
            db.add_social_items(
                run_id, competitor_id, "youtube",
                [{"title": v["title"], "url": v["url"], "content": v["description"]}
                 for v in result["videos"]],
            )

        # Cross-run pattern detection
        try:
            patterns = runner.pattern_agent.analyze(competitor_id, run_id=run_id)
            if patterns:
                result["logs"].append(f"Pattern agent: {len(patterns)} trend(s) flagged")
                for p in patterns:
                    result["logs"].append(f"   ↗ {p['message']}")
                    db.add_alert(competitor_id, run_id, f"Pattern: {p['message']}", severity="medium")
        except Exception:  # noqa: BLE001
            logger.exception("Pattern detection failed")

        # Source discovery already ran pre-scrape; log any remaining pending offers.
        try:
            pending = db.get_pending_sources(competitor_id)
            if pending:
                result["logs"].append(
                    f"Source discovery: {len(pending)} page(s) still awaiting confirmation"
                )
        except Exception:  # noqa: BLE001
            logger.exception("Pending source listing failed")

        # Adaptive frequency suggestion (never auto-applies)
        try:
            suggestion = runner.adaptive_frequency.analyze(dict(competitor))
            if suggestion:
                result["logs"].append(
                    f"Adaptive frequency suggestion: {suggestion['from_hours']}h → "
                    f"{suggestion['to_hours']}h — {suggestion['reason']}"
                )
        except Exception:  # noqa: BLE001
            logger.exception("Adaptive frequency analysis failed")

        overall = "ok" if any(s["status"] == "ok" for s in result["agent_status"].values()) else "error"
        db.finish_run(run_id, overall, result["logs"], result["agent_status"], tokens_used=tokens)

        high_items = [i for i in result["insights"] if i["severity"] == "high"]
        for item in high_items:
            message = f"[{item['category']}] {item['text']}"
            db.add_alert(competitor_id, run_id, message, severity="high")
            notify_all({
                "competitor": competitor.get("name", competitor["url"]),
                "severity": "high",
                "message": message,
                "url": competitor["url"],
                "reason": item.get("triage_reason", ""),
            })

        for item in result["insights"]:
            if item.get("needs_review"):
                db.add_alert(
                    competitor_id, run_id,
                    f"Needs review: [{item['category']}] {item['text']}",
                    severity="medium",
                )

        for item in result.get("news_items") or []:
            if item.get("signal_type") in ("funding", "partnership", "product_launch"):
                db.add_alert(
                    competitor_id, run_id,
                    f"News [{item.get('signal_label', item.get('signal_type'))}]: {item.get('title', '')}",
                    severity="high" if item.get("signal_type") == "funding" else "medium",
                )

        return run_id
    finally:
        end_run(competitor_id)


def run_adhoc(urls: List[str], twitter_handle: str = "", youtube_url: str = "") -> Dict:
    """One-off 'check now' run for ad-hoc URLs (no persistence)."""
    reset_token_usage()
    runner = PipelineRunner()
    merged: Dict = {"logs": [], "results": [], "tokens_used": 0}
    for url in urls:
        clean = normalize_url(url) or url
        result = runner.run(
            clean, twitter_handle=twitter_handle, youtube_url=youtube_url, enable_notion=False,
        )
        merged["logs"].extend(result["logs"])
        merged["results"].append({"url": clean, **result})
    merged["tokens_used"] = take_token_usage()
    if merged["tokens_used"]:
        merged["logs"].append(f"LLM tokens used: {merged['tokens_used']}")
    return merged
