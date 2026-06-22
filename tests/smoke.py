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

# Make `farmtracker` importable when run straight from the repo (the package
# isn't pip-installed; running a script puts tests/ — not the root — on path).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# Importing the bot module runs every @bot.tree.command decorator and builds
# the FarmBot instance — a real smoke test of the command definitions.
import farmtracker.bot  # noqa: E402, F401
from farmtracker import models as m  # noqa: E402
from farmtracker.store import Store  # noqa: E402

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
    from farmtracker.bot import can_undo

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

    async def add_reaction(self, emoji) -> None:
        self.channel.added.append((self.id, str(emoji)))

    async def remove_reaction(self, emoji, user) -> None:
        pass

    async def clear_reactions(self) -> None:
        pass

    async def edit(self, content=None, allowed_mentions=None) -> None:
        self.content = content

    async def delete(self) -> None:
        self.channel.deleted.append(self.id)


class FakeChannel:
    def __init__(self) -> None:
        self.id = 999
        self.msgs: dict[int, FakeMessage] = {}
        self.added: list = []
        self.deleted: list = []
        self._next = 1000

    async def send(self, content=None, allowed_mentions=None, **kw) -> FakeMessage:
        self._next += 1
        msg = FakeMessage(self._next, self)
        msg.content = content
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


async def test_lifecycle_and_snooze() -> None:
    """Drive the real reaction handlers end-to-end against the fake channel:
    fire a weekly task, ✅-complete it (must roll to the next weekday), then
    ⏩-open a snooze panel and pick '2 days' from it."""
    import farmtracker.bot as bot

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
    import farmtracker.bot as bot

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

        await bot.edittask.callback(_Inter(), "leg2", repeat="mon,thu")
        t = (await st.snapshot())["tasks"]["leg2"]
        assert t["freq"] == "weekly" and t["weekdays"] == [0, 3] and t["time_of_day"] == "07:30"


async def test_requeue() -> None:
    """A ✅-completed post grows a 🔄 button; tapping it re-fires the chore right
    now as a fresh occurrence, rolls on normally when finished, and declines
    while another occurrence is already live."""
    import farmtracker.bot as bot

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


async def test_requeue_oneoff() -> None:
    """A completed one-off is gone from the store; 🔄 rebuilds it from the saved
    snapshot and re-fires it."""
    import farmtracker.bot as bot

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
    """Pure scoring helpers: points (bounties double, legacy → 1) and the monthly
    ⭐ stars derived from past months only (ties share the star)."""
    from farmtracker.bot import _completion_points, monthly_scores, star_counts

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
    assert months["2026-04"][1] == {"points": 3, "chores": 2, "name": "Pat"}
    assert months["2026-04"][2]["points"] == 1

    # June is the current month -> excluded; Pat wins April, ties May -> 2 stars.
    assert star_counts(recs, 1, current_month="2026-06") == {1: 2, 2: 1}
    # Once July is current, June counts too and Sam leads it solo.
    assert star_counts(recs, 1, current_month="2026-07") == {1: 2, 2: 2}


async def test_bounty() -> None:
    """A bounty is worth 2 points and its creator can't claim it; anyone else can."""
    import farmtracker.bot as bot

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

        # Someone else claims it -> completed and worth 2 points.
        await bot.on_raw_reaction_add(
            FakePayload(posted, "✅", user_id=2, member=FakeMember(2, "Pat"))
        )
        snap = await st.snapshot()
        assert tid not in snap["tasks"], "a completed one-off bounty is removed"
        comps = st.read_completions()
        assert len(comps) == 1 and comps[0]["user_id"] == 2 and comps[0]["points"] == 2


def main() -> None:
    test_emoji_key()
    test_time_parsing()
    test_parse_clock()
    test_resolve_when()
    test_parse_repeat()
    test_describe_repeat()
    test_recurrence_dispatch()
    test_first_due()
    test_roll_forward_skips_backlog()
    test_roll_forward_dst()
    test_oneoff_parse()
    test_can_undo()
    test_points_and_stars()
    asyncio.run(test_void_completion())
    asyncio.run(test_bounty())
    asyncio.run(test_store())
    asyncio.run(test_lifecycle_and_snooze())
    asyncio.run(test_legacy_migration())
    asyncio.run(test_requeue())
    asyncio.run(test_requeue_oneoff())
    print("✅ all smoke tests passed")


if __name__ == "__main__":
    main()
