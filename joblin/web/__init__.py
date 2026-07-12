"""The bundled web UI: Joblin's schedule in a phone browser.

Runs *inside* the bot process — an aiohttp server on the same asyncio event
loop (aiohttp is already a discord.py dependency), so there is no second
service or tmux pane to manage. It is enabled only when ``WEB_BASE_URL``,
``DISCORD_CLIENT_ID`` and ``DISCORD_CLIENT_SECRET`` are all set; without them
the bot runs exactly as before and never opens a port.

Scope: a glanceable schedule plus convenient create/edit/delete for tasks,
pitch-ins, and do-em-ups. It deliberately does **not** complete chores or
award puntos — the ✅ lifecycle (finalizing the Discord post, the ↩️ undo
anchor, 👏 claps) is keyed off Discord message ids and stays in Discord, so
the economy has one door.

Auth
----
Discord OAuth2 (scopes ``identify guilds``) is the sign-in; there are no
Joblin accounts. After the code exchange we keep only the user's identity and
the ids of the guilds they share with the bot, in an HMAC-signed cookie (the
secret is auto-generated once into ``DATA_DIR/web_secret``); the Discord
tokens themselves are discarded. Guild access = (user's guilds at sign-in)
∩ (guilds the bot is in *right now*, re-checked per request), so kicking the
bot from a server ends web access to it immediately; a member who leaves the
guild keeps access until their cookie expires (14 days) — acceptable for a
family tool. Mutations additionally require the ``X-Joblin: 1`` header
(cross-site forms can't send custom headers) on top of the SameSite=Lax
cookie, as CSRF protection.

API
---
The JSON surface mirrors the slash commands rather than inventing new
semantics: the schedule is assembled from a plain ``store.snapshot()``, and
task create/edit/delete reuse ``schedule_from_rule`` + the exact field
assignments of ``/newtask``, ``/edit task`` and ``/deletetask``. Pitch-in and
do-em-up edits go through :func:`apply_game_edit` — the very engine behind
``/edit pitchin`` / ``/edit doemup`` (including its live-post re-render) —
and game deletes mirror the game branch of ``/deletetask``. *Playing* the
games (✅, ➕/➖, 🏁, 👏) stays in Discord.
The store is always read as ``core.store`` (never a module global) so the
test suite's store swap is observed here too.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import hmac
import html
import json
import os
import pathlib
import secrets
import time
import urllib.parse
from typing import Any, Optional
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web

from ..models import (
    _ordinal,
    describe_repeat,
    duration_input,
    from_iso,
    new_id,
    now_utc,
    parse_repeat,
    recurrence_of,
    to_iso,
)
from ..bot import core
from ..bot.helpers import config_ready, guild_config, schedule_label
from ..bot.commands.edit import apply_game_edit
from ..bot.commands.tasks import _cancel_game_message, schedule_from_rule
from ..bot.reactions import _delete_panels, _take_task_panels

DISCORD_API = "https://discord.com/api/v10"
SESSION_COOKIE = "joblin_session"
STATE_COOKIE = "joblin_oauth_state"
SESSION_DAYS = 14
HTML_PATH = pathlib.Path(__file__).parent / "index.html"
STATIC_DIR = pathlib.Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# Settings & session signing
# ---------------------------------------------------------------------------
def web_settings() -> Optional[dict]:
    """The web UI's env configuration, or None when it should stay disabled."""
    base = (os.getenv("WEB_BASE_URL") or "").strip().rstrip("/")
    client_id = (os.getenv("DISCORD_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("DISCORD_CLIENT_SECRET") or "").strip()
    if not (base and client_id and client_secret):
        return None
    return {
        "base_url": base,
        "client_id": client_id,
        "client_secret": client_secret,
        "host": os.getenv("WEB_HOST", "0.0.0.0"),
        "port": int(os.getenv("WEB_PORT", "8710")),
        # The Secure cookie flag only works over https; an http base URL (e.g.
        # a bare VPS port while trying things out) still gets HttpOnly+SameSite.
        "secure": base.startswith("https://"),
    }


_secret_cache: Optional[bytes] = None


def web_secret() -> bytes:
    """The cookie-signing secret: ``WEB_SECRET`` if set, else a random one
    generated once into ``DATA_DIR/web_secret`` (so sessions survive restarts
    without any manual key management)."""
    global _secret_cache
    if _secret_cache is None:
        env = os.getenv("WEB_SECRET")
        if env:
            _secret_cache = env.encode()
        else:
            path = core.DATA_DIR / "web_secret"
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(secrets.token_hex(32), encoding="utf-8")
                path.chmod(0o600)
            _secret_cache = path.read_text(encoding="utf-8").strip().encode()
    return _secret_cache


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_session(payload: dict, secret: bytes) -> str:
    """``payload`` (JSON) → ``body.sig`` with an HMAC-SHA256 signature."""
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(secret, body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def read_session(token: str, secret: bytes, now: Optional[float] = None) -> Optional[dict]:
    """Verify + decode a session token; None if forged, mangled, or expired."""
    try:
        body, sig = token.split(".", 1)
        want = _b64(hmac.new(secret, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, want):
            return None
        payload = json.loads(_unb64(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("exp", 0) < (time.time() if now is None else now):
        return None
    return payload


# ---------------------------------------------------------------------------
# Schedule assembly (pure functions over a store snapshot)
# ---------------------------------------------------------------------------
_WD_INPUT = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def repeat_input_of(rule: dict) -> str:
    """A ``repeat`` string that round-trips through :func:`parse_repeat` —
    used to prefill the edit form (``describe_repeat`` is for humans and
    doesn't always parse back, e.g. "every 2 weeks")."""
    freq = rule["freq"]
    if freq == "days":
        n = rule.get("interval_days") or 1
        return "daily" if n == 1 else f"every {n} days"
    if freq == "weekly":
        return ",".join(_WD_INPUT[w] for w in sorted(rule.get("weekdays", [])))
    if freq == "monthly":
        days = ",".join(_ordinal(d) for d in sorted(rule.get("monthdays", [])))
        return f"monthly on the {days}"
    return "once"


def _task_item(t: dict, tz: ZoneInfo) -> dict:
    pending = t.get("pending")
    due = (pending or {}).get("due_at") or t.get("next_due")
    rule = recurrence_of(t)
    if rule["freq"] == "once":
        at_input = from_iso(due).astimezone(tz).strftime("%Y-%m-%d %H:%M") if due else ""
    else:
        at_input = t.get("time_of_day") or ""
    return {
        "kind": "task",
        "id": t["id"],
        "brief": t["brief"],
        "description": t.get("description") or "",
        "bounty": bool(t.get("bounty")),
        "recurring": rule["freq"] != "once",
        "schedule_label": schedule_label(t),
        "repeat_input": repeat_input_of(rule),
        "at_input": at_input,
        # "pending" == the occurrence is live in Discord right now (fired,
        # awaiting ✅); "scheduled" == quietly waiting for next_due.
        "status": "pending" if pending else "scheduled",
        "due_at": due,
        "nag_count": t.get("nag_count", 0),
        "editable": True,
    }


def _game_item(g: dict, kind: str, tz: ZoneInfo) -> dict:
    live = bool(g.get("message_id"))
    closes_live = g.get("expires_at") if kind == "pitchin" else g.get("deadline")
    duration = g.get("duration_secs")
    opens_at = None if live else g.get("next_due")
    # Projected close for a scheduled round: open + stored window. Live uses the
    # concrete expires_at/deadline. Recurring with no duration runs until the
    # next slot — we leave closes_at None rather than re-running recurrence here.
    if live:
        closes_at = closes_live
    elif opens_at and duration:
        closes_at = to_iso(
            from_iso(opens_at) + dt.timedelta(seconds=int(duration))
        )
    else:
        closes_at = None
    rule = recurrence_of(g)
    label = describe_repeat(rule) if g.get("recurring") else "one-off"
    if g.get("recurring") and rule.get("time_of_day"):
        label += f" at {rule['time_of_day']}"
    # `at` prefill: a recurring game's slot is its wall-clock time; a deferred
    # one-off's is the instant its round opens; a live one-off has none (the
    # round is already running — only its close can still move).
    if g.get("recurring"):
        at_input = rule.get("time_of_day") or ""
    elif not live and g.get("next_due"):
        at_input = from_iso(g["next_due"]).astimezone(tz).strftime("%Y-%m-%d %H:%M")
    else:
        at_input = ""
    # `close` prefill: absolute wall time while live; relative window while
    # scheduled so an untouched save doesn't re-anchor (client only PATCHes when
    # the value differs from this snapshot).
    if live and closes_at:
        close_input = from_iso(closes_at).astimezone(tz).strftime("%Y-%m-%d %H:%M")
    elif duration:
        close_input = duration_input(int(duration))
    else:
        close_input = ""
    return {
        "kind": kind,  # "pitchin" | "doemup"
        "id": g["id"],
        "brief": g["brief"],
        "description": g.get("description") or "",
        "recurring": bool(g.get("recurring")),
        "schedule_label": label,
        "repeat_input": repeat_input_of(rule),
        "at_input": at_input,
        "close_input": close_input,
        # live → due_at is when the round closes (may be None: open-ended);
        # dormant/deferred → due_at is when the next round posts.
        "status": "live" if live else "scheduled",
        "due_at": closes_at if live else opens_at,
        "opens_at": opens_at,
        "closes_at": closes_at,
        "duration_secs": int(duration) if duration else None,
        "points_each": g.get("points_each", 1),
        "cap": g.get("max_scorers") if kind == "pitchin" else g.get("point_limit"),
        "editable": True,
    }


def _sort_key(item: dict) -> tuple:
    rank = 0 if item["status"] in ("pending", "live") else 1
    ts = from_iso(item["due_at"]).timestamp() if item.get("due_at") else float("inf")
    return (rank, ts, item["brief"].lower())


def build_schedule(snap: dict, guild_id: int) -> dict:
    """Everything the schedule view needs, from one snapshot: every task and
    game in the guild, each appearing once with its next relevant instant
    (windowing/grouping is the client's job — it has all items)."""
    cfg = guild_config(snap, guild_id) or {}
    tzname = cfg.get("timezone") or "UTC"
    try:
        tz = ZoneInfo(tzname)
    except Exception:
        tz, tzname = ZoneInfo("UTC"), "UTC"

    items = [
        _task_item(t, tz)
        for t in snap["tasks"].values()
        if str(t["guild_id"]) == str(guild_id)
    ]
    for kind, section in (("pitchin", "pitchins"), ("doemup", "doemups")):
        items += [
            _game_item(g, kind, tz)
            for g in snap[section].values()
            if str(g["guild_id"]) == str(guild_id)
        ]
    items.sort(key=_sort_key)
    return {
        "timezone": tzname,
        "config_ready": config_ready(cfg),
        "now": to_iso(now_utc()),
        "items": items,
    }


# ---------------------------------------------------------------------------
# Task mutations — mirrors of /newtask, /edit task, /deletetask
# ---------------------------------------------------------------------------
async def create_task(guild_id: int, user_id: int, fields: dict) -> tuple[Optional[dict], Optional[str]]:
    """Create a task exactly as ``/newtask`` would. Returns (task, error)."""
    snap = await core.store.snapshot()
    cfg = guild_config(snap, guild_id)
    if not config_ready(cfg):
        return None, "Run /joblinconfig in Discord to set a channel and timezone first."
    brief = str(fields.get("brief") or "").strip()[:200]
    if not brief:
        return None, "A brief is required."

    tz, now = ZoneInfo(cfg["timezone"]), now_utc()
    at = fields.get("at")
    try:
        sched = schedule_from_rule(
            parse_repeat(fields.get("repeat")), at, tz, now, at_given=at is not None
        )
    except ValueError as e:
        return None, str(e)

    description = str(fields.get("description") or "").strip()
    tid = new_id()
    task = {
        "id": tid,
        "guild_id": guild_id,
        "brief": brief,
        "description": description[:1500] if description else None,
        "bounty": bool(fields.get("bounty")),
        "recurring": sched["recurring"],
        "freq": sched["freq"],
        "interval_days": sched["interval_days"],
        "weekdays": sched["weekdays"],
        "monthdays": sched["monthdays"],
        "time_of_day": sched["time_of_day"],
        "next_due": to_iso(sched["next_due"]),
        "created_by": user_id,
        "created_at": to_iso(now),
        "pending": None,
    }
    async with core.store.txn() as data:
        data["tasks"][tid] = task
    return task, None


async def apply_task_edit(
    guild_id: int, tid: str, fields: dict
) -> tuple[Optional[dict], Optional[str], Optional[str]]:
    """Edit a task exactly as ``/edit task`` would. ``fields`` holds only the
    fields being changed (key *presence* is intent — the client omits anything
    untouched, so re-saving a brief never re-anchors the schedule). Returns
    (updated_task, note, error)."""
    snap = await core.store.snapshot()
    live = snap["tasks"].get(tid)
    if not live or str(live["guild_id"]) != str(guild_id):
        return None, None, "Task not found."

    recompute = "at" in fields or "repeat" in fields
    cfg = guild_config(snap, guild_id)
    if recompute and not config_ready(cfg):
        return None, None, "Set a timezone with /joblinconfig before changing the schedule."

    sched = None
    if recompute:
        tz, now = ZoneInfo(cfg["timezone"]), now_utc()
        try:
            new_rule = (parse_repeat(fields["repeat"]) if "repeat" in fields
                        else recurrence_of(live))
            sched = schedule_from_rule(
                new_rule, fields.get("at"), tz, now,
                at_given="at" in fields, default_tod=live.get("time_of_day"),
            )
        except ValueError as e:
            return None, None, str(e)

    updated = None
    note = None
    async with core.store.txn() as data:
        t = data["tasks"].get(tid)
        if t:
            if "brief" in fields:
                brief = str(fields["brief"] or "").strip()[:200]
                if brief:
                    t["brief"] = brief
            if "description" in fields:
                desc = str(fields["description"] or "").strip()
                t["description"] = desc[:1500] if desc else None
            if "bounty" in fields:
                t["bounty"] = bool(fields["bounty"])
            if sched is not None:
                t["recurring"] = sched["recurring"]
                t["freq"] = sched["freq"]
                t["interval_days"] = sched["interval_days"]
                t["weekdays"] = sched["weekdays"]
                t["monthdays"] = sched["monthdays"]
                t["time_of_day"] = sched["time_of_day"]
                if t.get("pending"):  # don't disturb a live occurrence
                    note = ("A reminder is live in Discord now; the new schedule "
                            "applies from the next cycle.")
                else:
                    t["next_due"] = to_iso(sched["next_due"])
            updated = json.loads(json.dumps(t))

    if not updated:
        return None, None, "Task not found."
    return updated, note, None


async def delete_task(guild_id: int, tid: str) -> Optional[dict]:
    """Delete a task exactly as ``/deletetask`` would (including sweeping its
    reaction-routing rows and any open snooze panels). Returns the removed
    task, or None if it wasn't found in this guild."""
    panels: list = []
    removed = None
    async with core.store.txn() as data:
        t = data["tasks"].get(tid)
        if t and str(t["guild_id"]) == str(guild_id):
            pending = t.get("pending")
            if pending:
                for mid in pending.get("message_ids", []):
                    data["messages"].pop(str(mid), None)
            for section in ("undo", "requeue", "claps"):
                for mid, rec in list(data[section].items()):
                    if rec.get("task_id") == tid:
                        data[section].pop(mid, None)
            panels = _take_task_panels(data, tid)
            removed = data["tasks"].pop(tid, None)
    await _delete_panels(panels)  # Discord I/O stays outside the txn
    return removed


# ---------------------------------------------------------------------------
# Game mutations — edits go through /edit's own engine (apply_game_edit,
# imported above); delete mirrors the game branch of /deletetask.
# ---------------------------------------------------------------------------
async def delete_game(guild_id: int, kind: str, eid: str) -> Optional[dict]:
    """Delete a pitch-in / do-em-up series exactly as ``/deletetask`` would:
    pop the row, sweep its reaction routing and claps, and strike through a
    live post as cancelled (delete ≠ close — no puntos are awarded). Returns
    the removed game, or None if it wasn't found in this guild."""
    section = "pitchins" if kind == "pitchin" else "doemups"
    removed, live_mid = None, None
    async with core.store.txn() as data:
        g = data[section].get(eid)
        if g and str(g["guild_id"]) == str(guild_id):
            live_mid = g.get("message_id")
            data["game_messages"].pop(str(live_mid), None)
            for mid, rec in list(data["claps"].items()):
                if rec.get("task_id") == eid:
                    data["claps"].pop(mid, None)
            removed = data[section].pop(eid, None)
    if removed and live_mid:  # Discord I/O stays outside the txn
        channel = (core.bot.get_channel(int(removed["channel_id"]))
                   if removed.get("channel_id") else None)
        if channel is not None:
            await _cancel_game_message(
                channel, removed["brief"], live_mid, is_doemup=(kind == "doemup"))
    return removed


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------
def _jerr(status: int, msg: str) -> web.Response:
    return web.json_response({"error": msg}, status=status)


def _page(title: str, body_html: str, status: int = 200) -> web.Response:
    """A tiny standalone page for the OAuth error paths."""
    doc = (
        "<!doctype html><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        "<body style='font-family:system-ui;max-width:26rem;margin:20vh auto 0;"
        "padding:0 1.5rem;line-height:1.5'>"
        f"<h1 style='font-size:1.2rem'>{html.escape(title)}</h1><p>{body_html}</p>"
    )
    return web.Response(text=doc, content_type="text/html", status=status)


def _session_of(request: web.Request) -> Optional[dict]:
    token = request.cookies.get(SESSION_COOKIE)
    return read_session(token, web_secret()) if token else None


def _bot_has_guild(gid: str) -> bool:
    try:
        return core.bot.get_guild(int(gid)) is not None
    except (ValueError, TypeError):
        return False


def _authed_guild(request: web.Request) -> tuple[Optional[dict], Optional[int], Optional[web.Response]]:
    """(session, guild_id, error_response) for the /api/guilds/{gid}/* routes."""
    sess = _session_of(request)
    if not sess:
        return None, None, _jerr(401, "Not signed in.")
    gid = request.match_info["gid"]
    if gid not in sess.get("guilds", []) or not _bot_has_guild(gid):
        return None, None, _jerr(403, "No access to that server.")
    return sess, int(gid), None


def _csrf_ok(request: web.Request) -> bool:
    return request.headers.get("X-Joblin") == "1"


async def _json_body(request: web.Request) -> Optional[dict]:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return body if isinstance(body, dict) else None


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def index(request: web.Request) -> web.StreamResponse:
    # The OpenGraph tags need absolute URLs, so the one {{BASE}} placeholder is
    # filled at serve time; link scrapers (Discord's unfurler included) get the
    # same page anonymously and read the tags without signing in.
    doc = HTML_PATH.read_text(encoding="utf-8").replace(
        "{{BASE}}", request.app["joblin_cfg"]["base_url"])
    return web.Response(text=doc, content_type="text/html",
                        headers={"Cache-Control": "no-store"})


def _asset(name: str):
    """A handler serving one file from ``static/`` (favicons, the OG image)."""
    async def handler(request: web.Request) -> web.StreamResponse:
        return web.FileResponse(STATIC_DIR / name,
                                headers={"Cache-Control": "public, max-age=86400"})
    return handler


async def login(request: web.Request) -> web.Response:
    cfg = request.app["joblin_cfg"]
    state = secrets.token_urlsafe(24)
    params = urllib.parse.urlencode({
        "client_id": cfg["client_id"],
        "redirect_uri": cfg["base_url"] + "/oauth/callback",
        "response_type": "code",
        "scope": "identify guilds",
        "state": state,
        "prompt": "none",  # returning users skip the consent screen
    })
    resp = web.Response(status=302, headers={
        "Location": f"https://discord.com/oauth2/authorize?{params}"})
    resp.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True,
                    samesite="Lax", secure=cfg["secure"], path="/")
    return resp


async def oauth_callback(request: web.Request) -> web.Response:
    cfg = request.app["joblin_cfg"]
    retry = "<a href='/login'>Try again</a>."
    state, want = request.query.get("state", ""), request.cookies.get(STATE_COOKIE, "")
    if not state or not want or not hmac.compare_digest(state, want):
        return _page("Sign-in failed", f"The sign-in link was stale. {retry}", 400)
    code = request.query.get("code")
    if not code:
        return _page("Sign-in cancelled", f"Discord didn't authorize us. {retry}", 400)

    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": cfg["base_url"] + "/oauth/callback",
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
    }
    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(f"{DISCORD_API}/oauth2/token", data=form) as r:
                if r.status != 200:
                    core.log.warning("oauth exchange failed: %s %s", r.status, await r.text())
                    return _page("Sign-in failed", f"Discord rejected the sign-in. {retry}", 502)
                tok = await r.json()
            headers = {"Authorization": f"Bearer {tok['access_token']}"}
            async with http.get(f"{DISCORD_API}/users/@me", headers=headers) as r:
                if r.status != 200:
                    return _page("Sign-in failed", f"Couldn't read your profile. {retry}", 502)
                me = await r.json()
            async with http.get(f"{DISCORD_API}/users/@me/guilds", headers=headers) as r:
                if r.status != 200:
                    return _page("Sign-in failed", f"Couldn't read your servers. {retry}", 502)
                their_guilds = await r.json()
    except aiohttp.ClientError:
        return _page("Sign-in failed", f"Couldn't reach Discord. {retry}", 502)

    # The bot may still be connecting right after a restart — give it a moment
    # so its guild list is real before we intersect against it.
    if not core.bot.is_ready():
        try:
            await asyncio.wait_for(core.bot.wait_until_ready(), timeout=15)
        except asyncio.TimeoutError:
            return _page("Not ready", f"The bot is still starting up. {retry}", 503)

    bot_gids = {str(g.id) for g in core.bot.guilds}
    mutual = [str(g["id"]) for g in their_guilds if str(g["id"]) in bot_gids]
    if not mutual:
        return _page(
            "No shared server",
            "This Discord account doesn't share a server with Joblin. "
            f"Join the family server first, then {retry}",
            403,
        )

    avatar = (
        f"https://cdn.discordapp.com/avatars/{me['id']}/{me['avatar']}.png?size=64"
        if me.get("avatar") else None
    )
    payload = {
        "uid": str(me["id"]),
        "name": me.get("global_name") or me.get("username") or "?",
        "avatar": avatar,
        "guilds": mutual,
        "exp": int(time.time()) + SESSION_DAYS * 86400,
    }
    resp = web.Response(status=302, headers={"Location": "/"})
    resp.set_cookie(SESSION_COOKIE, sign_session(payload, web_secret()),
                    max_age=SESSION_DAYS * 86400, httponly=True,
                    samesite="Lax", secure=cfg["secure"], path="/")
    resp.del_cookie(STATE_COOKIE, path="/")
    return resp


async def logout(request: web.Request) -> web.Response:
    resp = web.Response(status=302, headers={"Location": "/"})
    resp.del_cookie(SESSION_COOKIE, path="/")
    return resp


async def api_me(request: web.Request) -> web.Response:
    sess = _session_of(request)
    if not sess:
        return _jerr(401, "Not signed in.")
    guilds = []
    for gid in sess.get("guilds", []):
        g = core.bot.get_guild(int(gid)) if gid.isdigit() else None
        if g is not None:
            guilds.append({"id": gid, "name": g.name,
                           "icon": str(g.icon) if g.icon else None})
    return web.json_response({
        "user": {"id": sess["uid"], "name": sess["name"], "avatar": sess.get("avatar")},
        "guilds": guilds,
    })


async def api_schedule(request: web.Request) -> web.Response:
    _, gid, err = _authed_guild(request)
    if err:
        return err
    snap = await core.store.snapshot()
    return web.json_response(build_schedule(snap, gid))


async def api_task_create(request: web.Request) -> web.Response:
    sess, gid, err = _authed_guild(request)
    if err:
        return err
    if not _csrf_ok(request):
        return _jerr(403, "Bad request origin.")
    fields = await _json_body(request)
    if fields is None:
        return _jerr(400, "Expected a JSON object.")
    task, error = await create_task(gid, int(sess["uid"]), fields)
    if error:
        return _jerr(400, error)
    return web.json_response({"task": task}, status=201)


async def api_task_edit(request: web.Request) -> web.Response:
    _, gid, err = _authed_guild(request)
    if err:
        return err
    if not _csrf_ok(request):
        return _jerr(403, "Bad request origin.")
    fields = await _json_body(request)
    if fields is None:
        return _jerr(400, "Expected a JSON object.")
    updated, note, error = await apply_task_edit(gid, request.match_info["tid"], fields)
    if error:
        return _jerr(400, error)
    return web.json_response({"task": updated, "note": note})


async def api_task_delete(request: web.Request) -> web.Response:
    _, gid, err = _authed_guild(request)
    if err:
        return err
    if not _csrf_ok(request):
        return _jerr(403, "Bad request origin.")
    removed = await delete_task(gid, request.match_info["tid"])
    if not removed:
        return _jerr(404, "Task not found.")
    return web.json_response({"deleted": removed["id"]})


def _game_kind(request: web.Request) -> Optional[str]:
    kind = request.match_info["kind"]
    return kind if kind in ("pitchin", "doemup") else None


async def api_game_edit(request: web.Request) -> web.Response:
    _, gid, err = _authed_guild(request)
    if err:
        return err
    if not _csrf_ok(request):
        return _jerr(403, "Bad request origin.")
    kind = _game_kind(request)
    if kind is None:
        return _jerr(404, "No such game kind.")
    fields = await _json_body(request)
    if fields is None:
        return _jerr(400, "Expected a JSON object.")
    updated, note, error = await apply_game_edit(gid, kind, request.match_info["eid"], fields)
    if error:
        return _jerr(400, error)
    return web.json_response({"game": updated, "note": note})


async def api_game_delete(request: web.Request) -> web.Response:
    _, gid, err = _authed_guild(request)
    if err:
        return err
    if not _csrf_ok(request):
        return _jerr(403, "Bad request origin.")
    kind = _game_kind(request)
    if kind is None:
        return _jerr(404, "No such game kind.")
    removed = await delete_game(gid, kind, request.match_info["eid"])
    if not removed:
        return _jerr(404, f"{'Pitch-in' if kind == 'pitchin' else 'Do-em-up'} not found.")
    return web.json_response({"deleted": removed["id"]})


# ---------------------------------------------------------------------------
# Startup (called from JoblinBot.setup_hook — same event loop as the bot)
# ---------------------------------------------------------------------------
def build_app(cfg: dict) -> web.Application:
    app = web.Application()
    app["joblin_cfg"] = cfg
    app.add_routes([
        web.get("/", index),
        web.get("/favicon.ico", _asset("favicon.ico")),
        web.get("/favicon.png", _asset("favicon.png")),
        web.get("/apple-touch-icon.png", _asset("apple-touch-icon.png")),
        web.get("/og.jpg", _asset("og.jpg")),
        web.get("/login", login),
        web.get("/oauth/callback", oauth_callback),
        web.get("/logout", logout),
        web.get("/api/me", api_me),
        web.get("/api/guilds/{gid}/schedule", api_schedule),
        web.post("/api/guilds/{gid}/tasks", api_task_create),
        web.patch("/api/guilds/{gid}/tasks/{tid}", api_task_edit),
        web.delete("/api/guilds/{gid}/tasks/{tid}", api_task_delete),
        web.patch("/api/guilds/{gid}/games/{kind}/{eid}", api_game_edit),
        web.delete("/api/guilds/{gid}/games/{kind}/{eid}", api_game_delete),
    ])
    return app


async def start_web() -> bool:
    """Start the web UI if configured. Returns True if it is now listening.
    Never raises — a web failure (port taken, bad env) must not take the bot
    down with it."""
    cfg = web_settings()
    if cfg is None:
        core.log.info(
            "Web UI disabled — set WEB_BASE_URL, DISCORD_CLIENT_ID and "
            "DISCORD_CLIENT_SECRET to enable it."
        )
        return False
    try:
        runner = web.AppRunner(build_app(cfg), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, cfg["host"], cfg["port"]).start()
    except Exception:
        core.log.exception("Web UI failed to start — continuing without it")
        return False
    core.log.info("Web UI listening on %s:%s → %s", cfg["host"], cfg["port"], cfg["base_url"])
    return True


__all__ = [
    "apply_game_edit",
    "apply_task_edit",
    "build_app",
    "build_schedule",
    "create_task",
    "delete_game",
    "delete_task",
    "read_session",
    "repeat_input_of",
    "sign_session",
    "start_web",
    "web_secret",
    "web_settings",
]
