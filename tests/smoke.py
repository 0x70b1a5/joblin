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
        self.view = None

    async def add_reaction(self, emoji) -> None:
        self.channel.added.append((self.id, str(emoji)))

    async def remove_reaction(self, emoji, user) -> None:
        pass

    async def clear_reactions(self) -> None:
        self.channel.cleared.append(self.id)

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
        self.ephemeral = None
        self.view = None
        self._done = False

    async def send_message(self, content=None, *, ephemeral=False, embed=None,
                           allowed_mentions=None) -> None:
        self.content, self.embed, self.ephemeral, self._done = content, embed, ephemeral, True

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
    import farmtracker.bot as bot

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
    then the creator 🏁 closes it and awards a point to whoever's still in."""
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

        # The creator 🏁 closes it: Pat earns 1 point, the post is finalized.
        await bot.on_raw_reaction_add(FakePayload(mid, m.EMOJI_END, user_id=1, member=FakeMember(1, "Boss")))
        snap = await st.snapshot()
        assert pid not in snap["pitchins"] and str(mid) not in snap["game_messages"]
        recs = st.read_completions()
        assert len(recs) == 1 and recs[0]["user_id"] == 42 and recs[0]["points"] == 1
        assert recs[0]["kind"] == "pitchin"
        assert "pitched in!" in ch.msgs[mid].content and mid in ch.cleared


async def test_pitchin_cap_and_points() -> None:
    """max_scorers closes the pitch-in the instant it fills; points_each>1 pays
    each scorer that many points."""
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
    is refused, the creator End closes it and awards count×points, and the points
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

        # /leaderboard (the upstream points + ⭐ stars board) totals those points:
        # Pat leads with 5, Bo has 2, footer counts 2 records · 7 pts.
        month = now.astimezone(ZoneInfo("Europe/Berlin")).strftime("%Y-%m")
        inter = FakeInteraction(user=FakeUser(1, "Boss"))
        await bot.leaderboard.callback(inter, month=month)
        assert "<@42> — **5 pts**" in inter.response.content
        assert "7 pts this month" in inter.response.content


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
        assert "farmconfig" in inter.response.content
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


def test_trinkets() -> None:
    import random
    from farmtracker import trinkets as T

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
    import farmtracker.bot as B
    from farmtracker import trinkets as T

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
    won = B.vitrine_for(recs, 1, 5, 3, "2026-03")
    assert [t["month"] for t in won] == ["2026-01"], "award only past months >= bar"
    assert B.vitrine_for(recs, 1, 9, 3, "2026-03") == [], "below the bar earns nothing"
    # The current (still-contested) month is never awarded yet.
    assert all(t["month"] != "2026-03" for t in B.vitrine_for(recs, 1, 5, 1, "2026-03"))
    # And it's stable across calls (derived, not re-randomised each time).
    assert won[0]["display"] == B.vitrine_for(recs, 1, 5, 3, "2026-03")[0]["display"]

    # Multiples: each whole multiple of the bar earns another trinket from that
    # month. User 5 cleared 3 pts in 2026-01 → three at a 1-pt bar (idx 0,1,2),
    # one at a 2-pt bar (floor(3/2)), zero once the bar exceeds the score.
    many = B.vitrine_for(recs, 1, 5, 1, "2026-03")
    assert [t["month"] for t in many] == ["2026-01"] * 3, "3 pts / 1-pt bar → 3 trinkets"
    assert [t["idx"] for t in many] == [0, 1, 2], "indexed 0,1,2 within the month"
    assert len(B.vitrine_for(recs, 1, 5, 2, "2026-03")) == 1, "floor(3/2) = 1"
    assert B.vitrine_for(recs, 1, 5, 4, "2026-03") == [], "3 pts under a 4-pt bar earns none"

    # Each trinket is deterministic and wears the zone its own weighted draw chose
    # (the 70/30 bonus — see test_zone_pick), extras included, stable across calls.
    assert many[1] == T.roll_for(1, 5, "2026-01", 1), "extra trinkets are deterministic too"
    for t in many:
        zk, in_season = T.zone_pick_for(1, 5, "2026-01", t["idx"])
        assert t["zone_key"] == zk and t["in_season"] == in_season, "trinket wears its picked zone"


def test_zone_pick() -> None:
    """Each trinket draws its zone independently: the month's featured zone with
    probability FEATURED_WEIGHT (~0.70), else an off-season zone uniformly — a
    bonus, not a monopoly. Deterministic per (guild, user, month, idx)."""
    from farmtracker import trinkets as T

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
    test_trinkets()
    test_zone_pick()
    test_vitrine_award()
    asyncio.run(test_void_completion())
    asyncio.run(test_bounty())
    asyncio.run(test_store())
    asyncio.run(test_lifecycle_and_snooze())
    asyncio.run(test_legacy_migration())
    asyncio.run(test_requeue())
    asyncio.run(test_requeue_oneoff())
    asyncio.run(test_pitchin_lifecycle())
    asyncio.run(test_pitchin_cap_and_points())
    asyncio.run(test_pitchin_expiry())
    asyncio.run(test_doemup_lifecycle())
    asyncio.run(test_doemup_limit_and_deadline())
    asyncio.run(test_pitchin_recurring())
    asyncio.run(test_doemup_recurring())
    asyncio.run(test_doemup_recurring_limit_rolls_on())
    asyncio.run(test_delete_live_game())
    asyncio.run(test_game_commands_recurring())
    asyncio.run(test_game_commands())
    print("✅ all smoke tests passed")


if __name__ == "__main__":
    main()
