"""TrackTect web app.

Flask UI: auth, dashboard, comparison, search, saved views, digests, bulk
actions, per-competitor timeline/settings, suggestions, onboarding, global
settings, and alert center. Scheduled tracking runs via APScheduler.
"""

import functools
import json
import logging
from datetime import datetime, timedelta, timezone

from flask import (Flask, Response, abort, flash, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import auth_security
import db
from db_connection import DatabaseUnavailable
import digests
import pm_exports
import scheduler
import usage as usage_mod
from backend_logic import run_adhoc, run_and_store
from config import settings
from url_utils import normalize_url

logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = settings.secret_key
# Session cookies: harden for live demos (HTTPS should still be used in production).
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(settings.session_cookie_secure),
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
)
if auth_security.is_weak_secret_key(settings.secret_key):
    logger.warning(
        "SECRET_KEY looks weak or default — set a long random value in .env before going live."
    )

db.init_db()


@app.before_request
def _require_database():
    """Never serve app pages against a missing/wiped local file in production."""
    if request.endpoint == "static":
        return None
    if db.is_available():
        return None
    logger.error("Database down: %s", db.unavailable_reason())
    return render_template("unavailable.html"), 503


@app.errorhandler(DatabaseUnavailable)
def _database_unavailable(_exc):
    return render_template("unavailable.html"), 503


# ------------------------------------------------------------- helpers ----

def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        if not db.is_admin_user(session["user_id"]):
            flash("Admin access required.", "error")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)
    return wrapped


def _own_competitor_or_404(competitor_id: int):
    competitor = db.get_competitor(competitor_id)
    if competitor is None or competitor["user_id"] != session["user_id"] or not competitor["active"]:
        abort(404)
    return competitor


def _parse_id_list(raw) -> list:
    if raw is None:
        return []
    if isinstance(raw, list):
        values = raw
    else:
        values = [raw]
    out = []
    for v in values:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


@app.template_filter("timeago")
def timeago(iso: str) -> str:
    if not iso:
        return "never"
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    delta = datetime.now(timezone.utc) - then
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


@app.template_filter("fromjson")
def fromjson(value: str):
    try:
        return json.loads(value or "[]")
    except ValueError:
        return []


@app.template_filter("domain")
def domain(url: str) -> str:
    return (url or "").split("//")[-1].split("/")[0]


@app.context_processor
def inject_shell():
    if not db.is_available() or "user_id" not in session:
        return {
            "sidebar_competitors": [],
            "sidebar_views": [],
            "sidebar_tags": [],
            "unread_alerts": [],
            "unread_count": 0,
            "quota_used": 0,
            "quota_limit": 0,
            "quota_left": 0,
            "quota_label": "",
            "is_admin": False,
        }
    uid = session["user_id"]
    unread = db.get_alerts(uid, unseen_only=True, limit=20)
    used, quota, left = usage_mod.remaining(uid)
    return {
        "sidebar_competitors": db.get_competitors(uid),
        "sidebar_views": db.get_saved_views(uid),
        "sidebar_tags": db.get_tags(uid),
        "unread_alerts": unread,
        "unread_count": len(unread),
        "quota_used": used,
        "quota_limit": quota,
        "quota_left": left,
        "quota_label": usage_mod.friendly_quota_label(uid),
        "is_admin": db.is_admin_user(uid),
    }


# ---------------------------------------------------------------- auth ----

def _auth_client_ip() -> str:
    return auth_security.client_ip(
        request.remote_addr,
        request.headers.get("X-Forwarded-For"),
    )


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        ip = _auth_client_ip()
        allowed, retry_after = auth_security.allow_register_attempt(ip)
        if not allowed:
            flash(
                f"Too many sign-ups from this network. Try again in about {max(1, retry_after // 60)} minute(s).",
                "error",
            )
            return render_template("register.html"), 429

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("password_confirm", "")

        err = auth_security.validate_username(username)
        if err:
            flash(err, "error")
        elif (err := auth_security.validate_password(password)):
            flash(err, "error")
        elif confirm and confirm != password:
            flash("Passwords do not match.", "error")
        else:
            user_id = db.create_user(username, generate_password_hash(password))
            if user_id is None:
                flash("That username is already taken.", "error")
            else:
                session.clear()
                session.permanent = True
                session["user_id"] = user_id
                session["username"] = username
                return redirect(url_for("dashboard"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        ip = _auth_client_ip()
        allowed, retry_after = auth_security.allow_login_attempt(ip, username)
        if not allowed:
            flash(
                f"Too many login attempts. Wait about {max(1, retry_after // 60)} minute(s) and try again.",
                "error",
            )
            return render_template("login.html"), 429

        user = db.get_user_by_username(username)
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session.permanent = True
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Invalid username or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ------------------------------------------------------------ dashboard ----

@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    uid = session["user_id"]
    user = db.get_user_by_id(uid)
    tag = request.args.get("tag") or None
    view_id = request.args.get("view", type=int)
    filters = {"tag": tag, "severity": None, "category": None, "days": None, "needs_review": None}
    active_view = None

    if view_id:
        active_view = db.get_saved_view(view_id)
        if active_view and active_view["user_id"] == uid:
            try:
                filters.update(json.loads(active_view["filters"] or "{}"))
            except ValueError:
                pass
            tag = filters.get("tag") or tag

    # Query-string overrides for ad-hoc filtering
    for key in ("severity", "category", "tag"):
        if request.args.get(key):
            filters[key] = request.args.get(key)
    if request.args.get("days"):
        try:
            filters["days"] = int(request.args.get("days"))
        except ValueError:
            pass
    if request.args.get("needs_review") == "1":
        filters["needs_review"] = True
    if request.args.get("annotated") == "1":
        filters["annotated"] = True
    if request.args.get("roadmap") == "1":
        filters["roadmap"] = True

    overview = db.competitor_overview(uid, tag=filters.get("tag"))
    alerts = db.get_alerts(uid, unseen_only=True)
    feed = db.user_activity_feed(
        uid,
        limit=40,
        tag=filters.get("tag"),
        severity=filters.get("severity"),
        category=filters.get("category"),
        days=filters.get("days"),
        needs_review=filters.get("needs_review"),
        annotated_only=filters.get("annotated"),
        roadmap_only=filters.get("roadmap"),
    )
    suggestions = db.get_user_pending_suggestions(uid)
    daily_brief = db.build_daily_brief(uid, hours=48) if overview else None
    metrics = {
        "tracked": len(overview),
        "week_changes": sum(row["week_changes"] for row in overview),
        "high_alerts": len(alerts),
        "health_issues": sum(
            1 for row in overview if row["health"]["status"] in ("failing", "stale", "new")
        ),
    }
    show_onboarding = user and not user["has_onboarded"] and len(overview) == 0
    show_walkthrough = user and not user["has_onboarded"] and len(overview) > 0
    show_roadmap_setup = (
        user
        and not show_onboarding
        and db.roadmap_setup_needed(uid)
    )
    return render_template(
        "dashboard.html",
        overview=overview,
        alerts=alerts,
        feed=feed,
        metrics=metrics,
        daily_brief=daily_brief,
        filters=filters,
        active_view=active_view,
        suggestions=suggestions,
        show_onboarding=show_onboarding,
        show_walkthrough=show_walkthrough,
        show_roadmap_setup=show_roadmap_setup,
        roadmap_items=db.get_roadmap_items(uid),
        tags=db.get_tags(uid),
    )


@app.route("/how-it-works")
@login_required
def how_it_works():
    return render_template("how_it_works.html")


@app.route("/onboarding/dismiss", methods=["POST"])
@login_required
def onboarding_dismiss():
    db.mark_onboarded(session["user_id"])
    if request.headers.get("Accept", "").find("json") >= 0 or request.headers.get("X-Requested-With"):
        return ("", 204)
    return redirect(url_for("dashboard"))


@app.route("/onboarding/replay", methods=["POST"])
@login_required
def onboarding_replay():
    db.update_user_preferences(session["user_id"], has_onboarded=0)
    flash("Tour will show again on Overview.", "ok")
    return redirect(url_for("dashboard"))


@app.route("/alerts/seen", methods=["POST"])
@login_required
def alerts_seen():
    db.mark_alerts_seen(session["user_id"])
    next_url = request.form.get("next") or url_for("dashboard")
    return redirect(next_url)


# ----------------------------------------------------------- competitors ----

@app.route("/competitors/add", methods=["GET", "POST"])
@login_required
def add_competitor():
    user = db.get_user_by_id(session["user_id"])
    default_interval = (user["default_interval_hours"] if user else 24) or 24
    prefill = (request.args.get("prefill") or "").strip()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if len(name) < 2:
            flash("Type a company or brand name (at least 2 characters).", "error")
            return render_template(
                "add_competitor.html",
                default_interval=default_interval,
                prefill=name or prefill,
            )

        ok, err = usage_mod.try_consume(session["user_id"])
        if not ok:
            flash(err or usage_mod.quota_exhausted_message(session["user_id"]), "error")
            return render_template(
                "add_competitor.html",
                default_interval=default_interval,
                prefill=name,
            )

        from agents.discovery_agent import DiscoveryAgent
        discovery = DiscoveryAgent().discover(name)
        session["pending_discovery"] = discovery
        session["pending_discovery_interval"] = default_interval
        if not discovery.get("ok") and not discovery.get("website", {}).get("url"):
            flash(
                discovery.get("error")
                or "We couldn't find that company. Try a clearer brand name.",
                "error",
            )
            return render_template(
                "add_competitor_confirm.html",
                discovery=discovery,
                default_interval=default_interval,
            )
        return redirect(url_for("add_competitor_confirm"))
    return render_template(
        "add_competitor.html",
        default_interval=default_interval,
        prefill=prefill,
    )


@app.route("/competitors/add/confirm", methods=["GET", "POST"])
@login_required
def add_competitor_confirm():
    user = db.get_user_by_id(session["user_id"])
    default_interval = session.get("pending_discovery_interval") or (
        (user["default_interval_hours"] if user else 24) or 24
    )
    discovery = session.get("pending_discovery")
    if not discovery:
        flash("Start by typing a company name.", "error")
        return redirect(url_for("add_competitor"))

    if request.method == "POST":
        name = request.form.get("name", "").strip() or discovery.get("name") or "Competitor"
        url = normalize_url(request.form.get("url", "").strip())
        if not url:
            flash("Please confirm or enter the website URL.", "error")
            return render_template(
                "add_competitor_confirm.html",
                discovery=discovery,
                default_interval=default_interval,
            )
        try:
            interval = max(1, int(request.form.get("interval_hours", default_interval)))
        except ValueError:
            interval = default_interval
        twitter = request.form.get("twitter_handle", "").strip().lstrip("@")
        instagram = request.form.get("instagram_handle", "").strip().lstrip("@")
        youtube = request.form.get("youtube_url", "").strip()
        # Empty means "not found" — don't invent handles.
        competitor_id = db.add_competitor(
            user_id=session["user_id"],
            name=name,
            url=url,
            twitter_handle=twitter,
            youtube_url=youtube,
            interval_hours=interval,
            instagram_handle=instagram,
        )
        # If no twitter found, disable twitter agent so orchestrator skips cleanly.
        db.update_competitor_config(
            competitor_id,
            enable_twitter=1 if twitter else 0,
            enable_youtube=1 if youtube else 0,
        )
        tags_raw = request.form.get("tags", "")
        if tags_raw.strip():
            db.set_competitor_tags(
                competitor_id, session["user_id"],
                [t.strip() for t in tags_raw.split(",") if t.strip()],
            )
        # Persist discovery news against a first light run? Optional — store as news with run later.
        # Attach news to competitor via a synthetic run so briefing shows them.
        if discovery.get("news"):
            run_id = db.create_run(competitor_id, trigger="discovery")
            db.add_news_signals(run_id, competitor_id, discovery["news"])
            db.finish_run(
                run_id, "ok",
                ["Saved news headlines from discovery (no LLM check yet)."],
                {"discovery": {"status": "ok", "message": "sources confirmed"}},
                tokens_used=0,
            )
        scheduler.schedule_competitor(competitor_id, interval)
        db.mark_onboarded(session["user_id"])
        session.pop("pending_discovery", None)
        session.pop("pending_discovery_interval", None)
        flash(
            f"Now tracking {name}. We found their pages automatically — "
            "run Check now when you want a full update.",
            "ok",
        )
        return redirect(url_for("competitor_detail", competitor_id=competitor_id))

    return render_template(
        "add_competitor_confirm.html",
        discovery=discovery,
        default_interval=default_interval,
    )


@app.route("/competitor/<int:competitor_id>")
@login_required
def competitor_detail(competitor_id: int):
    competitor = _own_competitor_or_404(competitor_id)
    timeline = db.competitor_timeline(competitor_id)
    insight_ids = [e["id"] for e in timeline if e.get("type") == "insight" and e.get("id")]
    notes = db.notes_for_insights(session["user_id"], insight_ids)
    for event in timeline:
        if event.get("type") == "insight":
            event["note"] = notes.get(event.get("id"), "")
    runs = db.get_runs(competitor_id, limit=20)
    tags = db.get_competitor_tags(competitor_id)
    suggestions = db.get_pending_suggestions(competitor_id)
    pending_sources = db.get_pending_sources(competitor_id)
    patterns = db.get_patterns(competitor_id, limit=10)
    extra = []
    try:
        extra = json.loads(competitor["extra_sources"] or "[]")
    except (TypeError, ValueError):
        pass
    return render_template(
        "competitor.html",
        competitor=competitor,
        timeline=timeline,
        runs=runs,
        tags=tags,
        suggestions=suggestions,
        pending_sources=pending_sources,
        patterns=patterns,
        extra_sources=extra,
        sections=db.get_competitor_sections(competitor_id, days=21),
        news=db.get_competitor_news(competitor_id, days=21),
        pricing_history=db.pricing_history(competitor_id),
        pricing_spark=db.pricing_change_counts_by_week(competitor_id),
    )


@app.route("/competitor/<int:competitor_id>/settings", methods=["POST"])
@login_required
def competitor_settings(competitor_id: int):
    competitor = _own_competitor_or_404(competitor_id)
    try:
        interval = max(1, int(request.form.get("interval_hours", competitor["interval_hours"])))
    except ValueError:
        interval = competitor["interval_hours"]
    db.update_competitor_config(
        competitor_id,
        name=request.form.get("name", competitor["name"]).strip() or competitor["name"],
        twitter_handle=request.form.get("twitter_handle", "").strip().lstrip("@"),
        youtube_url=request.form.get("youtube_url", "").strip(),
        interval_hours=interval,
        enable_twitter=1 if request.form.get("enable_twitter") else 0,
        enable_youtube=1 if request.form.get("enable_youtube") else 0,
        enable_notion=1 if request.form.get("enable_notion") else 0,
    )
    tags_raw = request.form.get("tags", "")
    db.set_competitor_tags(
        competitor_id, session["user_id"],
        [t.strip() for t in tags_raw.split(",") if t.strip()],
    )
    if not competitor["paused"]:
        scheduler.schedule_competitor(competitor_id, interval)
    flash("Settings saved.", "ok")
    return redirect(url_for("competitor_detail", competitor_id=competitor_id))


@app.route("/competitor/<int:competitor_id>/delete", methods=["POST"])
@login_required
def competitor_delete(competitor_id: int):
    competitor = _own_competitor_or_404(competitor_id)
    db.delete_competitor(competitor_id)
    scheduler.unschedule_competitor(competitor_id)
    flash(f"Stopped tracking {competitor['name']}.", "ok")
    return redirect(url_for("dashboard"))


@app.route("/competitor/<int:competitor_id>/run", methods=["POST"])
@login_required
def competitor_run(competitor_id: int):
    competitor = _own_competitor_or_404(competitor_id)
    ok, err = usage_mod.try_consume(session["user_id"])
    if not ok:
        flash(err or usage_mod.quota_exhausted_message(session["user_id"]), "error")
        return redirect(url_for("competitor_detail", competitor_id=competitor_id))
    run_id = run_and_store(dict(competitor), trigger="manual", consume_quota=False)
    run = db.get_run(run_id)
    logs = []
    if run:
        try:
            logs = json.loads(run["logs"] or "[]")
        except (TypeError, ValueError):
            logs = []
    if logs and any("already running" in str(line).lower() for line in logs):
        flash(
            "A check is already running for this competitor. Wait for it to finish.",
            "error",
        )
        return redirect(url_for("competitor_detail", competitor_id=competitor_id))
    if logs and any("quota" in str(line).lower() for line in logs):
        flash(usage_mod.quota_exhausted_message(session["user_id"]), "error")
        return redirect(url_for("competitor_detail", competitor_id=competitor_id))
    flash("Check started — this used 1 of your searches.", "ok")
    return redirect(url_for("run_detail", run_id=run_id))


@app.route("/suggestion/<int:suggestion_id>/<action>", methods=["POST"])
@login_required
def suggestion_resolve(suggestion_id: int, action: str):
    if action not in ("accept", "reject"):
        abort(400)
    row = db.resolve_suggestion(suggestion_id, "accepted" if action == "accept" else "rejected")
    if row is None:
        abort(404)
    competitor = _own_competitor_or_404(row["competitor_id"])
    if action == "accept" and row["kind"] == "interval":
        try:
            payload = json.loads(row["payload"] or "{}")
            hours = int(payload.get("to_hours", competitor["interval_hours"]))
            db.update_competitor_config(competitor["id"], interval_hours=hours)
            if not competitor["paused"]:
                scheduler.schedule_competitor(competitor["id"], hours)
            flash(f"Check frequency for {competitor['name']} updated to every {hours}h.", "ok")
        except (TypeError, ValueError):
            flash("Could not apply interval suggestion.", "error")
    elif action == "reject":
        flash("Suggestion dismissed.", "ok")
    return redirect(request.referrer or url_for("competitor_detail", competitor_id=competitor["id"]))


@app.route("/source/<int:source_id>/<action>", methods=["POST"])
@login_required
def source_resolve(source_id: int, action: str):
    if action not in ("accept", "reject"):
        abort(400)
    row = db.resolve_discovered_source(source_id, "accepted" if action == "accept" else "rejected")
    if row is None:
        abort(404)
    competitor = _own_competitor_or_404(row["competitor_id"])
    if action == "accept":
        db.add_extra_source(competitor["id"], row["url"])
        flash(f"Now also tracking {row['kind']} page: {row['url']}", "ok")
    else:
        flash("Suggested source dismissed.", "ok")
    return redirect(url_for("competitor_detail", competitor_id=competitor["id"]))


# ----------------------------------------------------------- bulk actions ----

@app.route("/competitors/bulk", methods=["POST"])
@login_required
def competitors_bulk():
    ids = _parse_id_list(request.form.getlist("competitor_ids"))
    # Ownership check
    owned = []
    for cid in ids:
        c = db.get_competitor(cid)
        if c and c["user_id"] == session["user_id"] and c["active"]:
            owned.append(cid)
    action = request.form.get("action")
    if not owned:
        flash("No competitors selected.", "error")
        return redirect(url_for("dashboard"))

    if action == "pause":
        db.set_competitors_paused(owned, True)
        for cid in owned:
            scheduler.unschedule_competitor(cid)
        flash(f"Paused {len(owned)} competitor(s).", "ok")
    elif action == "resume":
        db.set_competitors_paused(owned, False)
        for cid in owned:
            c = db.get_competitor(cid)
            if c:
                scheduler.schedule_competitor(cid, c["interval_hours"])
        flash(f"Resumed {len(owned)} competitor(s).", "ok")
    elif action == "frequency":
        try:
            hours = max(1, int(request.form.get("interval_hours", 24)))
        except ValueError:
            hours = 24
        db.set_competitors_interval(owned, hours)
        for cid in owned:
            c = db.get_competitor(cid)
            if c and not c["paused"]:
                scheduler.schedule_competitor(cid, hours)
        flash(f"Updated frequency to every {hours}h for {len(owned)} competitor(s).", "ok")
    elif action == "delete":
        db.delete_competitors(owned)
        for cid in owned:
            scheduler.unschedule_competitor(cid)
        flash(f"Stopped tracking {len(owned)} competitor(s).", "ok")
    else:
        flash("Unknown bulk action.", "error")
    return redirect(url_for("dashboard"))


# -------------------------------------------------------- comparison ----

@app.route("/compare", methods=["GET", "POST"])
@login_required
def compare():
    comps = db.get_competitors(session["user_id"])
    selected = []
    data = None
    days = 14
    if request.method == "POST":
        selected = _parse_id_list(request.form.getlist("competitor_ids"))[:3]
        try:
            days = max(1, int(request.form.get("days", 14)))
        except ValueError:
            days = 14
        # Ownership
        selected = [
            cid for cid in selected
            if (c := db.get_competitor(cid)) and c["user_id"] == session["user_id"]
        ]
        if len(selected) < 2:
            flash("Pick at least 2 competitors to compare.", "error")
        else:
            data = db.comparison_data(selected, days=days)
    return render_template("compare.html", competitors=comps, selected=selected, data=data, days=days)


# ------------------------------------------------------------- search ----

@app.route("/search")
@login_required
def search():
    q = (request.args.get("q") or "").strip()
    uid = session["user_id"]
    matched_competitors = db.search_competitors(uid, q) if q else []
    change_results = db.search_changes(uid, q) if q else {}
    market = None
    if q:
        try:
            from agents.market_intel import lookup_market_intel_cached
            market = lookup_market_intel_cached(q, max_news=10)
        except Exception:
            try:
                from agents.market_intel import lookup_market_intel
                market = lookup_market_intel(q, max_news=10)
            except Exception:
                app.logger.exception("Market intel lookup failed for %s", q)
                market = None
    return render_template(
        "search.html",
        query=q,
        matched_competitors=matched_competitors,
        results=change_results,
        market=market,
    )


# -------------------------------------------------------- saved views ----

@app.route("/views/save", methods=["POST"])
@login_required
def save_view():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Give the view a name.", "error")
        return redirect(url_for("dashboard"))
    filters = {}
    for key in ("tag", "severity", "category"):
        if request.form.get(key):
            filters[key] = request.form.get(key)
    if request.form.get("days"):
        try:
            filters["days"] = int(request.form.get("days"))
        except ValueError:
            pass
    if request.form.get("needs_review") == "1":
        filters["needs_review"] = True
    view_id = db.create_saved_view(session["user_id"], name, filters)
    flash(f"Saved view “{name}”.", "ok")
    return redirect(url_for("dashboard", view=view_id))


@app.route("/views/<int:view_id>/delete", methods=["POST"])
@login_required
def delete_view(view_id: int):
    db.delete_saved_view(view_id, session["user_id"])
    flash("View deleted.", "ok")
    return redirect(url_for("dashboard"))


# --------------------------------------------------------------- digests ----

@app.route("/digests")
@login_required
def digests_list():
    items = db.get_digests(session["user_id"])
    user = db.get_user_by_id(session["user_id"])
    return render_template("digests.html", digests=items, digest_enabled=bool(user and user["digest_enabled"]))


@app.route("/digests/generate", methods=["POST"])
@login_required
def digests_generate():
    digest_id = digests.generate_and_store(session["user_id"], days=7, notify=False)
    flash("Digest generated.", "ok")
    return redirect(url_for("digest_detail", digest_id=digest_id))


@app.route("/digests/<int:digest_id>")
@login_required
def digest_detail(digest_id: int):
    item = db.get_digest(digest_id)
    if item is None or item["user_id"] != session["user_id"]:
        abort(404)
    return render_template("digest_detail.html", digest=item)


@app.route("/digests/<int:digest_id>/download")
@login_required
def digest_download(digest_id: int):
    item = db.get_digest(digest_id)
    if item is None or item["user_id"] != session["user_id"]:
        abort(404)
    filename = f"tracktect-digest-{item['created_at'][:10]}.md"
    return Response(
        item["content"],
        mimetype="text/markdown",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ----------------------------------------------------------------- runs ----

@app.route("/run/<int:run_id>")
@login_required
def run_detail(run_id: int):
    run = db.get_run(run_id)
    if run is None:
        abort(404)
    competitor = db.get_competitor(run["competitor_id"]) if run["competitor_id"] else None
    if competitor is None or competitor["user_id"] != session["user_id"]:
        abort(404)
    return render_template(
        "run.html",
        run=run,
        competitor=competitor,
        insights=db.get_run_insights(run_id),
        diffs=db.get_run_diffs(run_id),
        social=db.get_run_social(run_id),
        sections=db.get_run_sections(run_id),
        news=db.get_run_news(run_id),
    )


# ---------------------------------------------------------- PM briefing ----

@app.route("/briefing")
@login_required
def pm_briefing():
    try:
        days = max(1, min(90, int(request.args.get("days", 7))))
    except ValueError:
        days = 7
    briefing = db.pm_briefing_data(session["user_id"], days=days)
    return render_template("briefing.html", briefing=briefing, days=days)


# ---------------------------------------------------------- quick check ----

@app.route("/check", methods=["GET", "POST"])
@login_required
def quick_check():
    result = None
    if request.method == "POST":
        urls = [u.strip() for u in request.form.get("urls", "").split(",") if u.strip()]
        urls = [normalize_url(u) for u in urls]
        urls = [u for u in urls if u]
        if not urls:
            flash("Enter at least one URL.", "error")
        else:
            ok, err = usage_mod.try_consume(session["user_id"])
            if not ok:
                flash(err or usage_mod.quota_exhausted_message(session["user_id"]), "error")
            else:
                result = run_adhoc(
                    urls,
                    twitter_handle=request.form.get("twitter_handle", "").strip(),
                    youtube_url=request.form.get("youtube_url", "").strip(),
                )
                flash("Quick check used 1 of your searches.", "ok")
    return render_template("check.html", result=result)


# ------------------------------------------------------------- settings ----

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings_page():
    user = db.get_user_by_id(session["user_id"])
    if request.method == "POST":
        try:
            default_interval = max(1, int(request.form.get("default_interval_hours", 24)))
        except ValueError:
            default_interval = 24
        db.update_user_preferences(
            session["user_id"],
            digest_enabled=1 if request.form.get("digest_enabled") else 0,
            digest_daily=1 if request.form.get("digest_daily") else 0,
            default_interval_hours=default_interval,
        )
        raw = request.form.get("roadmap_items")
        if raw is not None:
            lines = [ln.strip() for ln in raw.replace("\r\n", "\n").split("\n") if ln.strip()]
            db.set_roadmap_items(session["user_id"], lines, mark_setup_done=True)
        flash("Settings saved.", "ok")
        return redirect(url_for("settings_page"))
    return render_template(
        "settings.html",
        user=user,
        roadmap_items=db.get_roadmap_items(session["user_id"]),
        email_configured=settings.email_configured,
        webhook_configured=bool(settings.notify_webhook_url),
    )


# --------------------------------------------------------------- admin ----

@app.route("/admin")
@admin_required
def admin_panel():
    users = db.get_all_users()
    return render_template("admin.html", users=users)


@app.route("/admin/users/<int:user_id>/quota", methods=["POST"])
@admin_required
def admin_set_quota(user_id: int):
    target = db.get_user_by_id(user_id)
    if target is None:
        abort(404)
    if user_id == session["user_id"]:
        flash("You can't change your own quota.", "error")
        return redirect(url_for("admin_panel"))
    try:
        quota = max(0, int(request.form.get("usage_quota", settings.default_user_quota)))
    except ValueError:
        flash("Invalid quota value.", "error")
        return redirect(url_for("admin_panel"))
    reset = bool(request.form.get("reset_count"))
    db.set_user_quota(user_id, quota, reset_count=reset)
    action = "reset usage and set quota" if reset else "updated quota"
    flash(f"{action.capitalize()} for {target['username']} → {quota}.", "ok")
    return redirect(url_for("admin_panel"))


@app.route("/admin/users/<int:user_id>/reset-usage", methods=["POST"])
@admin_required
def admin_reset_usage(user_id: int):
    target = db.get_user_by_id(user_id)
    if target is None:
        abort(404)
    if user_id == session["user_id"]:
        flash("You can't reset your own usage.", "error")
        return redirect(url_for("admin_panel"))
    db.reset_user_usage(user_id)
    flash(f"Reset usage count for {target['username']}.", "ok")
    return redirect(url_for("admin_panel"))


# --------------------------------------------------------------- export ----

@app.route("/export/digest.md")
@login_required
def export_digest():
    body = digests.build_digest_markdown(session["user_id"], days=7)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return Response(
        body,
        mimetype="text/markdown",
        headers={"Content-Disposition": f"attachment; filename=tracktect-digest-{today}.md"},
    )


# --------------------------------------------------------- PM features ----

@app.route("/roadmap", methods=["POST"])
@login_required
def roadmap_save():
    raw = request.form.get("roadmap_items") or ""
    lines = [ln.strip() for ln in raw.replace("\r\n", "\n").split("\n") if ln.strip()]
    db.set_roadmap_items(session["user_id"], lines, mark_setup_done=True)
    flash("Roadmap saved.", "ok")
    nxt = request.form.get("next") or url_for("settings_page")
    return redirect(nxt)


@app.route("/roadmap/skip", methods=["POST"])
@login_required
def roadmap_skip():
    db.mark_roadmap_setup_done(session["user_id"])
    flash("You can add roadmap themes anytime in Settings.", "ok")
    return redirect(url_for("dashboard"))


@app.route("/insights/<int:insight_id>/note", methods=["POST"])
@login_required
def insight_note(insight_id: int):
    note = request.form.get("note", "")
    if not db.set_insight_note(insight_id, session["user_id"], note):
        abort(404)
    flash("Note saved.", "ok")
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/insights/<int:insight_id>/decision", methods=["POST"])
@login_required
def insight_decision(insight_id: int):
    decision = request.form.get("decision", "")
    owner = request.form.get("owner", "")
    if not db.set_insight_decision(insight_id, session["user_id"], decision, owner):
        abort(404)
    flash("Decision saved.", "ok")
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/insights/<int:insight_id>/ticket")
@login_required
def insight_ticket(insight_id: int):
    insight = db.get_insight_for_user(insight_id, session["user_id"])
    if insight is None:
        abort(404)
    body = pm_exports.build_ticket_markdown(
        insight, competitor_name=insight.get("competitor_name") or ""
    )
    if request.args.get("download") == "1":
        return Response(
            body,
            mimetype="text/markdown",
            headers={
                "Content-Disposition": f"attachment; filename=tracktect-ticket-{insight_id}.md"
            },
        )
    return render_template(
        "ticket_draft.html",
        insight=insight,
        markdown=body,
        quota_left=usage_mod.remaining(session["user_id"])[2],
    )


@app.route("/insights/<int:insight_id>/ticket/detailed", methods=["POST"])
@login_required
def insight_ticket_detailed(insight_id: int):
    insight = db.get_insight_for_user(insight_id, session["user_id"])
    if insight is None:
        abort(404)
    err = pm_exports.consume_or_error(session["user_id"])
    if err:
        flash(err, "error")
        return redirect(url_for("insight_ticket", insight_id=insight_id))
    body, gen_err = pm_exports.enrich_ticket_with_llm(
        insight, competitor_name=insight.get("competitor_name") or ""
    )
    if gen_err or not body:
        flash(gen_err or "Detailed draft failed.", "error")
        return redirect(url_for("insight_ticket", insight_id=insight_id))
    flash("Detailed draft ready — used 1 search.", "ok")
    return render_template(
        "ticket_draft.html",
        insight=insight,
        markdown=body,
        detailed=True,
        quota_left=usage_mod.remaining(session["user_id"])[2],
    )


@app.route("/parity")
@login_required
def feature_parity():
    matrix = db.feature_parity_matrix(session["user_id"])
    return render_template(
        "parity.html",
        matrix=matrix,
        roadmap=db.get_roadmap_items(session["user_id"]),
    )


@app.route("/parity/cell")
@login_required
def feature_parity_cell():
    feature = (request.args.get("feature") or "").strip()
    try:
        competitor_id = int(request.args.get("competitor_id") or 0)
    except ValueError:
        competitor_id = 0
    _own_competitor_or_404(competitor_id)
    matrix = db.feature_parity_matrix(session["user_id"])
    cell = (matrix.get("cells") or {}).get(feature, {}).get(competitor_id)
    if cell is None:
        abort(404)
    comp = next((c for c in matrix["competitors"] if c["id"] == competitor_id), None)
    return render_template(
        "parity_cell.html",
        feature=feature,
        competitor=comp,
        cell=cell,
    )


@app.route("/competitor/<int:competitor_id>/battlecard", methods=["GET", "POST"])
@login_required
def competitor_battlecard(competitor_id: int):
    competitor = _own_competitor_or_404(competitor_id)
    snapshot = db.competitor_snapshot_for_battlecard(competitor_id)
    positioning = ""
    if request.method == "POST":
        if request.form.get("confirm") != "1":
            flash("Confirm that you’re okay using 1 search first.", "error")
            return redirect(url_for("competitor_battlecard", competitor_id=competitor_id))
        err = pm_exports.consume_or_error(session["user_id"])
        if err:
            flash(err, "error")
            return redirect(url_for("competitor_detail", competitor_id=competitor_id))
        positioning, gen_err = pm_exports.synthesize_battlecard_positioning(snapshot)
        if gen_err:
            flash(gen_err, "error")
            return redirect(url_for("competitor_battlecard", competitor_id=competitor_id))
        flash("Battlecard ready — used 1 search.", "ok")
    body = pm_exports.build_battlecard_markdown(snapshot, positioning=positioning or "")
    if request.args.get("download") == "1":
        safe = "".join(ch if ch.isalnum() else "-" for ch in competitor["name"])[:40]
        return Response(
            body,
            mimetype="text/markdown",
            headers={"Content-Disposition": f"attachment; filename=battlecard-{safe}.md"},
        )
    return render_template(
        "battlecard.html",
        competitor=competitor,
        markdown=body,
        positioning=positioning,
        quota_left=usage_mod.remaining(session["user_id"])[2],
        snapshot=snapshot,
    )


@app.route("/quarterly", methods=["GET", "POST"])
@login_required
def quarterly_review():
    uid = session["user_id"]
    today = datetime.now(timezone.utc).date()
    # Default: current calendar quarter
    q = (today.month - 1) // 3
    default_start = today.replace(month=q * 3 + 1, day=1)
    if q == 3:
        default_end = today.replace(month=12, day=31)
    else:
        next_q = default_start.replace(month=default_start.month + 3)
        default_end = next_q - timedelta(days=1)

    start_s = request.values.get("start") or default_start.isoformat()
    end_s = request.values.get("end") or default_end.isoformat()
    try:
        start_dt = datetime.fromisoformat(start_s).replace(tzinfo=timezone.utc)
        end_dt = datetime.fromisoformat(end_s).replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc
        )
    except ValueError:
        flash("Use dates like YYYY-MM-DD.", "error")
        start_dt = datetime.combine(default_start, datetime.min.time(), tzinfo=timezone.utc)
        end_dt = datetime.combine(default_end, datetime.max.time(), tzinfo=timezone.utc)
        start_s, end_s = default_start.isoformat(), default_end.isoformat()

    range_start = start_dt.isoformat()
    range_end = end_dt.isoformat()
    insights = db.insights_in_range(uid, range_start, range_end)
    grouped: dict = {}
    for item in insights:
        grouped.setdefault(item.get("category") or "Other", []).append(item)

    cached = db.get_quarterly_summary(uid, range_start, range_end)
    summary = cached["summary"] if cached else ""

    if request.method == "POST" and request.form.get("action") in ("generate", "regenerate"):
        if request.form.get("confirm") != "1":
            flash("Confirm that you’re okay using 1 search first.", "error")
        else:
            err = pm_exports.consume_or_error(uid)
            if err:
                flash(err, "error")
            else:
                text, gen_err = pm_exports.synthesize_quarterly_summary(
                    insights, range_start, range_end
                )
                if gen_err or not text:
                    flash(gen_err or "Summary failed.", "error")
                else:
                    db.save_quarterly_summary(uid, range_start, range_end, text)
                    summary = text
                    flash("Quarterly summary ready — used 1 search.", "ok")

    if request.args.get("download") == "1":
        lines = [
            f"# Quarterly review — {start_s} to {end_s}",
            "",
            summary or "_No summary generated yet._",
            "",
        ]
        for cat, items in grouped.items():
            lines.append(f"## {cat}")
            for it in items:
                lines.append(
                    f"- **{it.get('competitor_name')}** [{it.get('severity')}]: {it.get('text')}"
                )
            lines.append("")
        body = "\n".join(lines)
        return Response(
            body,
            mimetype="text/markdown",
            headers={
                "Content-Disposition": f"attachment; filename=tracktect-quarterly-{start_s}.md"
            },
        )

    return render_template(
        "quarterly.html",
        start=start_s,
        end=end_s,
        grouped=grouped,
        summary=summary,
        insight_count=len(insights),
        quota_left=usage_mod.remaining(uid)[2],
        has_cache=bool(cached),
    )


# ----------------------------------------------------------------- boot ----

def _start_scheduler_once() -> None:
    import os
    if settings.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return
    if not db.is_available():
        logger.error("Scheduler not started — database is unavailable")
        return
    scheduler.start()


_start_scheduler_once()


if __name__ == "__main__":
    app.run(debug=settings.debug, use_reloader=settings.debug)
