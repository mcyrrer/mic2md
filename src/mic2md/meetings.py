"""Look up the meeting happening right now in the macOS calendar (Calendar.app's EventKit store)."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime

ACCESS_TIMEOUT_S = 30.0
# Recording often starts a little before the meeting does.
GRACE_S = 5 * 60


class CalendarError(Exception):
    pass


@dataclass
class Meeting:
    title: str
    participants: list[str] = field(default_factory=list)
    start: datetime | None = None
    end: datetime | None = None


def one_line(text: str) -> str:
    """Collapse whitespace so the value fits on one flat front matter line."""
    return " ".join(text.split())


def participant_label(name: str | None, url: str | None) -> str:
    """Prefer the display name; fall back to the address from a ``mailto:`` URL."""
    if name and name.strip():
        return one_line(name)
    url = url or ""
    return one_line(url.removeprefix("mailto:"))


def _start(event) -> float:
    return event.startDate().timeIntervalSince1970()


def _end(event) -> float:
    return event.endDate().timeIntervalSince1970()


def current_events(events: list, now: datetime, grace_s: float = GRACE_S) -> list:
    """Timed events in progress or starting within ``grace_s``, the one starting last first.

    The same meeting shown in several calendars (same title and times) is listed once.
    """
    ts = now.timestamp()
    current, seen = [], set()
    for e in sorted(events, key=_start, reverse=True):
        key = (one_line(e.title() or ""), _start(e), _end(e))
        if e.isAllDay() or not _start(e) <= ts + grace_s or not ts < _end(e) or key in seen:
            continue
        seen.add(key)
        current.append(e)
    return current


def pick_current(events: list, now: datetime, grace_s: float = GRACE_S) -> object | None:
    """The default meeting: the current one that started last."""
    return next(iter(current_events(events, now, grace_s)), None)


def _request_access(store) -> bool:
    import EventKit

    status = EventKit.EKEventStore.authorizationStatusForEntityType_(EventKit.EKEntityTypeEvent)
    # 3 = authorized / fullAccess (macOS 14+ renamed it, same value).
    if status == 3:
        return True
    if status in (1, 2):  # restricted, denied
        return False
    done = threading.Event()
    result = {"granted": False}

    def handler(granted, _error):
        result["granted"] = bool(granted)
        done.set()

    if hasattr(store, "requestFullAccessToEventsWithCompletion_"):
        store.requestFullAccessToEventsWithCompletion_(handler)
    else:
        store.requestAccessToEntityType_completion_(EventKit.EKEntityTypeEvent, handler)
    done.wait(ACCESS_TIMEOUT_S)
    return result["granted"]


def to_meeting(event) -> Meeting:
    participants = []
    for p in event.attendees() or []:
        url = p.URL()
        label = participant_label(p.name(), url.absoluteString() if url else None)
        if label and label not in participants:
            participants.append(label)
    return Meeting(
        title=one_line(event.title() or ""),
        participants=participants,
        start=datetime.fromtimestamp(_start(event)),
        end=datetime.fromtimestamp(_end(event)),
    )


def current_meeting(now: datetime | None = None) -> Meeting | None:
    """The meeting in progress that started last, or None (see ``current_meetings``)."""
    return next(iter(current_meetings(now)), None)


def current_meetings(now: datetime | None = None) -> list[Meeting]:
    """Meetings in progress (or about to start), the one starting last first.

    Raises CalendarError if access is unavailable.
    """
    try:
        import EventKit
        from Foundation import NSDate
    except ImportError as e:
        raise CalendarError("EventKit is not available (macOS only)") from e

    store = EventKit.EKEventStore.alloc().init()
    if not _request_access(store):
        raise CalendarError(
            "no calendar access; allow your terminal in System Settings → "
            "Privacy & Security → Calendars"
        )
    now = now or datetime.now()
    ts = now.timestamp()
    # Search a window around now; long meetings that started earlier still overlap.
    predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
        NSDate.dateWithTimeIntervalSince1970_(ts - 12 * 3600),
        NSDate.dateWithTimeIntervalSince1970_(ts + GRACE_S + 60),
        None,
    )
    events = current_events(list(store.eventsMatchingPredicate_(predicate) or []), now)
    return [to_meeting(e) for e in events]
