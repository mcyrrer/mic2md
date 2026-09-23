from datetime import datetime

from mic2md.meetings import one_line, participant_label, pick_current


class _Date:
    def __init__(self, dt: datetime):
        self.ts = dt.timestamp()

    def timeIntervalSince1970(self):
        return self.ts


class _Event:
    def __init__(self, title, start, end, all_day=False):
        self.title = title
        self._start, self._end, self._all_day = _Date(start), _Date(end), all_day

    def startDate(self):
        return self._start

    def endDate(self):
        return self._end

    def isAllDay(self):
        return self._all_day


NOW = datetime(2026, 9, 23, 10, 15)


def test_pick_current_ignores_all_day_past_and_future():
    events = [
        _Event("all day", datetime(2026, 9, 23), datetime(2026, 9, 24), all_day=True),
        _Event("earlier", datetime(2026, 9, 23, 9), datetime(2026, 9, 23, 10)),
        _Event("later", datetime(2026, 9, 23, 11), datetime(2026, 9, 23, 12)),
    ]
    assert pick_current(events, NOW) is None


def test_pick_current_prefers_latest_start_when_overlapping():
    events = [
        _Event("workshop", datetime(2026, 9, 23, 9), datetime(2026, 9, 23, 12)),
        _Event("standup", datetime(2026, 9, 23, 10), datetime(2026, 9, 23, 10, 30)),
    ]
    assert pick_current(events, NOW).title == "standup"


def test_pick_current_end_is_exclusive():
    events = [_Event("done", datetime(2026, 9, 23, 10), datetime(2026, 9, 23, 10, 15))]
    assert pick_current(events, NOW) is None


def test_participant_label():
    assert participant_label("Ada  Lovelace", "mailto:ada@example.com") == "Ada Lovelace"
    assert participant_label(None, "mailto:ada@example.com") == "ada@example.com"
    assert participant_label("  ", None) == ""


def test_one_line():
    assert one_line(" Sprint\nplanning  ") == "Sprint planning"


def test_pick_current_includes_meetings_starting_within_grace():
    soon = _Event("soon", datetime(2026, 9, 23, 10, 18), datetime(2026, 9, 23, 11))
    later = _Event("later", datetime(2026, 9, 23, 10, 30), datetime(2026, 9, 23, 11))
    assert pick_current([soon, later], NOW).title == "soon"
    assert pick_current([soon], NOW, grace_s=0) is None


def test_pick_current_prefers_upcoming_over_ending_meeting():
    events = [
        _Event("ending", datetime(2026, 9, 23, 9), datetime(2026, 9, 23, 10, 20)),
        _Event("next", datetime(2026, 9, 23, 10, 20), datetime(2026, 9, 23, 11)),
    ]
    assert pick_current(events, NOW).title == "next"
