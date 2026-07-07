"""Lightweight smoke tests — run with: uv run python tests/smoke.py

No pytest dependency; just asserts. Covers the parts most likely to break:
importing the bot (validates all slash-command registrations), the DST-aware
recurrence math, and the store's transaction + atomic-write behavior.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import pathlib
import sys
import tempfile
from zoneinfo import ZoneInfo

# Make `joblin` importable when run straight from the repo (the package
# isn't pip-installed; running a script puts tests/ — not the root — on path).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# Importing the bot module runs every @bot.tree.command decorator and builds
# the JoblinBot instance — a real smoke test of the command definitions.
import joblin.bot  # noqa: E402, F401
import joblin.web as webui  # noqa: E402
from joblin import models as m  # noqa: E402
from joblin.store import Store  # noqa: E402

UTC = dt.timezone.utc


def test_emoji_key() -> None:
    assert m.emoji_key("ℹ️") == m.emoji_key("ℹ")
    assert m.emoji_key("✅") == "✅"


def test_time_parsing() -> None:
    assert m.parse_hhmm("8:00") == (8, 0)
    assert m.parse_hhmm("23:59") == (23, 59)
    assert m.normalise_hhmm("8:5".replace("5", "05")) == "08:05"
    for bad in ("24:00", "8:60", "abc", "8", "08-00"):
        try:
            m.parse_hhmm(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad!r} should have failed")


def test_first_due() -> None:
    tz = ZoneInfo("Europe/Berlin")
    # 09:00 local "now"; a 08:00 task already passed -> tomorrow.
    now = dt.datetime(2026, 6, 14, 9, 0, tzinfo=tz).astimezone(UTC)
    due = m.compute_first_due(now, tz, "08:00").astimezone(tz)
    assert (due.date() - now.astimezone(tz).date()).days == 1
    assert (due.hour, due.minute) == (8, 0)
    # a 10:00 task is still ahead -> today.
    due2 = m.compute_first_due(now, tz, "10:00").astimezone(tz)
    assert due2.date() == now.astimezone(tz).date()
    assert (due2.hour, due2.minute) == (10, 0)


def test_first_due_now_fires_immediately() -> None:
    # The "now" bug: a recurring task created mid-minute reduces "now" to its
    # HH:MM slot (seconds dropped), so today's slot is a few seconds in the past
    # the instant it's computed. It must still fire on the next tick, not skip a
    # full day. The current minute and the one just before it both count as now.
    tz = ZoneInfo("Europe/Berlin")
    now = dt.datetime(2026, 6, 14, 9, 5, 45, tzinfo=tz).astimezone(UTC)  # 09:05:45
    # "now" -> slot 09:05 today; with seconds elapsed it's just-passed -> fire now.
    same = m.compute_first_due(now, tz, "09:05")
    assert same <= now and same.astimezone(tz).hour == 9 and same.astimezone(tz).minute == 5
    # the previous minute (T-1m rounding) also counts as now.
    prev = m.compute_first_due(now, tz, "09:04")
    assert prev <= now and prev.astimezone(tz).minute == 4
    # but a genuinely older slot (>1 min past) still rolls to tomorrow.
    old = m.compute_first_due(now, tz, "09:03").astimezone(tz)
    assert (old.date() - now.astimezone(tz).date()).days == 1 and old.minute == 3
    # a still-future slot today is untouched.
    later = m.compute_first_due(now, tz, "18:00").astimezone(tz)
    assert later.date() == now.astimezone(tz).date() and later.hour == 18


def test_schedule_now_recurring_fires_today() -> None:
    # End-to-end: `at:"now"` through the command's scheduler-field builder must
    # land its first fire at/just-before now for every recurrence kind, so the
    # 30s scheduler loop posts it on the next tick rather than 24h+ later.
    from joblin.bot.commands import schedule_from_rule
    tz = ZoneInfo("Europe/Berlin")
    now = dt.datetime(2026, 6, 19, 13, 5, 30, tzinfo=tz).astimezone(UTC)  # Friday 13:05:30
    for repeat in ("daily", "every 2 days", "fri", "monthly", "weekly"):
        sched = schedule_from_rule(m.parse_repeat(repeat), "now", tz, now, at_given=True)
        assert sched["recurring"] and sched["time_of_day"] == "13:05"
        assert sched["next_due"] <= now, f"{repeat!r} created 'now' should fire immediately"


def test_weekly_pins_to_at_weekday() -> None:
    """`repeat: weekly` means *weekly on the day it starts*: the weekday named
    in `at` (or where the plain first fire lands) becomes a real weekly rule.
    Regression: "sunday 22:00" + "weekly" created after 22:00 on a Sunday used
    to keep only the time, first-fire tomorrow, and be a Monday task forever."""
    from joblin.bot.commands import schedule_from_rule
    from joblin.bot.commands.games import _game_recurrence_from
    tz = ZoneInfo("America/New_York")

    # Created Sunday 23:30, after the 22:00 slot -> next Sunday, still a Sunday.
    now = dt.datetime(2026, 7, 5, 23, 30, tzinfo=tz).astimezone(UTC)
    sched = schedule_from_rule(m.parse_repeat("weekly"), "sunday 22:00", tz, now, at_given=True)
    assert (sched["freq"], sched["weekdays"], sched["interval_days"]) == ("weekly", [6], 0)
    due = sched["next_due"].astimezone(tz)
    assert due.date() == dt.date(2026, 7, 12) and (due.hour, due.minute) == (22, 0)

    # Created Sunday morning -> today's 22:00 slot is still ahead.
    early = dt.datetime(2026, 7, 5, 10, 0, tzinfo=tz).astimezone(UTC)
    sched2 = schedule_from_rule(m.parse_repeat("weekly"), "sunday 22:00", tz, early, at_given=True)
    assert sched2["next_due"].astimezone(tz).date() == dt.date(2026, 7, 5)

    # The pinned day needn't be today: Tuesday + "fri 18:00" -> Fridays.
    tue = dt.datetime(2026, 7, 7, 9, 0, tzinfo=tz).astimezone(UTC)
    sched3 = schedule_from_rule(m.parse_repeat("weekly"), "fri 18:00", tz, tue, at_given=True)
    assert sched3["weekdays"] == [4]
    assert sched3["next_due"].astimezone(tz).date() == dt.date(2026, 7, 10)

    # "every 7 days" is the same cadence and pins the same way.
    sched4 = schedule_from_rule(m.parse_repeat("every 7 days"), "sunday 22:00", tz, now, at_given=True)
    assert (sched4["freq"], sched4["weekdays"]) == ("weekly", [6])

    # No `at` (e.g. /edit changing just the repeat): pins where the plain
    # first fire lands — tomorrow, since the kept 22:00 already passed today.
    sched5 = schedule_from_rule(m.parse_repeat("weekly"), None, tz, now,
                                at_given=False, default_tod="22:00")
    assert (sched5["freq"], sched5["weekdays"]) == ("weekly", [0])
    assert sched5["next_due"].astimezone(tz).date() == dt.date(2026, 7, 6)

    # Recurring pitch-ins/do-em-ups share the same pinning.
    rule = _game_recurrence_from("weekly", tz, now, at="sunday 22:00")
    assert (rule["freq"], rule["weekdays"]) == ("weekly", [6])
    assert m.first_due(rule, tz, now).astimezone(tz).date() == dt.date(2026, 7, 12)


def test_roll_forward_skips_backlog() -> None:
    tz = ZoneInfo("Europe/Berlin")
    prev = dt.datetime(2026, 6, 10, 8, 0, tzinfo=tz).astimezone(UTC)  # due 5 days ago
    now = dt.datetime(2026, 6, 14, 9, 0, tzinfo=tz).astimezone(UTC)
    # daily: next slot strictly after now -> tomorrow 08:00, NOT a backlog of 5.
    nxt = m.roll_forward(prev, tz, "08:00", 1, now).astimezone(tz)
    assert nxt.date() == dt.date(2026, 6, 15) and (nxt.hour, nxt.minute) == (8, 0)
    # every 2 days, anchored on the 10th -> 10,12,14,16; next after the 14th 09:00 is the 16th.
    nxt2 = m.roll_forward(prev, tz, "08:00", 2, now).astimezone(tz)
    assert nxt2.date() == dt.date(2026, 6, 16) and (nxt2.hour, nxt2.minute) == (8, 0)


def test_roll_forward_dst() -> None:
    # Spring-forward in Berlin is 2026-03-29 (02:00 -> 03:00). A daily 08:00 task
    # must stay at wall-clock 08:00 across the boundary, even though the UTC
    # offset changes from +01:00 to +02:00.
    tz = ZoneInfo("Europe/Berlin")
    prev = dt.datetime(2026, 3, 28, 8, 0, tzinfo=tz).astimezone(UTC)
    now = dt.datetime(2026, 3, 28, 8, 30, tzinfo=tz).astimezone(UTC)
    nxt = m.roll_forward(prev, tz, "08:00", 1, now).astimezone(tz)
    assert nxt.date() == dt.date(2026, 3, 29)
    assert (nxt.hour, nxt.minute) == (8, 0)
    assert prev.astimezone(tz).utcoffset() != nxt.utcoffset()  # offset really changed


def test_parse_clock() -> None:
    assert m.parse_clock("8") == (8, 0)
    assert m.parse_clock("8:30") == (8, 30)
    assert m.parse_clock("8am") == (8, 0)
    assert m.parse_clock("12am") == (0, 0)
    assert m.parse_clock("12pm") == (12, 0)
    assert m.parse_clock("8:15pm") == (20, 15)
    assert m.parse_clock("20:15") == (20, 15)
    for bad in ("25:00", "8:99", "xyz", "13pm"):
        try:
            m.parse_clock(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad!r} should have failed")


def test_resolve_when() -> None:
    tz = ZoneInfo("Europe/Berlin")
    now = dt.datetime(2026, 6, 19, 13, 5, tzinfo=tz).astimezone(UTC)  # a Friday

    def loc(s: str):
        return m.resolve_when(s, tz, now).astimezone(tz)

    assert m.resolve_when("", tz, now) == now
    assert m.resolve_when("now", tz, now) == now
    assert m.resolve_when("in 2h", tz, now) == now + dt.timedelta(hours=2)
    assert m.resolve_when("+3d", tz, now) == now + dt.timedelta(days=3)
    assert m.resolve_when("90m", tz, now) == now + dt.timedelta(minutes=90)
    t = loc("tomorrow 8am")
    assert (t.year, t.month, t.day, t.hour, t.minute) == (2026, 6, 20, 8, 0)
    assert (loc("18:00").hour, loc("18:00").minute) == (18, 0)
    assert (loc("6pm").hour, loc("6pm").minute) == (18, 0)
    mon = loc("mon 9:00")
    assert mon.weekday() == 0 and (mon.hour, mon.minute) == (9, 0)
    iso = loc("2026-06-20 14:00")
    assert (iso.month, iso.day, iso.hour) == (6, 20, 14)
    jun = loc("Jun 20")
    assert (jun.month, jun.day, jun.hour) == (6, 20, 9)
    for bad in ("nonsense", "32:00", "2026-13-40 10:00"):
        try:
            m.resolve_when(bad, tz, now)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad!r} should have failed")


def test_parse_repeat() -> None:
    P = m.parse_repeat
    assert P("")["freq"] == "once"
    assert P("once")["freq"] == "once"
    assert P("daily") == {"freq": "days", "interval_days": 1, "weekdays": [], "monthdays": []}
    assert P("every 2 days")["interval_days"] == 2
    assert P("3d")["interval_days"] == 3
    assert P("5")["interval_days"] == 5
    assert P("weekly")["interval_days"] == 7
    assert P("weekdays")["weekdays"] == [0, 1, 2, 3, 4]
    assert P("weekends")["weekdays"] == [5, 6]
    assert P("mon,thu")["weekdays"] == [0, 3]
    assert P("every tuesday")["weekdays"] == [1]
    assert P("mon/wed/fri")["weekdays"] == [0, 2, 4]
    assert P("monthly on the 1st")["monthdays"] == [1]
    assert P("1st,15th")["monthdays"] == [1, 15]
    assert P("last day of the month")["monthdays"] == [31]
    assert P("monthly")["freq"] == "monthly" and P("monthly")["monthdays"] == []
    for bad in ("garbage", "every 0 days"):
        try:
            P(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad!r} should have failed")


def test_describe_repeat() -> None:
    d = lambda s: m.describe_repeat(m.parse_repeat(s))  # noqa: E731
    assert d("daily") == "every day"
    assert d("every 2 days") == "every 2 days"
    assert d("weekly") == "weekly"
    assert d("mon,thu") == "Mon & Thu"
    assert d("weekdays") == "weekdays"
    assert d("mon/wed/fri") == "Mon, Wed & Fri"
    assert d("1st,15th") == "monthly on the 1st & 15th"
    assert d("last day") == "monthly on the last day"


def test_recurrence_dispatch() -> None:
    tz = ZoneInfo("Europe/Berlin")
    now = dt.datetime(2026, 6, 19, 13, 5, tzinfo=tz).astimezone(UTC)  # Friday
    weekly = {"freq": "weekly", "interval_days": 0, "weekdays": [0, 3],
              "monthdays": [], "time_of_day": "19:00"}
    d1 = m.first_due(weekly, tz, now).astimezone(tz)
    assert d1.weekday() == 0 and (d1.hour, d1.minute) == (19, 0)  # next Monday
    d2 = m.next_due(weekly, tz, m.first_due(weekly, tz, now), now).astimezone(tz)
    assert d2.weekday() == 3  # then Thursday

    monthly = {"freq": "monthly", "interval_days": 0, "weekdays": [],
               "monthdays": [31], "time_of_day": "09:00"}
    jan31 = dt.datetime(2026, 1, 31, 9, 0, tzinfo=tz).astimezone(UTC)
    feb = m.next_due(monthly, tz, jan31, jan31 + dt.timedelta(minutes=1)).astimezone(tz)
    assert (feb.month, feb.day) == (2, 28)  # clamps Feb 31 -> Feb 28

    legacy = {"recurring": True, "interval_days": 2, "time_of_day": "07:30"}
    rule = m.recurrence_of(legacy)
    assert rule["freq"] == "days" and rule["interval_days"] == 2
    nxt = m.first_due(rule, tz, now).astimezone(tz)
    assert (nxt.hour, nxt.minute) == (7, 30)


def test_oneoff_parse() -> None:
    tz = ZoneInfo("Europe/Berlin")
    due = m.parse_oneoff("2026-06-20 14:00", tz)
    assert due == dt.datetime(2026, 6, 20, 14, 0, tzinfo=tz).astimezone(UTC)
    assert m.parse_oneoff("2026-06-20T14:00", tz) == due  # T separator allowed
    for bad in ("2026-13-01 10:00", "not a date", "2026-06-20"):
        try:
            m.parse_oneoff(bad, tz)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad!r} should have failed")


def test_can_undo() -> None:
    from joblin.bot import can_undo

    rec = {"recurring": True, "pending": {"due_at": "2026-06-14T06:00:00+00:00", "message_ids": [1]}}
    # recurring complete/skip: only while no new occurrence has fired, task alive.
    assert can_undo("done", rec, {"recurring": True, "pending": None}) is True
    assert can_undo("skip", rec, {"recurring": True, "pending": {"due_at": "later"}}) is False
    assert can_undo("done", rec, None) is False  # task was deleted -> don't resurrect

    # one-off complete/delete: the task is gone; restore only if the id is free.
    one = {"recurring": False, "pending": {"due_at": "2026-06-14T06:00:00+00:00", "message_ids": [2]}}
    assert can_undo("done", one, None) is True
    assert can_undo("delete", one, {"recurring": False, "pending": None}) is False

    # snooze: the very same occurrence must still be pending (matched by due_at).
    assert can_undo("snooze", rec, {"recurring": True, "pending": {"due_at": "2026-06-14T06:00:00+00:00"}}) is True
    assert can_undo("snooze", rec, {"recurring": True, "pending": {"due_at": "2026-06-15T06:00:00+00:00"}}) is False
    assert can_undo("snooze", rec, {"recurring": True, "pending": None}) is False
    assert can_undo("snooze", rec, None) is False


async def test_void_completion() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / "store.json"
        store = Store(path, pathlib.Path(d) / "log.jsonl")
        store.load()
        assert "undo" in store.data, "store should grow an 'undo' section"
        assert "requeue" in store.data, "store should grow a 'requeue' section"

        await store.log_completion({"id": "aaa", "user_id": 1})
        await store.log_completion({"id": "bbb", "user_id": 2})
        assert await store.void_completion("aaa") is True
        recs = store.read_completions()
        assert len(recs) == 1 and recs[0]["id"] == "bbb", "only the voided record is gone"
        assert await store.void_completion("missing") is False, "voiding an absent id is a no-op"


async def test_store() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / "store.json"
        store = Store(path, pathlib.Path(d) / "log.jsonl")
        store.load()

        async with store.txn() as data:
            data["tasks"]["abc"] = {"brief": "feed", "guild_id": 1}
        assert path.exists(), "store file should be written atomically"

        snap = await store.snapshot()
        snap["tasks"]["abc"]["brief"] = "MUTATED"  # must not affect the store
        again = await store.snapshot()
        assert again["tasks"]["abc"]["brief"] == "feed", "snapshot must be a deep copy"

        # Exceptions inside a txn must NOT be flushed.
        try:
            async with store.txn() as data:
                data["tasks"]["abc"]["brief"] = "half-written"
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        store2 = Store(path, pathlib.Path(d) / "log.jsonl")
        store2.load()
        assert store2.data["tasks"]["abc"]["brief"] == "feed", "rolled-back change leaked to disk"

        await store.log_completion({"user_id": 7, "ts": "2026-06-14T08:00:00+00:00"})
        await store.log_completion({"user_id": 7, "ts": "2026-06-14T09:00:00+00:00"})
        recs = store.read_completions()
        assert len(recs) == 2 and recs[0]["user_id"] == 7


# --- A tiny fake Discord layer so we can drive the real reaction handlers ----
class FakeMessage:
    def __init__(self, mid: int, channel: "FakeChannel") -> None:
        self.id = mid
        self.channel = channel
        self.content = None
        self.view = None
        # The live set of reactions on this message (emoji string -> present),
        # so a test can verify a member's fun reaction survives a close while our
        # functional buttons are stripped.
        self.reactions: set[str] = set()

    async def add_reaction(self, emoji) -> None:
        self.channel.added.append((self.id, str(emoji)))
        self.reactions.add(str(emoji))

    async def remove_reaction(self, emoji, user) -> None:
        self.reactions.discard(str(emoji))

    async def clear_reaction(self, emoji) -> None:
        # Discord's clear_reaction(emoji) sweeps everyone's copy of that one emoji.
        self.channel.cleared_emoji.append((self.id, str(emoji)))
        self.reactions.discard(str(emoji))

    async def clear_reactions(self) -> None:
        self.channel.cleared.append(self.id)
        self.reactions.clear()

    async def edit(self, content=None, **kw) -> None:  # absorbs view=/allowed_mentions=
        self.content = content
        if "view" in kw:
            self.view = kw["view"]

    async def delete(self) -> None:
        self.channel.deleted.append(self.id)


class FakeChannel:
    def __init__(self) -> None:
        self.id = 999
        self.msgs: dict[int, FakeMessage] = {}
        self.added: list = []
        self.deleted: list = []
        self.cleared: list = []
        self.cleared_emoji: list = []
        self.files: list = []  # discord.File objects passed to send(file=...)
        self._next = 1000

    async def send(self, content=None, allowed_mentions=None, **kw) -> FakeMessage:
        self._next += 1
        msg = FakeMessage(self._next, self)
        msg.content = content
        if kw.get("file") is not None:
            self.files.append(kw["file"])
        self.msgs[msg.id] = msg
        return msg

    def get_partial_message(self, mid: int) -> FakeMessage:
        return self.msgs.setdefault(mid, FakeMessage(mid, self))


class FakeMember:
    def __init__(self, uid: int, name: str) -> None:
        self.id = uid
        self.display_name = name
        self.mention = f"<@{uid}>"


class FakePayload:
    def __init__(self, message_id, emoji, *, user_id=42, member=None,
                 guild_id=1, channel_id=999) -> None:
        self.message_id = message_id
        self.emoji = emoji
        self.user_id = user_id
        self.member = member
        self.guild_id = guild_id
        self.channel_id = channel_id


class FakeUser:
    def __init__(self, uid: int, name: str) -> None:
        self.id = uid
        self.display_name = name
        self.mention = f"<@{uid}>"


class FakeResponse:
    """Captures whatever a command / button handler does with the interaction."""

    def __init__(self) -> None:
        self.content = None
        self.embed = None
        self.embeds = None
        self.ephemeral = None
        self.view = None
        self._done = False

    async def send_message(self, content=None, *, ephemeral=False, embed=None,
                           embeds=None, view=None, allowed_mentions=None) -> None:
        self.content, self.ephemeral, self.view, self._done = content, ephemeral, view, True
        self.embeds = embeds
        self.embed = embed if embed is not None else (embeds[0] if embeds else None)

    async def edit_message(self, content=None, *, view=None, allowed_mentions=None) -> None:
        self.content, self.view, self._done = content, view, True

    async def defer(self, *a, **k) -> None:
        self._done = True

    def is_done(self) -> bool:
        return self._done


class FakeInteraction:
    def __init__(self, *, guild_id=1, user=None, channel=None) -> None:
        self.guild_id = guild_id
        self.user = user or FakeUser(1, "Boss")
        self.channel = channel
        self.response = FakeResponse()


async def _game_setup(d):
    """A fresh store wired into the bot module, a fake farm channel, and config."""
    import joblin.bot as bot

    st = Store(pathlib.Path(d) / "store.json", pathlib.Path(d) / "log.jsonl")
    st.load()
    bot.store = st  # handlers read the module global at call time
    ch = FakeChannel()
    bot.bot.get_channel = lambda cid: ch  # type: ignore[assignment]
    async with st.txn() as data:
        data["configs"]["1"] = {
            "channel_id": 999, "timezone": "Europe/Berlin", "reminder_role_id": None,
        }
    return bot, st, ch


async def test_pitchin_lifecycle() -> None:
    """Post a pitch-in, two people ✅, one un-✅s, a non-creator 🏁 is ignored,
    then the creator 🏁 closes it and awards a punto to whoever's still in."""
    with tempfile.TemporaryDirectory() as d:
        bot, st, ch = await _game_setup(d)
        now = m.now_utc()
        pid, msg = await bot.post_pitchin(
            ch, guild_id=1, creator_id=1, brief="Laundry bonanza", description=None,
            expires_at=m.to_iso(now + dt.timedelta(hours=6)), points_each=1,
            max_scorers=None, now=now,
        )
        mid = msg.id

        await bot.on_raw_reaction_add(FakePayload(mid, "✅", user_id=42, member=FakeMember(42, "Pat")))
        await bot.on_raw_reaction_add(FakePayload(mid, "✅", user_id=7, member=FakeMember(7, "Sam")))
        snap = await st.snapshot()
        assert [s["user_id"] for s in snap["pitchins"][pid]["scorers"]] == [42, 7]
        assert "Pitched in (2):" in ch.msgs[mid].content

        # Sam pulls his ✅ back off -> dropped before it closes.
        await bot.on_raw_reaction_remove(FakePayload(mid, "✅", user_id=7))
        snap = await st.snapshot()
        assert [s["user_id"] for s in snap["pitchins"][pid]["scorers"]] == [42]
        assert "Pitched in (1):" in ch.msgs[mid].content

        # A non-creator 🏁 must NOT close it.
        await bot.on_raw_reaction_add(FakePayload(mid, m.EMOJI_END, user_id=42, member=FakeMember(42, "Pat")))
        assert pid in (await st.snapshot())["pitchins"], "only the creator ends a pitch-in"

        # The creator 🏁 closes it: Pat earns 1 punto, the post is finalized.
        await bot.on_raw_reaction_add(FakePayload(mid, m.EMOJI_END, user_id=1, member=FakeMember(1, "Boss")))
        snap = await st.snapshot()
        assert pid not in snap["pitchins"] and str(mid) not in snap["game_messages"]
        recs = st.read_completions()
        assert len(recs) == 1 and recs[0]["user_id"] == 42 and recs[0]["points"] == 1
        assert recs[0]["kind"] == "pitchin"
        # Closing strips our ✅/🏁 buttons (not via the all-nuking clear_reactions).
        assert "pitched in!" in ch.msgs[mid].content
        assert (mid, "✅") in ch.cleared_emoji and (mid, m.EMOJI_END) in ch.cleared_emoji
        assert mid not in ch.cleared


async def test_pitchin_cap_and_points() -> None:
    """max_scorers closes the pitch-in the instant it fills; points_each>1 pays
    each scorer that many puntos."""
    with tempfile.TemporaryDirectory() as d:
        bot, st, ch = await _game_setup(d)
        now = m.now_utc()
        pid, msg = await bot.post_pitchin(
            ch, guild_id=1, creator_id=1, brief="Move the sheep", description=None,
            expires_at=m.to_iso(now + dt.timedelta(hours=6)), points_each=2,
            max_scorers=2, now=now,
        )
        mid = msg.id
        await bot.on_raw_reaction_add(FakePayload(mid, "✅", user_id=42, member=FakeMember(42, "Pat")))
        assert pid in (await st.snapshot())["pitchins"], "still open after 1 of 2"
        await bot.on_raw_reaction_add(FakePayload(mid, "✅", user_id=7, member=FakeMember(7, "Sam")))
        assert pid not in (await st.snapshot())["pitchins"], "fills at 2 -> auto-closes"
        recs = sorted(st.read_completions(), key=lambda r: r["user_id"])
        assert [(r["user_id"], r["points"]) for r in recs] == [(7, 2), (42, 2)]
        # A late ✅ after it closed is a harmless no-op (no longer indexed).
        await bot.on_raw_reaction_add(FakePayload(mid, "✅", user_id=9, member=FakeMember(9, "Lee")))
        assert len(st.read_completions()) == 2


async def test_pitchin_expiry() -> None:
    """An expired pitch-in closes on the next scheduler sweep (restart-safe)."""
    with tempfile.TemporaryDirectory() as d:
        bot, st, ch = await _game_setup(d)
        now = m.now_utc()
        pid, msg = await bot.post_pitchin(
            ch, guild_id=1, creator_id=1, brief="Quick hands", description=None,
            expires_at=m.to_iso(now - dt.timedelta(seconds=1)), points_each=1,
            max_scorers=None, now=now,
        )
        await bot.on_raw_reaction_add(FakePayload(msg.id, "✅", user_id=42, member=FakeMember(42, "Pat")))
        await bot.sweep_games(m.now_utc(), await st.snapshot())
        assert pid not in (await st.snapshot())["pitchins"]
        recs = st.read_completions()
        assert len(recs) == 1 and recs[0]["points"] == 1


async def test_doemup_lifecycle() -> None:
    """Drive the do-em-up buttons: ➕ tallies live, ➖ corrects, a non-creator End
    is refused, the creator End closes it and awards count×puntos, and the puntos
    land on the unified /leaderboard."""
    with tempfile.TemporaryDirectory() as d:
        bot, st, ch = await _game_setup(d)
        now = m.now_utc()
        did, msg = await bot.post_doemup(
            ch, guild_id=1, creator_id=1, brief="Clear the thistle", description=None,
            points_each=1, deadline=None, point_limit=None, now=now,
        )
        mid = msg.id

        async def press(action, uid, name):
            inter = FakeInteraction(user=FakeUser(uid, name), channel=ch)
            await bot.handle_doemup_button(did, action, inter)
            return inter

        last = None
        for _ in range(5):
            last = await press("plus", 42, "Pat")
        assert "Pat ×5" in last.response.content  # live tally rode the interaction
        for _ in range(3):
            await press("plus", 7, "Bo")
        await press("minus", 7, "Bo")  # Bo fixes one -> 2
        snap = await st.snapshot()
        d_ = snap["doemups"][did]
        assert d_["tallies"]["42"]["count"] == 5 and d_["tallies"]["7"]["count"] == 2

        # Non-creator End is refused; the do-em-up stays open.
        inter = await press("end", 42, "Pat")
        assert "Only the person" in inter.response.content
        assert did in (await st.snapshot())["doemups"]

        # Creator End closes it and awards 5 + 2.
        await press("end", 1, "Boss")
        snap = await st.snapshot()
        assert did not in snap["doemups"] and str(mid) not in snap["game_messages"]
        by = {r["user_id"]: r["points"] for r in st.read_completions()}
        assert by == {42: 5, 7: 2}
        assert all(r["kind"] == "doemup" for r in st.read_completions())
        assert "done!" in ch.msgs[mid].content

        # /leaderboard (the upstream puntos + ⭐ stars board) totals those puntos:
        # Pat leads with 5, Bo has 2, footer counts 2 records · 7 puntos.
        month = now.astimezone(ZoneInfo("Europe/Berlin")).strftime("%Y-%m")
        inter = FakeInteraction(user=FakeUser(1, "Boss"))
        await bot.leaderboard.callback(inter, month=month)
        assert "**5 puntos** — <@42>" in inter.response.content
        assert "7 puntos this month" in inter.response.content


async def test_doemup_limit_and_deadline() -> None:
    """point_limit auto-closes at the cap; a past deadline closes on the sweep."""
    with tempfile.TemporaryDirectory() as d:
        bot, st, ch = await _game_setup(d)
        now = m.now_utc()
        # Cap of 3 -> the 3rd ➕ closes it.
        did, msg = await bot.post_doemup(
            ch, guild_id=1, creator_id=1, brief="Pull 3 weeds", description=None,
            points_each=1, deadline=None, point_limit=3, now=now,
        )
        for _ in range(3):
            await bot.handle_doemup_button(
                did, "plus", FakeInteraction(user=FakeUser(42, "Pat"), channel=ch)
            )
        assert did not in (await st.snapshot())["doemups"], "hits the cap -> closes"
        assert {r["user_id"]: r["points"] for r in st.read_completions()} == {42: 3}

        # Deadline already past -> closes on the next sweep, paying points_each.
        did2, msg2 = await bot.post_doemup(
            ch, guild_id=1, creator_id=1, brief="Beat the clock", description=None,
            points_each=2, deadline=m.to_iso(now - dt.timedelta(seconds=1)),
            point_limit=None, now=now,
        )
        await bot.handle_doemup_button(
            did2, "plus", FakeInteraction(user=FakeUser(7, "Bo"), channel=ch)
        )
        await bot.sweep_games(m.now_utc(), await st.snapshot())
        assert did2 not in (await st.snapshot())["doemups"]
        bo = [r for r in st.read_completions() if r["user_id"] == 7]
        assert len(bo) == 1 and bo[0]["points"] == 2  # 1 unit × 2 each


async def test_pitchin_recurring() -> None:
    """A recurring pitch-in awards its round, goes dormant, re-posts a fresh round
    at its next slot, rolls on past the creator's 🏁, and is torn down by
    /deletetask."""
    with tempfile.TemporaryDirectory() as d:
        bot, st, ch = await _game_setup(d)
        tz = ZoneInfo("Europe/Berlin")
        now = m.now_utc()
        rule = {"freq": "days", "interval_days": 1, "weekdays": [], "monthdays": [],
                "time_of_day": now.astimezone(tz).strftime("%H:%M")}
        # window already lapsed -> the next sweep closes round one; no fixed
        # duration, so re-posts run to the next daily slot.
        pid, msg = await bot.post_pitchin(
            ch, guild_id=1, creator_id=1, brief="Daily tidy", description=None,
            expires_at=m.to_iso(now - dt.timedelta(seconds=1)), points_each=1,
            max_scorers=None, now=now, recurrence=rule, duration_secs=None,
        )
        mid = msg.id
        assert (await st.snapshot())["pitchins"][pid]["recurring"] is True

        await bot.on_raw_reaction_add(FakePayload(mid, "✅", member=FakeMember(42, "Pat")))
        await bot.sweep_games(m.now_utc(), await st.snapshot())
        snap = await st.snapshot()
        p = snap["pitchins"][pid]
        assert p["message_id"] is None and p["scorers"] == [], "round closed -> dormant"
        nd = m.from_iso(p["next_due"])
        assert nd > now and nd.astimezone(tz).strftime("%H:%M") == rule["time_of_day"]
        assert str(mid) not in snap["game_messages"], "old post de-registered"
        assert "pitched in!" in ch.msgs[mid].content and "Next round" in ch.msgs[mid].content
        assert len(st.read_completions()) == 1

        # Force the next slot due -> the sweep re-posts a fresh, live round.
        async with st.txn() as data:
            data["pitchins"][pid]["next_due"] = m.to_iso(m.now_utc() - dt.timedelta(seconds=1))
        await bot.sweep_games(m.now_utc(), await st.snapshot())
        snap = await st.snapshot()
        p = snap["pitchins"][pid]
        new_mid = p["message_id"]
        assert new_mid is not None and new_mid != mid, "a brand-new round post"
        assert p["next_due"] is None and m.from_iso(p["expires_at"]) > m.now_utc()
        assert snap["game_messages"][str(new_mid)] == {"kind": "pitchin", "id": pid}
        assert (new_mid, m.EMOJI_DONE) in ch.added, "the fresh round self-reacts ✅"

        # Score the new round; the creator's 🏁 closes THIS round (awarding Sam)
        # and the recurring series rolls on rather than ending.
        await bot.on_raw_reaction_add(
            FakePayload(new_mid, "✅", user_id=7, member=FakeMember(7, "Sam"))
        )
        await bot.on_raw_reaction_add(
            FakePayload(new_mid, m.EMOJI_END, user_id=1, member=FakeMember(1, "Boss"))
        )
        snap = await st.snapshot()
        assert pid in snap["pitchins"], "🏁 closes a round, not the series"
        assert snap["pitchins"][pid]["message_id"] is None, "rolled on -> dormant again"
        assert "Next round" in ch.msgs[new_mid].content
        by = sorted(r["user_id"] for r in st.read_completions())
        assert by == [7, 42], "round one (Pat) and round two (Sam) both scored"

        # /deletetask tears the whole series down (it's dormant -> nothing live).
        inter = FakeInteraction(guild_id=1, user=FakeUser(1, "Boss"))
        await bot.deletetask.callback(inter, pid)
        assert pid not in (await st.snapshot())["pitchins"], "deletetask kills the series"
        assert "Deleted" in inter.response.content


async def test_pitchin_at_deferred() -> None:
    """`/pitchin at:` defers the first round to its slot instead of posting now:
    a recurring one sits dormant until the scheduler opens it (then runs its fixed
    window), and a one-off `at` schedules a single round for later."""
    with tempfile.TemporaryDirectory() as d:
        bot, st, ch = await _game_setup(d)
        tz = ZoneInfo("Europe/Berlin")
        now = m.now_utc()
        slot = (now.astimezone(tz) + dt.timedelta(hours=2)).strftime("%H:%M")

        # Recurring daily with an explicit 5-minute window, deferred to its slot.
        inter = FakeInteraction(guild_id=1, user=FakeUser(1, "Boss"))
        await bot.pitchin.callback(
            inter, brief="Dawn tidy", at=slot, expires="in 5 minutes", repeat="daily"
        )
        snap = await st.snapshot()
        assert len(snap["pitchins"]) == 1
        p = next(iter(snap["pitchins"].values()))
        pid = p["id"]
        assert p["recurring"] and p["freq"] == "days" and p["interval_days"] == 1
        assert p["message_id"] is None and not ch.msgs, "deferred -> nothing posted yet"
        assert p["duration_secs"] == 300, "the 5-minute window each round reuses"
        nd = m.from_iso(p["next_due"])
        assert nd > now and nd.astimezone(tz).strftime("%H:%M") == slot, "fires at its slot"
        assert "🤝 Scheduled" in inter.response.content and "opens" in inter.response.content

        # Force the slot due -> the sweep opens a live round with the stored window.
        async with st.txn() as data:
            data["pitchins"][pid]["next_due"] = m.to_iso(m.now_utc() - dt.timedelta(seconds=1))
        await bot.sweep_games(m.now_utc(), await st.snapshot())
        p = (await st.snapshot())["pitchins"][pid]
        assert p["message_id"] is not None and p["next_due"] is None, "now live"
        assert 290 <= (m.from_iso(p["expires_at"]) - m.now_utc()).total_seconds() <= 305

        # A one-off `at` schedules a single deferred round (default 24h window).
        inter = FakeInteraction(guild_id=1, user=FakeUser(1, "Boss"))
        await bot.pitchin.callback(inter, brief="One and done", at="in 1 hour")
        one = next(q for q in (await st.snapshot())["pitchins"].values()
                   if q["brief"] == "One and done")
        assert not one["recurring"] and one["message_id"] is None
        assert one["next_due"] is not None and one["duration_secs"] == 86400
        assert "🤝 Scheduled" in inter.response.content

        # A past explicit `at` is rejected; nothing new is scheduled.
        inter = FakeInteraction(guild_id=1, user=FakeUser(1, "Boss"))
        await bot.pitchin.callback(inter, brief="Too late", at="2000-01-01 00:00")
        assert "past" in inter.response.content
        assert len((await st.snapshot())["pitchins"]) == 2


async def test_doemup_recurring() -> None:
    """A recurring do-em-up rolls on after its deadline (and re-posts live buttons),
    a point_limit closes just the round, and the creator's End stops the series."""
    with tempfile.TemporaryDirectory() as d:
        bot, st, ch = await _game_setup(d)
        tz = ZoneInfo("Europe/Berlin")
        now = m.now_utc()
        rule = {"freq": "days", "interval_days": 1, "weekdays": [], "monthdays": [],
                "time_of_day": now.astimezone(tz).strftime("%H:%M")}
        did, msg = await bot.post_doemup(
            ch, guild_id=1, creator_id=1, brief="Daily weeds", description=None,
            points_each=1, deadline=m.to_iso(now - dt.timedelta(seconds=1)),
            point_limit=None, now=now, recurrence=rule,
            duration_secs=6 * 3600,  # each round runs a fixed 6h window
        )
        mid = msg.id

        async def press(action, uid, name):
            await bot.handle_doemup_button(
                did, action, FakeInteraction(user=FakeUser(uid, name), channel=ch)
            )

        await press("plus", 42, "Pat")
        await press("plus", 42, "Pat")
        await bot.sweep_games(m.now_utc(), await st.snapshot())
        snap = await st.snapshot()
        dd = snap["doemups"][did]
        assert dd["message_id"] is None and dd["tallies"] == {}, "round closed -> dormant"
        assert dd["next_due"] is not None and ch.msgs[mid].view is None
        assert "done!" in ch.msgs[mid].content and "Next round" in ch.msgs[mid].content
        assert {r["user_id"]: r["points"] for r in st.read_completions()} == {42: 2}

        # Force the next slot due -> re-post a fresh round with a fixed 6h window.
        async with st.txn() as data:
            data["doemups"][did]["next_due"] = m.to_iso(m.now_utc() - dt.timedelta(seconds=1))
        await bot.sweep_games(m.now_utc(), await st.snapshot())
        snap = await st.snapshot()
        dd = snap["doemups"][did]
        new_mid = dd["message_id"]
        assert new_mid is not None and new_mid != mid
        win = (m.from_iso(dd["deadline"]) - m.now_utc()).total_seconds()
        assert 5.9 * 3600 < win < 6.1 * 3600, "fixed-duration window honored on re-post"
        assert dd["next_due"] is None
        assert snap["game_messages"][str(new_mid)] == {"kind": "doemup", "id": did}

        # End closes THIS round (awarding Bo); the recurring series rolls on.
        await press("plus", 7, "Bo")
        await press("end", 1, "Boss")
        snap = await st.snapshot()
        assert did in snap["doemups"], "End closes a round, not the series"
        assert snap["doemups"][did]["message_id"] is None, "rolled on -> dormant"
        by = {r["user_id"]: r["points"] for r in st.read_completions()}
        assert by == {42: 2, 7: 1}

        # /deletetask tears the whole series down.
        inter = FakeInteraction(guild_id=1, user=FakeUser(1, "Boss"))
        await bot.deletetask.callback(inter, did)
        assert did not in (await st.snapshot())["doemups"]
        assert "Deleted" in inter.response.content


async def test_doemup_at_deferred() -> None:
    """`/doemup at:` defers the first round to its slot: a recurring one sits
    dormant until the scheduler opens it, and a one-off `at` with no deadline opens
    at its time and stays open until 🏁 (exercising the deferred-no-window path)."""
    with tempfile.TemporaryDirectory() as d:
        bot, st, ch = await _game_setup(d)
        tz = ZoneInfo("Europe/Berlin")
        now = m.now_utc()
        slot = (now.astimezone(tz) + dt.timedelta(hours=2)).strftime("%H:%M")

        # Recurring daily with a 90-minute window, deferred to its slot.
        inter = FakeInteraction(guild_id=1, user=FakeUser(1, "Boss"))
        await bot.doemup.callback(
            inter, brief="Dawn reps", at=slot, deadline="in 90 minutes", repeat="daily"
        )
        snap = await st.snapshot()
        rec = next(iter(snap["doemups"].values()))
        rid = rec["id"]
        assert rec["recurring"] and rec["message_id"] is None and not ch.msgs
        assert rec["duration_secs"] == 5400 and rec["deadline"] is None
        nd = m.from_iso(rec["next_due"])
        assert nd > now and nd.astimezone(tz).strftime("%H:%M") == slot
        assert "💪 Scheduled" in inter.response.content and "opens" in inter.response.content

        # Force the slot due -> the sweep opens a live round with the stored window.
        async with st.txn() as data:
            data["doemups"][rid]["next_due"] = m.to_iso(m.now_utc() - dt.timedelta(seconds=1))
        await bot.sweep_games(m.now_utc(), await st.snapshot())
        rec = (await st.snapshot())["doemups"][rid]
        assert rec["message_id"] is not None and rec["next_due"] is None
        assert 5390 <= (m.from_iso(rec["deadline"]) - m.now_utc()).total_seconds() <= 5405

        # A one-off `at` with no deadline: deferred, then open until 🏁 (no auto-close).
        inter = FakeInteraction(guild_id=1, user=FakeUser(1, "Boss"))
        await bot.doemup.callback(inter, brief="Later, open-ended", at="in 1 hour")
        one = next(q for q in (await st.snapshot())["doemups"].values()
                   if q["brief"] == "Later, open-ended")
        assert not one["recurring"] and one["message_id"] is None
        assert one["duration_secs"] is None, "no window -> runs until 🏁 when it opens"
        async with st.txn() as data:
            data["doemups"][one["id"]]["next_due"] = m.to_iso(m.now_utc() - dt.timedelta(seconds=1))
        await bot.sweep_games(m.now_utc(), await st.snapshot())
        one = (await st.snapshot())["doemups"][one["id"]]
        assert one["message_id"] is not None and one["deadline"] is None, "open until 🏁"

        # A past explicit `at` is rejected.
        inter = FakeInteraction(guild_id=1, user=FakeUser(1, "Boss"))
        await bot.doemup.callback(inter, brief="Too late", at="2000-01-01 00:00")
        assert "past" in inter.response.content


async def test_doemup_recurring_limit_rolls_on() -> None:
    """Hitting a recurring do-em-up's point_limit closes only that round; the
    series rolls on to its next slot rather than ending."""
    with tempfile.TemporaryDirectory() as d:
        bot, st, ch = await _game_setup(d)
        tz = ZoneInfo("Europe/Berlin")
        now = m.now_utc()
        rule = {"freq": "days", "interval_days": 1, "weekdays": [], "monthdays": [],
                "time_of_day": now.astimezone(tz).strftime("%H:%M")}
        did, msg = await bot.post_doemup(
            ch, guild_id=1, creator_id=1, brief="Three weeds", description=None,
            points_each=1, deadline=m.to_iso(now + dt.timedelta(hours=6)),
            point_limit=3, now=now, recurrence=rule, duration_secs=6 * 3600,
        )
        for _ in range(3):  # the 3rd ➕ hits the cap
            await bot.handle_doemup_button(
                did, "plus", FakeInteraction(user=FakeUser(42, "Pat"), channel=ch)
            )
        snap = await st.snapshot()
        assert did in snap["doemups"], "a capped round does NOT end a recurring series"
        assert snap["doemups"][did]["next_due"] is not None, "it rolls to the next slot"
        assert {r["user_id"]: r["points"] for r in st.read_completions()} == {42: 3}


async def test_delete_live_game() -> None:
    """/deletetask cancels a live game outright — removed from the store and its
    post struck through as cancelled, awarding nobody (delete ≠ close)."""
    with tempfile.TemporaryDirectory() as d:
        bot, st, ch = await _game_setup(d)
        now = m.now_utc()

        # A live pitch-in with a scorer -> deletetask cancels it, no points.
        pid, msg = await bot.post_pitchin(
            ch, guild_id=1, creator_id=1, brief="Laundry", description=None,
            expires_at=m.to_iso(now + dt.timedelta(hours=6)), points_each=1,
            max_scorers=None, now=now,
        )
        await bot.on_raw_reaction_add(FakePayload(msg.id, "✅", user_id=42, member=FakeMember(42, "Pat")))
        inter = FakeInteraction(guild_id=1, user=FakeUser(1, "Boss"))
        await bot.deletetask.callback(inter, pid)
        snap = await st.snapshot()
        assert pid not in snap["pitchins"] and str(msg.id) not in snap["game_messages"]
        assert "cancelled" in ch.msgs[msg.id].content and msg.id in ch.cleared
        assert st.read_completions() == [], "delete awards nobody"

        # A live do-em-up -> its buttons are dropped (view=None) on cancel.
        did, dmsg = await bot.post_doemup(
            ch, guild_id=1, creator_id=1, brief="Weeds", description=None,
            points_each=1, deadline=None, point_limit=None, now=now,
        )
        await bot.handle_doemup_button(did, "plus", FakeInteraction(user=FakeUser(7, "Bo"), channel=ch))
        inter = FakeInteraction(guild_id=1, user=FakeUser(1, "Boss"))
        await bot.deletetask.callback(inter, did)
        snap = await st.snapshot()
        assert did not in snap["doemups"] and str(dmsg.id) not in snap["game_messages"]
        assert "cancelled" in ch.msgs[dmsg.id].content and ch.msgs[dmsg.id].view is None
        assert st.read_completions() == []

        # Deleting something that doesn't exist is a clean miss.
        inter = FakeInteraction(guild_id=1, user=FakeUser(1, "Boss"))
        await bot.deletetask.callback(inter, "nope-nope")
        assert "Not found" in inter.response.content


async def test_game_commands_recurring() -> None:
    """The /pitchin and /doemup `repeat` option records a recurring game that's
    live now (next_due only fills once it goes dormant)."""
    with tempfile.TemporaryDirectory() as d:
        bot, st, ch = await _game_setup(d)

        inter = FakeInteraction(guild_id=1, user=FakeUser(1, "Boss"))
        await bot.pitchin.callback(inter, brief="Morning chores", repeat="daily")
        p = next(iter((await st.snapshot())["pitchins"].values()))
        assert p["recurring"] and p["freq"] == "days" and p["interval_days"] == 1
        assert p["next_due"] is None and m.from_iso(p["expires_at"]) > m.now_utc()
        assert "🔁" in inter.response.content and "every day" in inter.response.content

        inter = FakeInteraction(guild_id=1, user=FakeUser(1, "Boss"))
        await bot.doemup.callback(inter, brief="Weekday reps", repeat="weekdays")
        dd = next(iter((await st.snapshot())["doemups"].values()))
        assert dd["recurring"] and dd["freq"] == "weekly" and dd["weekdays"] == [0, 1, 2, 3, 4]
        assert dd["next_due"] is None and dd["deadline"] is not None
        assert "🔁" in inter.response.content

        # Junk repeat is rejected before anything is posted.
        inter = FakeInteraction(guild_id=1, user=FakeUser(1, "Boss"))
        await bot.pitchin.callback(inter, brief="Nope", repeat="garblarg")
        assert "repeat" in inter.response.content.lower()


async def test_game_commands() -> None:
    """The /pitchin and /doemup command callbacks: config gate, time parsing,
    past-time rejection, and posting into the configured channel."""
    with tempfile.TemporaryDirectory() as d:
        bot, st, ch = await _game_setup(d)

        # No config for guild 2 -> refuses and posts nothing.
        inter = FakeInteraction(guild_id=2, user=FakeUser(1, "Boss"))
        await bot.pitchin.callback(inter, brief="Nope")
        assert "joblinconfig" in inter.response.content
        assert not (await st.snapshot())["pitchins"]

        # Happy path: default 24h expiry; posts and self-reacts ✅ + 🏁.
        inter = FakeInteraction(guild_id=1, user=FakeUser(1, "Boss"))
        await bot.pitchin.callback(inter, brief="Laundry bonanza")
        snap = await st.snapshot()
        assert len(snap["pitchins"]) == 1
        p = next(iter(snap["pitchins"].values()))
        assert p["points_each"] == 1 and p["max_scorers"] is None
        assert "🤝 Posted" in inter.response.content and inter.response.ephemeral
        mid = p["message_id"]
        assert (mid, m.EMOJI_DONE) in ch.added and (mid, m.EMOJI_END) in ch.added

        # A past expiry is rejected; nothing new is posted.
        inter = FakeInteraction(guild_id=1, user=FakeUser(1, "Boss"))
        await bot.pitchin.callback(inter, brief="Too late", expires="2000-01-01 00:00")
        assert "past" in inter.response.content
        assert len((await st.snapshot())["pitchins"]) == 1

        # /doemup happy path with a deadline + cap -> posts with live buttons.
        inter = FakeInteraction(guild_id=1, user=FakeUser(1, "Boss"))
        await bot.doemup.callback(
            inter, brief="Thistle bush removed", deadline="in 3h", point_limit=50
        )
        snap = await st.snapshot()
        assert len(snap["doemups"]) == 1
        dd = next(iter(snap["doemups"].values()))
        assert dd["point_limit"] == 50 and dd["deadline"] is not None
        assert "💪 Posted" in inter.response.content

        # A past deadline is rejected.
        inter = FakeInteraction(guild_id=1, user=FakeUser(1, "Boss"))
        await bot.doemup.callback(inter, brief="Nope", deadline="2000-01-01 00:00")
        assert "past" in inter.response.content
        assert len((await st.snapshot())["doemups"]) == 1


async def test_lifecycle_and_snooze() -> None:
    """Drive the real reaction handlers end-to-end against the fake channel:
    fire a weekly task, ✅-complete it (must roll to the next weekday), then
    ⏩-open a snooze panel and pick '2 days' from it."""
    import joblin.bot as bot

    with tempfile.TemporaryDirectory() as d:
        st = Store(pathlib.Path(d) / "store.json", pathlib.Path(d) / "log.jsonl")
        st.load()
        bot.store = st  # handlers read the module global at call time
        ch = FakeChannel()
        bot.bot.get_channel = lambda cid: ch  # type: ignore[assignment]

        tz = ZoneInfo("Europe/Berlin")
        now = m.now_utc()
        cfg = {"channel_id": 999, "timezone": "Europe/Berlin", "reminder_role_id": None}
        tid = "task1"
        task = {
            "id": tid, "guild_id": 1, "brief": "Trash out", "description": None,
            "recurring": True, "freq": "weekly", "interval_days": 0,
            "weekdays": [0, 3], "monthdays": [],
            "time_of_day": now.astimezone(tz).strftime("%H:%M"),
            "next_due": m.to_iso(now - dt.timedelta(seconds=1)),  # already due -> fires
            "created_by": 1, "created_at": m.to_iso(now), "pending": None,
        }
        async with st.txn() as data:
            data["configs"]["1"] = cfg
            data["tasks"][tid] = task

        # 1) Fire it.
        await bot.fire_task(tid, ch, cfg)
        snap = await st.snapshot()
        assert snap["tasks"][tid]["pending"], "task should be pending after firing"
        posted = snap["tasks"][tid]["pending"]["message_ids"][-1]

        # 2) ✅ complete -> recurring weekly rolls to the next Mon/Thu, logs once.
        await bot.on_raw_reaction_add(
            FakePayload(posted, "✅", member=FakeMember(42, "Pat"))
        )
        snap = await st.snapshot()
        t = snap["tasks"][tid]
        assert t["pending"] is None, "completed occurrence should clear pending"
        nd = m.from_iso(t["next_due"])
        assert nd > now and nd.astimezone(tz).weekday() in (0, 3), "rolled to next weekday"
        completions = st.read_completions()
        assert len(completions) == 1 and completions[0]["user_id"] == 42

        # 3) Re-arm a pending occurrence and open a snooze panel via ⏩.
        async with st.txn() as data:
            data["tasks"][tid]["pending"] = None
            data["tasks"][tid]["next_due"] = m.to_iso(now - dt.timedelta(seconds=1))
        await bot.fire_task(tid, ch, cfg)
        snap = await st.snapshot()
        anchor = snap["tasks"][tid]["pending"]["message_ids"][-1]

        await bot.on_raw_reaction_add(
            FakePayload(anchor, "⏩", member=FakeMember(42, "Pat"))
        )
        snap = await st.snapshot()
        assert len(snap["snooze_panels"]) == 1, "⏩ should open exactly one panel"
        panel_id = int(next(iter(snap["snooze_panels"])))

        # Toggle the unit to days, then pick 2 -> snooze 2 days.
        await bot.on_raw_reaction_add(
            FakePayload(panel_id, m.EMOJI_SNOOZE_DAYS, member=FakeMember(42, "Pat"))
        )
        snap = await st.snapshot()
        assert snap["snooze_panels"][str(panel_id)]["unit"] == "days"

        await bot.on_raw_reaction_add(
            FakePayload(panel_id, m.DIGIT_EMOJI[2], member=FakeMember(42, "Pat"))
        )
        snap = await st.snapshot()
        assert str(panel_id) not in snap["snooze_panels"], "panel consumed after a pick"
        assert panel_id in ch.deleted, "panel message deleted"
        remind = m.from_iso(snap["tasks"][tid]["pending"]["remind_at"])
        assert (remind - m.now_utc()).total_seconds() > 1.9 * 86400, "~2 days out"
        assert str(anchor) in snap["undo"], "snooze armed an undo on the task post"
        assert "Snoozed 2 days" in (ch.msgs[anchor].content or "")


async def test_legacy_migration() -> None:
    """A store written by the OLD schema — tasks with no freq/weekdays/monthdays,
    and no top-level snooze_panels key — must load and run without a hiccup, and
    upgrade in place when edited. This guards the family's existing data."""
    import joblin.bot as bot

    tzname = "Europe/Berlin"
    tz = ZoneInfo(tzname)
    with tempfile.TemporaryDirectory() as d:
        now = m.now_utc()
        past = m.to_iso(now - dt.timedelta(minutes=5))
        future = m.to_iso(now + dt.timedelta(hours=3))
        legacy = {
            "configs": {"1": {"channel_id": 999, "timezone": tzname, "reminder_role_id": None}},
            "tasks": {
                # daily, due in the past -> will fire
                "leg1": {"id": "leg1", "guild_id": 1, "brief": "Put animals out",
                         "description": None, "recurring": True, "interval_days": 1,
                         "time_of_day": "08:00", "next_due": past,
                         "created_by": 1, "created_at": past, "pending": None},
                # every-2-days, not yet due
                "leg2": {"id": "leg2", "guild_id": 1, "brief": "Refill water",
                         "description": "Blue hose", "recurring": True, "interval_days": 2,
                         "time_of_day": "07:30", "next_due": future,
                         "created_by": 1, "created_at": past, "pending": None},
                # one-off
                "leg3": {"id": "leg3", "guild_id": 1, "brief": "Vet visit",
                         "description": None, "recurring": False, "interval_days": 0,
                         "time_of_day": None, "next_due": future,
                         "created_by": 1, "created_at": past, "pending": None},
                # ALREADY pending at the moment of upgrade (bot restarted mid-occurrence)
                "leg4": {"id": "leg4", "guild_id": 1, "brief": "Lock the coop",
                         "description": None, "recurring": True, "interval_days": 1,
                         "time_of_day": "21:00", "next_due": None,
                         "created_by": 1, "created_at": past,
                         "pending": {"due_at": past, "remind_at": past, "ffwd_count": 0,
                                     "channel_id": 999, "message_ids": [5005]}},
            },
            "messages": {"5005": "leg4"},
            "undo": {},
            # deliberately NO "snooze_panels" key — an old on-disk store
        }
        path = pathlib.Path(d) / "store.json"
        path.write_text(json.dumps(legacy), encoding="utf-8")
        st = Store(path, pathlib.Path(d) / "log.jsonl")
        st.load()
        bot.store = st
        ch = FakeChannel()
        bot.bot.get_channel = lambda cid: ch  # type: ignore[assignment]

        # load() backfills the new top-level sections the old file lacked.
        assert "snooze_panels" in st.data
        assert "requeue" in st.data

        # recurrence_of reads the legacy fields as the equivalent rule.
        assert m.recurrence_of(legacy["tasks"]["leg1"])["freq"] == "days"
        assert m.recurrence_of(legacy["tasks"]["leg2"])["interval_days"] == 2
        assert m.recurrence_of(legacy["tasks"]["leg3"])["freq"] == "once"

        # schedule_label / listtasks render every legacy task without KeyError.
        for tid in ("leg1", "leg2", "leg3", "leg4"):
            bot.schedule_label((await st.snapshot())["tasks"][tid])

        # Fire + ✅ a legacy DAILY task -> rolls forward to 08:00, logs once.
        await bot.fire_task("leg1", ch, legacy["configs"]["1"])
        posted = (await st.snapshot())["tasks"]["leg1"]["pending"]["message_ids"][-1]
        await bot.on_raw_reaction_add(FakePayload(posted, "✅", member=FakeMember(1, "Sam")))
        snap = await st.snapshot()
        assert snap["tasks"]["leg1"]["pending"] is None
        nd = m.from_iso(snap["tasks"]["leg1"]["next_due"]).astimezone(tz)
        assert (nd.hour, nd.minute) == (8, 0)
        assert len(st.read_completions()) == 1

        # A legacy occurrence already pending at restart still takes reactions,
        # and the NEW snooze panel works on it.
        await bot.on_raw_reaction_add(FakePayload(5005, "⏩", member=FakeMember(2, "Lee")))
        snap = await st.snapshot()
        assert len(snap["snooze_panels"]) == 1
        panel_id = int(next(iter(snap["snooze_panels"])))
        await bot.on_raw_reaction_add(FakePayload(panel_id, m.DIGIT_EMOJI[3], member=FakeMember(2, "Lee")))
        snap = await st.snapshot()
        assert m.from_iso(snap["tasks"]["leg4"]["pending"]["remind_at"]) > now

        # Editing a legacy task upgrades it in place, keeping its existing time.
        class _Resp:
            def __init__(s): s.msg = None
            async def send_message(s, content=None, ephemeral=False, embed=None, allowed_mentions=None):
                s.msg = content
            def is_done(s): return s.msg is not None

        class _Inter:
            guild_id = 1

            class user:  # noqa: N801
                id = 9

            def __init__(s): s.response = _Resp()

        await bot.edit_task.callback(_Inter(), "leg2", repeat="mon,thu")
        t = (await st.snapshot())["tasks"]["leg2"]
        assert t["freq"] == "weekly" and t["weekdays"] == [0, 3] and t["time_of_day"] == "07:30"


async def test_requeue() -> None:
    """A ✅-completed post grows a 🔄 button; tapping it re-fires the chore right
    now as a fresh occurrence, rolls on normally when finished, and declines
    while another occurrence is already live."""
    import joblin.bot as bot

    with tempfile.TemporaryDirectory() as d:
        st = Store(pathlib.Path(d) / "store.json", pathlib.Path(d) / "log.jsonl")
        st.load()
        bot.store = st  # handlers read the module global at call time
        ch = FakeChannel()
        bot.bot.get_channel = lambda cid: ch  # type: ignore[assignment]

        tz = ZoneInfo("Europe/Berlin")
        now = m.now_utc()
        tod = now.astimezone(tz).strftime("%H:%M")
        cfg = {"channel_id": 999, "timezone": "Europe/Berlin", "reminder_role_id": None}
        tid = "water"
        task = {
            "id": tid, "guild_id": 1, "brief": "Water the animals", "description": None,
            "recurring": True, "freq": "days", "interval_days": 1, "weekdays": [],
            "monthdays": [], "time_of_day": tod,
            "next_due": m.to_iso(now - dt.timedelta(seconds=1)),  # already due -> fires
            "created_by": 1, "created_at": m.to_iso(now), "pending": None,
        }
        async with st.txn() as data:
            data["configs"]["1"] = cfg
            data["tasks"][tid] = task

        # Fire + ✅ complete -> the completed post should carry a 🔄.
        await bot.fire_task(tid, ch, cfg)
        posted = (await st.snapshot())["tasks"][tid]["pending"]["message_ids"][-1]
        await bot.on_raw_reaction_add(FakePayload(posted, "✅", member=FakeMember(42, "Pat")))
        snap = await st.snapshot()
        assert snap["tasks"][tid]["pending"] is None
        assert str(posted) in snap["requeue"], "completing arms a requeue on the post"
        assert (posted, "🔄") in ch.added, "🔄 button added to the completed post"
        assert len(st.read_completions()) == 1

        # 🔄 re-fires it now as a fresh occurrence; the spent record is dropped.
        await bot.on_raw_reaction_add(FakePayload(posted, "🔄", member=FakeMember(7, "Sam")))
        snap = await st.snapshot()
        assert str(posted) not in snap["requeue"], "the spent requeue record is dropped"
        p = snap["tasks"][tid]["pending"]
        assert p is not None, "requeue fires a fresh, live occurrence"
        fresh = p["message_ids"][-1]
        assert fresh != posted, "a brand-new post, not the completed one"

        # Finishing the re-run rolls the daily recurrence on to tomorrow's slot
        # (re-pinned to its time-of-day, so no drift) and counts again.
        await bot.on_raw_reaction_add(FakePayload(fresh, "✅", member=FakeMember(7, "Sam")))
        snap = await st.snapshot()
        assert snap["tasks"][tid]["pending"] is None
        nd = m.from_iso(snap["tasks"][tid]["next_due"]).astimezone(tz)
        assert nd.strftime("%H:%M") == tod and m.from_iso(snap["tasks"][tid]["next_due"]) > now
        assert len(st.read_completions()) == 2, "the re-run also counts on the leaderboard"

        # With another occurrence live, 🔄 on the re-run's completed post declines
        # rather than double-firing.
        fresh_post = int(next(iter(snap["requeue"])))  # armed on the re-run's post
        async with st.txn() as data:  # simulate the next cycle having fired
            data["tasks"][tid]["next_due"] = m.to_iso(now - dt.timedelta(seconds=1))
        await bot.fire_task(tid, ch, cfg)
        live_mid = (await st.snapshot())["tasks"][tid]["pending"]["message_ids"][-1]
        await bot.on_raw_reaction_add(FakePayload(fresh_post, "🔄", member=FakeMember(7, "Sam")))
        snap = await st.snapshot()
        assert snap["tasks"][tid]["pending"]["message_ids"][-1] == live_mid, "no double-fire while busy"
        assert str(fresh_post) in snap["requeue"], "the button stays for later"
        assert any("already queued" in (msg.content or "") for msg in ch.msgs.values())


async def test_claps() -> None:
    """A ✅-completed post grows a 👏 button; an outsider's tap tips the doer a
    bonus punto (one per outsider), a participant's own 👏 is ignored, and undoing
    the ✅ retracts both the completion and every clap bonus."""
    import joblin.bot as bot

    with tempfile.TemporaryDirectory() as d:
        st = Store(pathlib.Path(d) / "store.json", pathlib.Path(d) / "log.jsonl")
        st.load()
        assert "claps" in st.data, "store should grow a 'claps' section"
        bot.store = st  # handlers read the module global at call time
        ch = FakeChannel()
        bot.bot.get_channel = lambda cid: ch  # type: ignore[assignment]

        tz = ZoneInfo("Europe/Berlin")
        now = m.now_utc()
        tod = now.astimezone(tz).strftime("%H:%M")
        cfg = {"channel_id": 999, "timezone": "Europe/Berlin", "reminder_role_id": None}
        tid = "sweep"
        task = {
            "id": tid, "guild_id": 1, "brief": "Sweep the barn", "description": None,
            "recurring": True, "freq": "days", "interval_days": 1, "weekdays": [],
            "monthdays": [], "time_of_day": tod,
            "next_due": m.to_iso(now - dt.timedelta(seconds=1)),  # already due -> fires
            "created_by": 1, "created_at": m.to_iso(now), "pending": None,
        }
        async with st.txn() as data:
            data["configs"]["1"] = cfg
            data["tasks"][tid] = task

        # Fire + ✅ complete by Pat -> the completed post carries a 👏.
        await bot.fire_task(tid, ch, cfg)
        posted = (await st.snapshot())["tasks"][tid]["pending"]["message_ids"][-1]
        await bot.on_raw_reaction_add(FakePayload(posted, "✅", member=FakeMember(42, "Pat")))
        snap = await st.snapshot()
        assert str(posted) in snap["claps"], "completing arms a clap on the post"
        assert (posted, "👏") in ch.added, "👏 button added to the completed post"
        assert [p["user_id"] for p in snap["claps"][str(posted)]["participants"]] == [42]
        assert len(st.read_completions()) == 1, "just the completion so far"

        # An outsider (Sam) claps -> Pat earns a +1 bonus; the tally shows on the post.
        await bot.on_raw_reaction_add(FakePayload(posted, "👏", user_id=7, member=FakeMember(7, "Sam")))
        recs = st.read_completions()
        claps = [r for r in recs if r["kind"] == "clap"]
        assert len(claps) == 1 and claps[0]["user_id"] == 42 and claps[0]["points"] == 1
        assert "👏 ×1" in (ch.msgs[posted].content or "")

        # Sam clapping again is a no-op (one per outsider).
        await bot.on_raw_reaction_add(FakePayload(posted, "👏", user_id=7, member=FakeMember(7, "Sam")))
        assert len([r for r in st.read_completions() if r["kind"] == "clap"]) == 1

        # Pat can't clap their own finished chore.
        await bot.on_raw_reaction_add(FakePayload(posted, "👏", user_id=42, member=FakeMember(42, "Pat")))
        assert len([r for r in st.read_completions() if r["kind"] == "clap"]) == 1
        assert 42 not in (await st.snapshot())["claps"][str(posted)]["clappers"]

        # A second outsider (Lee) stacks another bonus -> Pat now has the chore + 2 claps.
        await bot.on_raw_reaction_add(FakePayload(posted, "👏", user_id=9, member=FakeMember(9, "Lee")))
        assert "👏 ×2" in (ch.msgs[posted].content or "")
        by = {}
        for r in st.read_completions():
            by[r["user_id"]] = by.get(r["user_id"], 0) + r.get("points", 1)
        assert by == {42: 3}, "1 for the chore + 2 clapped bonuses"

        # /leaderboard totals the bonus into Pat's score and shows the claps received.
        month = now.astimezone(tz).strftime("%Y-%m")
        inter = FakeInteraction(user=FakeUser(1, "Boss"))
        await bot.leaderboard.callback(inter, month=month)
        assert "**3 puntos** — <@42> · 👏×2" in inter.response.content

        # Undoing the ✅ retracts the completion AND every clap bonus.
        await bot.on_raw_reaction_add(FakePayload(posted, "↩️", member=FakeMember(42, "Pat")))
        snap = await st.snapshot()
        assert str(posted) not in snap["claps"], "undo drops the clap record"
        assert snap["tasks"][tid]["pending"] is not None, "the occurrence is live again"
        assert st.read_completions() == [], "completion + both clap bonuses are gone"


async def test_game_claps() -> None:
    """A closed pitch-in / do-em-up round grows a 👏 button; an outsider's tap tips
    *every* scorer a bonus punto (one clap per outsider), and a scorer's own 👏 is
    ignored."""
    with tempfile.TemporaryDirectory() as d:
        bot, st, ch = await _game_setup(d)
        now = m.now_utc()

        # --- Pitch-in: Pat + Sam pitch in, the creator 🏁 closes the round. ---
        pid, msg = await bot.post_pitchin(
            ch, guild_id=1, creator_id=1, brief="Laundry bonanza", description=None,
            expires_at=m.to_iso(now + dt.timedelta(hours=6)), points_each=1,
            max_scorers=None, now=now,
        )
        mid = msg.id
        await bot.on_raw_reaction_add(FakePayload(mid, "✅", user_id=42, member=FakeMember(42, "Pat")))
        await bot.on_raw_reaction_add(FakePayload(mid, "✅", user_id=7, member=FakeMember(7, "Sam")))
        await bot.on_raw_reaction_add(FakePayload(mid, m.EMOJI_END, user_id=1, member=FakeMember(1, "Boss")))
        snap = await st.snapshot()
        assert pid not in snap["pitchins"], "creator 🏁 closed the round"
        assert str(mid) in snap["claps"], "the finished round armed a clap"
        assert {p["user_id"] for p in snap["claps"][str(mid)]["participants"]} == {42, 7}
        assert (mid, "👏") in ch.added
        assert len(st.read_completions()) == 2, "Pat + Sam each scored 1"

        # An outsider (Lee) claps -> +1 to BOTH scorers; the tally shows "each".
        await bot.on_raw_reaction_add(FakePayload(mid, "👏", user_id=9, member=FakeMember(9, "Lee")))
        claps = [r for r in st.read_completions() if r["kind"] == "clap"]
        assert sorted(r["user_id"] for r in claps) == [7, 42], "one clap tips every scorer"
        assert "👏 ×1" in (ch.msgs[mid].content or "") and "each" in (ch.msgs[mid].content or "")

        # Lee again is a no-op; a scorer (Pat) clapping their own round is ignored.
        await bot.on_raw_reaction_add(FakePayload(mid, "👏", user_id=9, member=FakeMember(9, "Lee")))
        await bot.on_raw_reaction_add(FakePayload(mid, "👏", user_id=42, member=FakeMember(42, "Pat")))
        assert len([r for r in st.read_completions() if r["kind"] == "clap"]) == 2
        by = {}
        for r in st.read_completions():
            by[r["user_id"]] = by.get(r["user_id"], 0) + r.get("points", 1)
        assert by == {42: 2, 7: 2}, "each scored 1 + was clapped 1"

        # --- Do-em-up: Pat tallies 3, Bo tallies 2, the creator ends it. ---
        did, dmsg = await bot.post_doemup(
            ch, guild_id=1, creator_id=1, brief="Clear the thistle", description=None,
            points_each=1, deadline=None, point_limit=None, now=now,
        )
        dmid = dmsg.id

        async def press(action, uid, name):
            await bot.handle_doemup_button(
                did, action, FakeInteraction(user=FakeUser(uid, name), channel=ch)
            )

        for _ in range(3):
            await press("plus", 42, "Pat")
        for _ in range(2):
            await press("plus", 7, "Bo")
        await press("end", 1, "Boss")
        snap = await st.snapshot()
        assert did not in snap["doemups"] and str(dmid) in snap["claps"], "closed round armed a clap"
        assert {p["user_id"] for p in snap["claps"][str(dmid)]["participants"]} == {42, 7}
        assert (dmid, "👏") in ch.added

        # An outsider claps the closed do-em-up -> +1 to each tallier (once).
        await bot.on_raw_reaction_add(FakePayload(dmid, "👏", user_id=9, member=FakeMember(9, "Lee")))
        dclaps = [r for r in st.read_completions() if r["kind"] == "clap" and r["task_id"] == did]
        assert sorted(r["user_id"] for r in dclaps) == [7, 42], "both talliers tipped once"


async def test_requeue_oneoff() -> None:
    """A completed one-off is gone from the store; 🔄 rebuilds it from the saved
    snapshot and re-fires it."""
    import joblin.bot as bot

    with tempfile.TemporaryDirectory() as d:
        st = Store(pathlib.Path(d) / "store.json", pathlib.Path(d) / "log.jsonl")
        st.load()
        bot.store = st
        ch = FakeChannel()
        bot.bot.get_channel = lambda cid: ch  # type: ignore[assignment]

        now = m.now_utc()
        cfg = {"channel_id": 999, "timezone": "Europe/Berlin", "reminder_role_id": None}
        tid = "vet"
        task = {
            "id": tid, "guild_id": 1, "brief": "Move the sheep", "description": None,
            "recurring": False, "freq": "once", "interval_days": 0, "weekdays": [],
            "monthdays": [], "time_of_day": None,
            "next_due": m.to_iso(now - dt.timedelta(seconds=1)),
            "created_by": 1, "created_at": m.to_iso(now), "pending": None,
        }
        async with st.txn() as data:
            data["configs"]["1"] = cfg
            data["tasks"][tid] = task

        await bot.fire_task(tid, ch, cfg)
        posted = (await st.snapshot())["tasks"][tid]["pending"]["message_ids"][-1]
        await bot.on_raw_reaction_add(FakePayload(posted, "✅", member=FakeMember(42, "Pat")))
        snap = await st.snapshot()
        assert tid not in snap["tasks"], "a completed one-off is removed"
        assert str(posted) in snap["requeue"], "but it still offers a requeue"

        await bot.on_raw_reaction_add(FakePayload(posted, "🔄", member=FakeMember(42, "Pat")))
        snap = await st.snapshot()
        assert tid in snap["tasks"], "requeue rebuilt the one-off"
        assert snap["tasks"][tid]["pending"] is not None, "and re-fired it"
        assert snap["tasks"][tid]["recurring"] is False


def test_points_and_stars() -> None:
    """Pure scoring helpers: puntos (bounties double, legacy → 1) and the monthly
    ⭐ stars derived from past months only (ties share the star)."""
    from joblin.bot import _completion_points, monthly_scores, star_counts

    assert _completion_points({}) == 1  # legacy record with no 'points' field
    assert _completion_points({"points": 2}) == 2
    assert _completion_points({"points": 0}) == 1  # bad value falls back to 1

    recs = [
        # April: Pat 3 (a bounty=2 plus a normal), Sam 1 -> Pat wins April.
        {"guild_id": 1, "month": "2026-04", "user_id": 1, "user_name": "Pat", "points": 2},
        {"guild_id": 1, "month": "2026-04", "user_id": 1, "user_name": "Pat"},
        {"guild_id": 1, "month": "2026-04", "user_id": 2, "user_name": "Sam"},
        # May: Pat 2, Sam 2 -> a tie, so both earn a star.
        {"guild_id": 1, "month": "2026-05", "user_id": 1, "user_name": "Pat", "points": 2},
        {"guild_id": 1, "month": "2026-05", "user_id": 2, "user_name": "Sam", "points": 2},
        # June (the current month below): Sam leads but it's not decided yet.
        {"guild_id": 1, "month": "2026-06", "user_id": 2, "user_name": "Sam", "points": 5},
        # A different guild must never bleed in.
        {"guild_id": 9, "month": "2026-04", "user_id": 1, "user_name": "X", "points": 99},
    ]

    months = monthly_scores(recs, 1)
    assert set(months) == {"2026-04", "2026-05", "2026-06"}, "other guild excluded"
    assert months["2026-04"][1] == {"points": 3, "chores": 2, "claps": 0, "name": "Pat"}
    assert months["2026-04"][2]["points"] == 1

    # June is the current month -> excluded; Pat wins April, ties May -> 2 stars.
    assert star_counts(recs, 1, current_month="2026-06") == {1: 2, 2: 1}
    # Once July is current, June counts too and Sam leads it solo.
    assert star_counts(recs, 1, current_month="2026-07") == {1: 2, 2: 2}


def test_chore_count_shares_games() -> None:
    """The leaderboard's chore tally: each of your own completions is a chore,
    but a game round is ONE chore shared by everyone who scored in it (three
    people pitching in on the car wash = one chore; a do-em-up is one chore
    however many bricks got laid), and claps aren't chores at all. Regression:
    every log row used to bump "chores", so the footer's chore count silently
    tracked the row count instead of the chores done."""
    from joblin.bot import build_leaderboard, monthly_scores

    recs = [
        {"guild_id": 1, "month": "2026-04", "user_id": 1, "user_name": "Pat",
         "kind": "recurring", "points": 1},
        {"guild_id": 1, "month": "2026-04", "user_id": 1, "user_name": "Pat",
         "kind": "once", "points": 2},  # a bounty
        # One pitch-in round, two scorers (same game id + close timestamp).
        {"guild_id": 1, "month": "2026-04", "user_id": 1, "user_name": "Pat",
         "kind": "pitchin", "task_id": "g1", "ts": "t1", "points": 1},
        {"guild_id": 1, "month": "2026-04", "user_id": 2, "user_name": "Sam",
         "kind": "pitchin", "task_id": "g1", "ts": "t1", "points": 1},
        # A later round of the same recurring pitch-in is its own chore.
        {"guild_id": 1, "month": "2026-04", "user_id": 1, "user_name": "Pat",
         "kind": "pitchin", "task_id": "g1", "ts": "t2", "points": 1},
        # One do-em-up round: many units, two talliers, still one chore.
        {"guild_id": 1, "month": "2026-04", "user_id": 1, "user_name": "Pat",
         "kind": "doemup", "task_id": "g2", "ts": "t3", "points": 5},
        {"guild_id": 1, "month": "2026-04", "user_id": 2, "user_name": "Sam",
         "kind": "doemup", "task_id": "g2", "ts": "t3", "points": 3},
        {"guild_id": 1, "month": "2026-04", "user_id": 1, "user_name": "Pat",
         "kind": "clap", "points": 1},
        # A legacy row without "kind" reads as a chore.
        {"guild_id": 1, "month": "2026-04", "user_id": 1, "user_name": "Pat"},
    ]
    ent = monthly_scores(recs, 1)["2026-04"][1]
    assert ent["points"] == 12  # every row's puntos still count
    assert ent["chores"] == 3  # own chores only: recurring + once + legacy
    assert ent["claps"] == 1

    # Footer: 3 own chores + 2 pitch-in rounds + 1 do-em-up round = 6 chores,
    # while the puntos keep every row's full value (Pat's 12 + Sam's 4).
    cfg = {"channel_id": 999, "timezone": "Europe/Berlin", "item_bar": 25}
    text, empty = build_leaderboard(recs, 1, cfg, "2026-04")
    assert not empty and "_6 chores · 16 puntos in 2026-04._" in text


async def test_bounty() -> None:
    """A bounty is worth 2 puntos and its creator can't claim it; anyone else can."""
    import joblin.bot as bot

    with tempfile.TemporaryDirectory() as d:
        st = Store(pathlib.Path(d) / "store.json", pathlib.Path(d) / "log.jsonl")
        st.load()
        bot.store = st  # handlers read the module global at call time
        ch = FakeChannel()
        bot.bot.get_channel = lambda cid: ch  # type: ignore[assignment]

        now = m.now_utc()
        cfg = {"channel_id": 999, "timezone": "Europe/Berlin", "reminder_role_id": None}
        tid, creator = "muck", 1
        task = {
            "id": tid, "guild_id": 1, "brief": "Muck out the barn", "description": None,
            "bounty": True, "recurring": False, "freq": "once", "interval_days": 0,
            "weekdays": [], "monthdays": [], "time_of_day": None,
            "next_due": m.to_iso(now - dt.timedelta(seconds=1)),  # already due -> fires
            "created_by": creator, "created_at": m.to_iso(now), "pending": None,
        }
        async with st.txn() as data:
            data["configs"]["1"] = cfg
            data["tasks"][tid] = task

        await bot.fire_task(tid, ch, cfg)
        posted = (await st.snapshot())["tasks"][tid]["pending"]["message_ids"][-1]
        assert "💰" in (ch.msgs[posted].content or ""), "bounty post is tagged"

        # The creator's ✅ must be declined — nothing completed, nothing logged.
        await bot.on_raw_reaction_add(
            FakePayload(posted, "✅", user_id=creator, member=FakeMember(creator, "Boss"))
        )
        snap = await st.snapshot()
        assert snap["tasks"][tid]["pending"] is not None, "creator can't claim own bounty"
        assert st.read_completions() == [], "a blocked self-claim logs nothing"
        assert any("your** bounty" in (msg.content or "") for msg in ch.msgs.values())

        # Someone else claims it -> completed and worth 2 puntos.
        await bot.on_raw_reaction_add(
            FakePayload(posted, "✅", user_id=2, member=FakeMember(2, "Pat"))
        )
        snap = await st.snapshot()
        assert tid not in snap["tasks"], "a completed one-off bounty is removed"
        comps = st.read_completions()
        assert len(comps) == 1 and comps[0]["user_id"] == 2 and comps[0]["points"] == 2


def test_trinkets() -> None:
    import random
    from joblin import trinkets as T

    # Determinism is the whole foundation: identical inputs -> identical trinket,
    # even across processes/restarts (sha256 seed, not the salted builtin hash).
    a = T.roll_for(7, 99, "2026-06")
    assert a == T.roll_for(7, 99, "2026-06"), "trinket rolls must be deterministic"

    # Every zone yields a clean, fully-rendered name for many users — no empty
    # names, no unfilled "{...}" placeholders, no "of a a"/" a a" article slips.
    for zk in T.ZONE_KEYS:
        for u in range(300):
            rng = random.Random(T._seed("t", 1, u, "x", zk))
            t = T.roll_trinket(rng, T.ZONES[zk])
            d = t["display"]
            assert d.strip(), f"empty trinket name in zone {zk}"
            assert "{" not in d and "}" not in d, f"unfilled placeholder: {d!r}"
            assert " a a" not in f" {d}" and "of a a" not in d, f"bad article: {d!r}"

    # Zone rotation: each cycle of len(ZONES) months is a permutation of all
    # zones (true rotation), with no back-to-back repeats inside a cycle.
    n = len(T.ZONE_KEYS)
    start = (T._month_index("2026-01") // n) * n
    cycle = []
    for i in range(start, start + n):
        y, mo = divmod(i, 12)
        cycle.append(T.zone_for_month(f"{y:04d}-{mo + 1:02d}"))
    assert sorted(cycle) == sorted(T.ZONE_KEYS), "a cycle must cover every zone once"
    assert all(cycle[i] != cycle[i + 1] for i in range(n - 1)), "no back-to-back repeats"


def test_vitrine_award() -> None:
    import joblin.bot as B
    from joblin import trinkets as T

    # Bar handling: explicit, defaulted, and junk all resolve sanely.
    assert B._guild_bar({"item_bar": 40}) == 40
    assert B._guild_bar({}) == T.DEFAULT_BAR
    assert B._guild_bar(None) == T.DEFAULT_BAR

    recs = [
        {"guild_id": 1, "user_id": 5, "user_name": "A", "month": "2026-01"},               # 1
        {"guild_id": 1, "user_id": 5, "user_name": "A", "month": "2026-01", "points": 2},  # +2 = 3
        {"guild_id": 1, "user_id": 9, "user_name": "B", "month": "2026-01"},               # 1 (< bar)
        {"guild_id": 1, "user_id": 5, "user_name": "A", "month": "2026-03"},               # current
    ]
    # bar=3, "current" month = 2026-03: only user 5's closed 2026-01 qualifies.
    won = B.vitrine_for(recs, 1, 5, {"item_bar": 3}, "2026-03")
    assert [t["month"] for t in won] == ["2026-01"], "award only past months >= bar"
    assert B.vitrine_for(recs, 1, 9, {"item_bar": 3}, "2026-03") == [], "below the bar earns nothing"
    # The current (still-contested) month is never awarded yet.
    assert all(t["month"] != "2026-03" for t in B.vitrine_for(recs, 1, 5, {"item_bar": 1}, "2026-03"))
    # And it's stable across calls (derived, not re-randomised each time).
    assert won[0]["display"] == B.vitrine_for(recs, 1, 5, {"item_bar": 3}, "2026-03")[0]["display"]

    # Multiples: each whole multiple of the bar earns another trinket from that
    # month. User 5 cleared 3 puntos in 2026-01 → three at a 1-punto bar (idx 0,1,2),
    # one at a 2-punto bar (floor(3/2)), zero once the bar exceeds the score.
    many = B.vitrine_for(recs, 1, 5, {"item_bar": 1}, "2026-03")
    assert [t["month"] for t in many] == ["2026-01"] * 3, "3 puntos / 1-punto bar → 3 trinkets"
    assert [t["idx"] for t in many] == [0, 1, 2], "indexed 0,1,2 within the month"
    assert len(B.vitrine_for(recs, 1, 5, {"item_bar": 2}, "2026-03")) == 1, "floor(3/2) = 1"
    assert B.vitrine_for(recs, 1, 5, {"item_bar": 4}, "2026-03") == [], "3 puntos under a 4-punto bar earns none"

    # Each trinket is deterministic and wears the zone its own weighted draw chose
    # (the 70/30 bonus — see test_zone_pick), extras included, stable across calls.
    assert many[1] == T.roll_for(1, 5, "2026-01", 1), "extra trinkets are deterministic too"
    for t in many:
        zk, in_season = T.zone_pick_for(1, 5, "2026-01", t["idx"])
        assert t["zone_key"] == zk and t["in_season"] == in_season, "trinket wears its picked zone"


def test_bar_for_history() -> None:
    """bar_for: a month is governed by the last bar change made before its
    guild-local close; a still-open month floats with the latest change —
    whatever is in force when the month ends is what freezes."""
    import joblin.bot as B
    from joblin import trinkets as T

    # Legacy configs (no history) read as the scalar having been in force forever.
    assert B.bar_for({"item_bar": 40}, "2020-01") == 40
    assert B.bar_for({}, "2026-01") == T.DEFAULT_BAR
    assert B.bar_for(None, "2026-01") == T.DEFAULT_BAR

    cfg = {"timezone": "Europe/Berlin", "item_bar": 50,
           "bar_history": [{"at": "1970-01-01T00:00:00+00:00", "bar": 25},
                           {"at": "2026-08-15T12:00:00+00:00", "bar": 50}]}
    assert B.bar_for(cfg, "2026-07") == 25, "July closed before the change — frozen"
    assert B.bar_for(cfg, "2026-08") == 50, "August was still open, so it floats"
    assert B.bar_for(cfg, "2026-09") == 50, "and 50 is what August's close froze"

    # The close is guild-local: 22:30 UTC on July 31st is already August 1st in
    # Berlin (July closed at 22:00 UTC), so the change misses July…
    cfg["bar_history"][1]["at"] = "2026-07-31T22:30:00+00:00"
    assert B.bar_for(cfg, "2026-07") == 25
    assert B.bar_for(cfg, "2026-08") == 50
    # …but 21:30 UTC is 23:30 Berlin — still July, so the change lands in it.
    cfg["bar_history"][1]["at"] = "2026-07-31T21:30:00+00:00"
    assert B.bar_for(cfg, "2026-07") == 50

    # Several changes within one month: the last one standing at close wins.
    cfg["bar_history"] = [{"at": "1970-01-01T00:00:00+00:00", "bar": 25},
                          {"at": "2026-08-10T00:00:00+00:00", "bar": 100},
                          {"at": "2026-08-20T00:00:00+00:00", "bar": 10}]
    assert B.bar_for(cfg, "2026-08") == 10

    # Mangled events are skipped; a junk month string reads as still open.
    cfg["bar_history"].append({"at": "not-a-date", "bar": 7})
    assert B.bar_for(cfg, "2026-08") == 10
    assert B.bar_for(cfg, "garbage") == 10


def test_vitrine_frozen_bars() -> None:
    """Each month's trinkets are judged against the bar it closed under —
    changing the bar later never redraws a finished month."""
    import joblin.bot as B

    recs = [
        {"guild_id": 1, "user_id": 5, "user_name": "A", "month": "2026-01", "points": 2},
        {"guild_id": 1, "user_id": 5, "user_name": "A", "month": "2026-01"},               # 3 total
        {"guild_id": 1, "user_id": 5, "user_name": "A", "month": "2026-02", "points": 2},
        {"guild_id": 1, "user_id": 5, "user_name": "A", "month": "2026-02", "points": 2},  # 4 total
    ]
    cfg = {"item_bar": 2,
           "bar_history": [{"at": "1970-01-01T00:00:00+00:00", "bar": 3},
                           {"at": "2026-02-10T00:00:00+00:00", "bar": 2}]}
    won = B.vitrine_for(recs, 1, 5, cfg, "2026-03")
    # January closed under bar 3 → 3//3 = 1; February under bar 2 → 4//2 = 2.
    assert [t["month"] for t in won] == ["2026-01", "2026-02", "2026-02"]

    # Raising the bar afterwards (in March) leaves both closed months alone.
    cfg["bar_history"].append({"at": "2026-03-05T00:00:00+00:00", "bar": 100})
    cfg["item_bar"] = 100
    assert B.vitrine_for(recs, 1, 5, cfg, "2026-03") == won


def test_record_bar_change() -> None:
    """record_bar_change seeds the history with the pre-change bar (back-dated
    to the epoch) so already-closed months keep reading the bar they ended
    under, then appends the new value and mirrors it into item_bar."""
    import datetime as dt

    import joblin.bot as B
    from joblin import trinkets as T

    now = dt.datetime(2026, 8, 15, 12, 0, tzinfo=dt.timezone.utc)
    cfg = {"item_bar": 25}
    B.record_bar_change(cfg, 50, now)
    assert cfg["item_bar"] == 50
    assert [ev["bar"] for ev in cfg["bar_history"]] == [25, 50]
    assert cfg["bar_history"][0]["at"].startswith("1970-"), "seeded old bar at the epoch"
    assert B.bar_for(cfg, "2026-07") == 25, "months closed pre-change keep the old bar"
    assert B.bar_for(cfg, "2026-08") == 50

    B.record_bar_change(cfg, 50, now)  # a no-op "change" appends nothing
    assert [ev["bar"] for ev in cfg["bar_history"]] == [25, 50]
    B.record_bar_change(cfg, 30, dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc))
    assert [ev["bar"] for ev in cfg["bar_history"]] == [25, 50, 30]

    fresh: dict = {}  # a config that never had a bar seeds from the default
    B.record_bar_change(fresh, 10, now)
    assert [ev["bar"] for ev in fresh["bar_history"]] == [T.DEFAULT_BAR, 10]
    assert fresh["item_bar"] == 10


def test_zone_pick() -> None:
    """Each trinket draws its zone independently: the month's featured zone with
    probability FEATURED_WEIGHT (~0.70), else an off-season zone uniformly — a
    bonus, not a monopoly. Deterministic per (guild, user, month, idx)."""
    from joblin import trinkets as T

    ym = "2026-06"
    featured = T.zone_for_month(ym)
    assert T.zone_pick_for(1, 2, ym, 0) == T.zone_pick_for(1, 2, ym, 0), "stable per identity"

    # Over many users the featured share lands near FEATURED_WEIGHT; every pick is
    # a real zone, and in_season is true iff it's the featured one.
    n, hits, off = 5000, 0, set()
    for u in range(n):
        zk, in_season = T.zone_pick_for(1, u, ym, 0)
        assert zk in T.ZONE_KEYS
        assert (zk == featured) == in_season, "in_season iff featured zone"
        if in_season:
            hits += 1
        else:
            off.add(zk)
    frac = hits / n
    assert 0.66 < frac < 0.74, f"featured share {frac:.3f} not ≈ {T.FEATURED_WEIGHT}"
    assert featured not in off, "an off-season pick is never the featured zone"
    assert len(off) > 1, "off-season strays spread across multiple other zones"

    # Different indices draw independently, so a month's trinkets can mix zones.
    zones = {T.zone_pick_for(1, 3, ym, i)[0] for i in range(40)}
    assert len(zones) > 1, "40 trinkets in one month should not all be one zone"

    # roll_for stamps the picked zone + flag (and matching emoji/name) onto the item.
    t = T.roll_for(1, 7, ym, 0)
    zk, in_season = T.zone_pick_for(1, 7, ym, 0)
    assert t["zone_key"] == zk and t["in_season"] == in_season
    assert t["zone_emoji"] == T.zone_emoji(zk) and t["zone_name"] == T.zone_label(zk)


async def test_finalize_keeps_fun_reactions() -> None:
    """Closing a pitch-in takes down our ✅/🏁 buttons but leaves a member's fun
    reaction (a 😄) in place — the old clear_reactions() would have wiped it."""
    with tempfile.TemporaryDirectory() as d:
        bot, st, ch = await _game_setup(d)
        now = m.now_utc()
        pid, msg = await bot.post_pitchin(
            ch, guild_id=1, creator_id=1, brief="Laundry bonanza", description=None,
            expires_at=m.to_iso(now + dt.timedelta(hours=6)), points_each=1,
            max_scorers=None, now=now,
        )
        mid = msg.id
        await bot.on_raw_reaction_add(FakePayload(mid, "✅", user_id=42, member=FakeMember(42, "Pat")))
        ch.msgs[mid].reactions.add("😄")  # a family member piles on for fun
        await bot.on_raw_reaction_add(
            FakePayload(mid, m.EMOJI_END, user_id=1, member=FakeMember(1, "Boss"))
        )
        react = ch.msgs[mid].reactions
        assert "😄" in react, "a fun reaction must survive the close"
        assert "✅" not in react and m.EMOJI_END not in react, "our buttons are taken down"
        assert mid not in ch.cleared, "we never nuke every reaction anymore"


async def test_nag_tally() -> None:
    """Every nag bumps a lifetime nag_count that survives completion, keeps
    accumulating across occurrences, and surfaces in /listtasks."""
    with tempfile.TemporaryDirectory() as d:
        bot, st, ch = await _game_setup(d)
        tz = ZoneInfo("Europe/Berlin")
        now = m.now_utc()
        tod = now.astimezone(tz).strftime("%H:%M")
        cfg = {"channel_id": 999, "timezone": "Europe/Berlin", "reminder_role_id": None}
        tid = "feed"
        async with st.txn() as data:
            data["tasks"][tid] = {
                "id": tid, "guild_id": 1, "brief": "Feed the goats", "description": None,
                "recurring": True, "freq": "days", "interval_days": 1, "weekdays": [],
                "monthdays": [], "time_of_day": tod,
                "next_due": m.to_iso(now - dt.timedelta(seconds=1)),
                "created_by": 1, "created_at": m.to_iso(now), "pending": None,
            }

        await bot.fire_task(tid, ch, cfg)
        for _ in range(2):  # force two nags
            async with st.txn() as data:
                data["tasks"][tid]["pending"]["remind_at"] = m.to_iso(now - dt.timedelta(seconds=1))
            await bot.send_reminder(tid, ch, cfg)
        assert (await st.snapshot())["tasks"][tid]["nag_count"] == 2

        # Completing the occurrence must NOT reset the lifetime tally.
        posted = (await st.snapshot())["tasks"][tid]["pending"]["message_ids"][0]
        await bot.on_raw_reaction_add(FakePayload(posted, "✅", member=FakeMember(42, "Pat")))
        snap = await st.snapshot()
        assert snap["tasks"][tid]["pending"] is None
        assert snap["tasks"][tid]["nag_count"] == 2, "nag tally persists past completion"

        # A nag on the NEXT occurrence keeps accumulating.
        async with st.txn() as data:
            data["tasks"][tid]["next_due"] = m.to_iso(now - dt.timedelta(seconds=1))
        await bot.fire_task(tid, ch, cfg)
        async with st.txn() as data:
            data["tasks"][tid]["pending"]["remind_at"] = m.to_iso(now - dt.timedelta(seconds=1))
        await bot.send_reminder(tid, ch, cfg)
        assert (await st.snapshot())["tasks"][tid]["nag_count"] == 3

        inter = FakeInteraction(guild_id=1, user=FakeUser(1, "Boss"))
        await bot.listtasks.callback(inter)
        assert "🔔×3" in inter.response.content, "/listtasks shows the lifetime nag count"


async def test_listopen() -> None:
    """/listopen lists open chores + live games, each linking to the ORIGINAL post
    (never a nag), posts publicly, and says so when nothing is open."""
    with tempfile.TemporaryDirectory() as d:
        bot, st, ch = await _game_setup(d)
        tz = ZoneInfo("Europe/Berlin")
        now = m.now_utc()
        tod = now.astimezone(tz).strftime("%H:%M")
        cfg = {"channel_id": 999, "timezone": "Europe/Berlin", "reminder_role_id": None}

        # Nothing open yet.
        inter = FakeInteraction(guild_id=1, user=FakeUser(1, "Boss"))
        await bot.listopen.callback(inter)
        assert "caught up" in inter.response.content

        # A recurring chore: fire it, then nag it so it has an original + a nag post.
        tid = "water"
        async with st.txn() as data:
            data["tasks"][tid] = {
                "id": tid, "guild_id": 1, "brief": "Fill animal water", "description": None,
                "recurring": True, "freq": "days", "interval_days": 1, "weekdays": [],
                "monthdays": [], "time_of_day": tod,
                "next_due": m.to_iso(now - dt.timedelta(seconds=1)),
                "created_by": 1, "created_at": m.to_iso(now), "pending": None,
            }
        await bot.fire_task(tid, ch, cfg)
        orig = (await st.snapshot())["tasks"][tid]["pending"]["message_ids"][0]
        async with st.txn() as data:
            data["tasks"][tid]["pending"]["remind_at"] = m.to_iso(now - dt.timedelta(seconds=1))
        await bot.send_reminder(tid, ch, cfg)
        mids = (await st.snapshot())["tasks"][tid]["pending"]["message_ids"]
        assert len(mids) == 2 and mids[0] == orig
        nag = mids[1]

        # And a live pitch-in.
        pid, pmsg = await bot.post_pitchin(
            ch, guild_id=1, creator_id=1, brief="Sweep the barn", description=None,
            expires_at=m.to_iso(now + dt.timedelta(hours=6)), points_each=1,
            max_scorers=None, now=now,
        )

        inter = FakeInteraction(guild_id=1, user=FakeUser(1, "Boss"))
        await bot.listopen.callback(inter)
        desc = inter.response.embed.description
        assert "Fill animal water" in desc and "Sweep the barn" in desc
        assert f"/{orig}" in desc, "links to the original post"
        assert f"/{nag}" not in desc, "never links to a nag"
        assert f"/{pmsg.id}" in desc, "the live pitch-in links to its own post"
        assert not inter.response.ephemeral, "listopen posts publicly to the channel"


async def test_listtasks_pagination() -> None:
    """Many tasks paginate: /listtasks attaches a ◀/▶ pager whose buttons flip
    pages, so every id stays reachable instead of being truncated away."""
    with tempfile.TemporaryDirectory() as d:
        bot, st, ch = await _game_setup(d)
        now = m.now_utc()
        async with st.txn() as data:
            for i in range(40):
                data["tasks"][f"t{i:02d}"] = {
                    "id": f"t{i:02d}", "guild_id": 1,
                    "brief": f"Chore number {i} on the farm", "description": None,
                    "recurring": False, "freq": "once", "interval_days": 0,
                    "weekdays": [], "monthdays": [], "time_of_day": None,
                    "next_due": m.to_iso(now + dt.timedelta(hours=i + 1)),
                    "created_by": 1, "created_at": m.to_iso(now), "pending": None,
                }

        inter = FakeInteraction(guild_id=1, user=FakeUser(1, "Boss"))
        await bot.listtasks.callback(inter)
        view = inter.response.view
        assert isinstance(view, bot.ListPaginator), "many tasks -> a pager is attached"
        assert "Page 1/" in inter.response.content
        assert view.prev_btn.disabled and not view.next_btn.disabled, "start: ◀ off, ▶ on"

        # Click ▶ to the last page; it should disable at the end.
        guard = 0
        i2 = inter
        while view.index < len(view.pages) - 1 and guard < 50:
            i2 = FakeInteraction(guild_id=1, user=FakeUser(1, "Boss"))
            await view._go_next(i2)
            guard += 1
        assert view.index == len(view.pages) - 1
        assert view.next_btn.disabled and not view.prev_btn.disabled, "end: ▶ off, ◀ on"
        assert i2.response.content == view.pages[-1]

        # Every id is reachable across the pages.
        allpages = "\n".join(view.pages)
        for i in range(40):
            assert f"t{i:02d}" in allpages, f"id t{i:02d} must be reachable"


async def test_edit_games() -> None:
    """/edit pitchin|doemup: retiming a dormant recurring game moves its slot and
    next round; editing a live game re-renders its post; a schedule change to a
    live round applies from the next round; bad/empty edits are rejected."""
    with tempfile.TemporaryDirectory() as d:
        bot, st, ch = await _game_setup(d)
        tz = ZoneInfo("Europe/Berlin")
        now = m.now_utc()
        rule = {"freq": "days", "interval_days": 1, "weekdays": [], "monthdays": [],
                "time_of_day": "07:00"}

        # Recurring pitch-in, closed so it's dormant between rounds. /edit pitchin
        # at: moves the daily slot later in the day (the "go to bed later" case).
        pid, _ = await bot.post_pitchin(
            ch, guild_id=1, creator_id=1, brief="Bed o'clock", description=None,
            expires_at=m.to_iso(now - dt.timedelta(seconds=1)), points_each=1,
            max_scorers=None, now=now, recurrence=rule, duration_secs=300,
        )
        await bot.sweep_games(m.now_utc(), await st.snapshot())  # close -> dormant
        p = (await st.snapshot())["pitchins"][pid]
        assert p["message_id"] is None and p["next_due"] is not None, "dormant"

        inter = FakeInteraction(guild_id=1, user=FakeUser(1, "Boss"))
        await bot.edit_pitchin.callback(inter, event=pid, at="22:30")
        p = (await st.snapshot())["pitchins"][pid]
        nd = m.from_iso(p["next_due"])
        assert p["time_of_day"] == "22:30", "slot moved"
        assert nd > m.now_utc() and nd.astimezone(tz).strftime("%H:%M") == "22:30"
        assert "Updated" in inter.response.content and "next round" in inter.response.content

        # Live do-em-up: editing brief + points re-renders the open post in place.
        did, msg = await bot.post_doemup(
            ch, guild_id=1, creator_id=1, brief="Old name", description=None,
            points_each=1, deadline=None, point_limit=None, now=m.now_utc(),
            recurrence=None, duration_secs=None,
        )
        inter = FakeInteraction(guild_id=1, user=FakeUser(1, "Boss"))
        await bot.edit_doemup.callback(inter, event=did, brief="New name", puntos=3)
        dd = (await st.snapshot())["doemups"][did]
        assert dd["brief"] == "New name" and dd["points_each"] == 3
        assert "New name" in ch.msgs[msg.id].content, "live post re-rendered"
        assert ch.msgs[msg.id].view is not None, "buttons kept on re-render"

        # Live pitch-in: /edit pitchin expires: pulls in this round's close time.
        pid2, msg2 = await bot.post_pitchin(
            ch, guild_id=1, creator_id=1, brief="Open now", description=None,
            expires_at=m.to_iso(m.now_utc() + dt.timedelta(hours=1)), points_each=1,
            max_scorers=None, now=m.now_utc(), recurrence=None, duration_secs=None,
        )
        inter = FakeInteraction(guild_id=1, user=FakeUser(1, "Boss"))
        await bot.edit_pitchin.callback(inter, event=pid2, expires="in 10 minutes")
        left = (m.from_iso((await st.snapshot())["pitchins"][pid2]["expires_at"])
                - m.now_utc()).total_seconds()
        assert 580 <= left <= 605, "close pulled in to ~10 min"

        # A schedule change to a LIVE recurring round applies from the next round:
        # the slot updates but the current open post is left running.
        did2, _ = await bot.post_doemup(
            ch, guild_id=1, creator_id=1, brief="Live recurring", description=None,
            points_each=1, deadline=m.to_iso(m.now_utc() + dt.timedelta(hours=1)),
            point_limit=None, now=m.now_utc(), recurrence=rule, duration_secs=3600,
        )
        before_mid = (await st.snapshot())["doemups"][did2]["message_id"]
        inter = FakeInteraction(guild_id=1, user=FakeUser(1, "Boss"))
        await bot.edit_doemup.callback(inter, event=did2, at="06:15")
        dd2 = (await st.snapshot())["doemups"][did2]
        assert dd2["time_of_day"] == "06:15", "future rounds use the new slot"
        assert dd2["message_id"] == before_mid and dd2["next_due"] is None, "live round untouched"
        assert "next round" in inter.response.content

        # Errors: unknown event, and an empty edit.
        inter = FakeInteraction(guild_id=1, user=FakeUser(1, "Boss"))
        await bot.edit_pitchin.callback(inter, event="nope-nope")
        assert "not found" in inter.response.content.lower()
        inter = FakeInteraction(guild_id=1, user=FakeUser(1, "Boss"))
        await bot.edit_doemup.callback(inter, event=did)
        assert "nothing to change" in inter.response.content.lower()


def test_leaderboard_text() -> None:
    """build_leaderboard renders the board and flags the empty-month case (it's
    the shared core of /leaderboard and the nightly auto-post)."""
    import joblin.bot as bot

    cfg = {"channel_id": 999, "timezone": "Europe/Berlin", "item_bar": 25}
    recs = [
        {"guild_id": 1, "month": "2026-04", "user_id": 1, "user_name": "Pat", "points": 2},
        {"guild_id": 1, "month": "2026-04", "user_id": 2, "user_name": "Sam"},
        {"guild_id": 1, "month": "2026-04", "user_id": 1, "user_name": "Pat",
         "kind": "clap", "points": 1},
    ]
    text, empty = bot.build_leaderboard(recs, 1, cfg, "2026-04")
    assert not empty
    assert "Chore leaderboard — 2026-04" in text
    # Puntos lead each line; claps received ride the name like nags in /listtasks
    # (April is a past month, so Pat also wears its ⭐ on the line).
    assert "**3 puntos** — <@1> ⭐×1 · 👏×1" in text
    assert "**1 punto** — <@2>" in text and "<@2> · 👏" not in text
    # A month with nothing logged → empty flag set, gentle nudge shown.
    text2, empty2 = bot.build_leaderboard(recs, 1, cfg, "2026-03")
    assert empty2 and "No chores logged" in text2
    # Another guild's records never bleed in.
    _, empty3 = bot.build_leaderboard(recs, 9, cfg, "2026-04")
    assert empty3


async def test_daily_backup() -> None:
    """Nightly backup: arms on first sight, fires once the deadline passes *and*
    the log changed (posting a zip + leaderboard), and stays quiet otherwise."""
    import io
    import zipfile
    import joblin.bot as bot

    with tempfile.TemporaryDirectory() as d:
        _, st, ch = await _game_setup(d)
        ym = m.now_utc().astimezone(ZoneInfo("Europe/Berlin")).strftime("%Y-%m")

        # 1) First tick on a ready guild only *arms* the schedule — no post.
        now = m.now_utc()
        await bot.run_daily_backups(now, await st.snapshot())
        armed = (await st.snapshot())["configs"]["1"]["next_backup_at"]
        assert armed and m.from_iso(armed) > now, "next backup armed for the future"
        assert not ch.msgs and not ch.files, "arming must not post anything"

        # 2) Deadline due + a fresh completion → it fires.
        async with st.txn() as data:
            data["configs"]["1"]["next_backup_at"] = m.to_iso(now - dt.timedelta(minutes=1))
        await st.log_completion(
            {"id": "c1", "guild_id": 1, "month": ym, "user_id": 1,
             "user_name": "Pat", "points": 1}
        )
        await bot.run_daily_backups(m.now_utc(), await st.snapshot())

        assert len(ch.files) == 1, "one zip attachment posted"
        assert len(ch.msgs) == 2, "zip caption + leaderboard"
        captions = [msg.content for msg in ch.msgs.values()]
        assert any("Nightly backup" in c for c in captions)
        assert any("Chore leaderboard" in c for c in captions)

        # The zip holds both state files; the log carries our completion.
        raw = ch.files[0].fp.getvalue()
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            assert set(z.namelist()) == {"store.json", "completions.jsonl"}
            assert b'"id": "c1"' in z.read("completions.jsonl")

        cfg_now = (await st.snapshot())["configs"]["1"]
        assert cfg_now.get("last_backup_sig"), "change-detection baseline recorded"
        assert m.from_iso(cfg_now["next_backup_at"]) > m.now_utc(), "deadline rolled forward"

        # 3) Due again but nothing new logged → deadline rolls, nothing posts.
        async with st.txn() as data:
            data["configs"]["1"]["next_backup_at"] = m.to_iso(m.now_utc() - dt.timedelta(minutes=1))
        await bot.run_daily_backups(m.now_utc(), await st.snapshot())
        assert len(ch.files) == 1 and len(ch.msgs) == 2, "quiet when nothing was logged"

        # 4) A new completion + due deadline → fires again.
        async with st.txn() as data:
            data["configs"]["1"]["next_backup_at"] = m.to_iso(m.now_utc() - dt.timedelta(minutes=1))
        await st.log_completion(
            {"id": "c2", "guild_id": 1, "month": ym, "user_id": 2,
             "user_name": "Sam", "points": 1}
        )
        await bot.run_daily_backups(m.now_utc(), await st.snapshot())
        assert len(ch.files) == 2, "second backup posted after fresh activity"


# ---------------------------------------------------------------------------
# Web UI: session signing, schedule assembly, and the task CRUD mirrors
# ---------------------------------------------------------------------------
def test_web_sessions() -> None:
    """Sign/verify roundtrip, plus rejection of tampering and expiry."""
    secret = b"test-secret"
    exp = int(m.now_utc().timestamp()) + 3600
    payload = {"uid": "42", "name": "Pat", "guilds": ["1"], "exp": exp}
    tok = webui.sign_session(payload, secret)
    assert webui.read_session(tok, secret) == payload
    assert webui.read_session(tok, b"other-secret") is None, "wrong key rejected"
    body, sig = tok.split(".", 1)
    assert webui.read_session(f"{body}x.{sig}", secret) is None, "tampered body rejected"
    assert webui.read_session("garbage", secret) is None
    stale = webui.sign_session({**payload, "exp": exp - 7200}, secret)
    assert webui.read_session(stale, secret) is None, "expired session rejected"


def test_web_repeat_input() -> None:
    """The edit form's repeat prefill must round-trip through parse_repeat
    (describe_repeat is for humans and e.g. "every 2 weeks" doesn't parse)."""
    for rule in (
        {"freq": "once", "interval_days": 0, "weekdays": [], "monthdays": []},
        {"freq": "days", "interval_days": 1, "weekdays": [], "monthdays": []},
        {"freq": "days", "interval_days": 14, "weekdays": [], "monthdays": []},
        {"freq": "weekly", "interval_days": 0, "weekdays": [0, 3], "monthdays": []},
        {"freq": "weekly", "interval_days": 0, "weekdays": [5, 6], "monthdays": []},
        {"freq": "monthly", "interval_days": 0, "weekdays": [], "monthdays": [1, 15, 31]},
    ):
        back = m.parse_repeat(webui.repeat_input_of(rule))
        for key in ("freq", "interval_days", "weekdays", "monthdays"):
            assert back[key] == rule[key], f"{rule} came back as {back}"


def test_web_schedule() -> None:
    """build_schedule: guild filtering, pending/live-first ordering, form
    prefills, and read-only games — all from a plain snapshot."""
    now = m.now_utc()
    base = {"weekdays": [], "monthdays": [], "pending": None}
    snap = {
        "configs": {"1": {"channel_id": 9, "timezone": "Europe/Berlin"}},
        "tasks": {
            "aa": {**base, "id": "aa", "guild_id": 1, "brief": "Water plants",
                   "bounty": False, "recurring": True, "freq": "days",
                   "interval_days": 1, "time_of_day": "18:00",
                   "next_due": m.to_iso(now + dt.timedelta(days=1))},
            "bb": {**base, "id": "bb", "guild_id": 1, "brief": "Fix gate",
                   "bounty": True, "recurring": False, "freq": "once",
                   "interval_days": 0, "time_of_day": None, "next_due": None,
                   "pending": {"due_at": m.to_iso(now - dt.timedelta(hours=2)),
                               "remind_at": m.to_iso(now), "ffwd_count": 0,
                               "channel_id": 9, "message_ids": [1]}},
            "zz": {**base, "id": "zz", "guild_id": 2, "brief": "Other farm",
                   "bounty": False, "recurring": False, "freq": "once",
                   "interval_days": 0, "time_of_day": None,
                   "next_due": m.to_iso(now)},
        },
        "pitchins": {"pp": {"id": "pp", "guild_id": 1, "brief": "Hay day",
                            "message_id": 5, "channel_id": 9, "points_each": 2,
                            "expires_at": m.to_iso(now + dt.timedelta(hours=1)),
                            "recurring": False}},
        "doemups": {},
    }
    sched = webui.build_schedule(snap, 1)
    assert sched["timezone"] == "Europe/Berlin" and sched["config_ready"]
    assert [i["id"] for i in sched["items"]] == ["bb", "pp", "aa"], \
        "pending/live first (by instant), then scheduled"
    items = {i["id"]: i for i in sched["items"]}
    assert items["bb"]["status"] == "pending" and items["bb"]["bounty"]
    assert items["bb"]["at_input"].count(":") == 1 and "-" in items["bb"]["at_input"]
    assert items["aa"]["status"] == "scheduled"
    assert items["aa"]["repeat_input"] == "daily" and items["aa"]["at_input"] == "18:00"
    assert items["pp"]["status"] == "live" and items["pp"]["editable"] is False
    # An unconfigured guild still renders (UTC) but flags config_ready=False.
    bare = webui.build_schedule({**snap, "configs": {}}, 1)
    assert bare["timezone"] == "UTC" and not bare["config_ready"]


async def test_web_task_crud() -> None:
    """create/edit/delete mirrors of /newtask, /edit task, /deletetask — field
    presence is intent on edit, pending occurrences are left alone, and delete
    sweeps the reaction-routing rows."""
    with tempfile.TemporaryDirectory() as d:
        bot, st, ch = await _game_setup(d)

        # No config → create refuses (same gate as /newtask).
        missing, err = await webui.create_task(2, 42, {"brief": "x"})
        assert missing is None and "joblinconfig" in err

        task, err = await webui.create_task(
            1, 42, {"brief": "Fix fence", "at": "18:00", "repeat": "daily",
                    "description": "the far paddock", "bounty": True})
        assert err is None and task["freq"] == "days" and task["bounty"]
        assert task["time_of_day"] == "18:00" and task["created_by"] == 42
        tid = task["id"]
        assert (await st.snapshot())["tasks"][tid]["brief"] == "Fix fence"

        # Editing just the brief must not touch the schedule.
        before_due = task["next_due"]
        upd, note, err = await webui.apply_task_edit(1, tid, {"brief": "Fix the fence"})
        assert err is None and upd["brief"] == "Fix the fence"
        assert upd["next_due"] == before_due and upd["time_of_day"] == "18:00"

        # Repeat-only edit keeps the standing time_of_day (default_tod path).
        upd, note, err = await webui.apply_task_edit(1, tid, {"repeat": "mon,thu"})
        assert err is None and upd["freq"] == "weekly" and upd["weekdays"] == [0, 3]
        assert upd["time_of_day"] == "18:00"

        # A bad repeat is a clean error, nothing half-written.
        bad, note, err = await webui.apply_task_edit(1, tid, {"repeat": "blorp"})
        assert bad is None and err and "repeat" in err
        assert (await st.snapshot())["tasks"][tid]["freq"] == "weekly"

        # While an occurrence is pending, a schedule edit stores the new rule
        # but leaves the live occurrence alone (next_due stays None) + notes it.
        now = m.now_utc()
        async with st.txn() as data:
            t = data["tasks"][tid]
            t["pending"] = {"due_at": m.to_iso(now), "remind_at": m.to_iso(now),
                            "ffwd_count": 0, "channel_id": 999, "message_ids": [555]}
            t["next_due"] = None
            data["messages"]["555"] = tid
        upd, note, err = await webui.apply_task_edit(1, tid, {"repeat": "daily"})
        assert err is None and note and "next cycle" in note
        assert upd["freq"] == "days" and upd["next_due"] is None and upd["pending"]

        # Wrong guild can't delete; the right one sweeps the routing rows too.
        assert await webui.delete_task(2, tid) is None
        removed = await webui.delete_task(1, tid)
        assert removed and removed["id"] == tid
        snap = await st.snapshot()
        assert tid not in snap["tasks"] and "555" not in snap["messages"]


def main() -> None:
    test_emoji_key()
    test_time_parsing()
    test_parse_clock()
    test_resolve_when()
    test_parse_repeat()
    test_describe_repeat()
    test_recurrence_dispatch()
    test_first_due()
    test_first_due_now_fires_immediately()
    test_schedule_now_recurring_fires_today()
    test_weekly_pins_to_at_weekday()
    test_roll_forward_skips_backlog()
    test_roll_forward_dst()
    test_oneoff_parse()
    test_can_undo()
    test_points_and_stars()
    test_chore_count_shares_games()
    test_trinkets()
    test_zone_pick()
    test_vitrine_award()
    test_bar_for_history()
    test_vitrine_frozen_bars()
    test_record_bar_change()
    test_leaderboard_text()
    asyncio.run(test_daily_backup())
    asyncio.run(test_void_completion())
    asyncio.run(test_bounty())
    asyncio.run(test_store())
    asyncio.run(test_finalize_keeps_fun_reactions())
    asyncio.run(test_nag_tally())
    asyncio.run(test_listopen())
    asyncio.run(test_listtasks_pagination())
    asyncio.run(test_lifecycle_and_snooze())
    asyncio.run(test_legacy_migration())
    asyncio.run(test_requeue())
    asyncio.run(test_requeue_oneoff())
    asyncio.run(test_claps())
    asyncio.run(test_game_claps())
    asyncio.run(test_pitchin_lifecycle())
    asyncio.run(test_pitchin_cap_and_points())
    asyncio.run(test_pitchin_expiry())
    asyncio.run(test_doemup_lifecycle())
    asyncio.run(test_doemup_limit_and_deadline())
    asyncio.run(test_pitchin_recurring())
    asyncio.run(test_pitchin_at_deferred())
    asyncio.run(test_doemup_recurring())
    asyncio.run(test_doemup_at_deferred())
    asyncio.run(test_doemup_recurring_limit_rolls_on())
    asyncio.run(test_delete_live_game())
    asyncio.run(test_game_commands_recurring())
    asyncio.run(test_game_commands())
    asyncio.run(test_edit_games())
    test_web_sessions()
    test_web_repeat_input()
    test_web_schedule()
    asyncio.run(test_web_task_crud())
    print("✅ all smoke tests passed")


if __name__ == "__main__":
    main()
