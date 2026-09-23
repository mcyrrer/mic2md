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


def one_line(text: str) -> str:
    """Collapse whitespace so the value fits on one flat front matter line."""
    return " ".join(text.split())


def participant_label(name: str | None, url: str | None) -> str:
    """Prefer the display name; fall back to the address from a ``mailto:`` URL."""
    if name and name.strip():
        return one_line(name)
    url = url or ""
    return one_line(url.removeprefix("mailto:"))


def pick_current(events: list, now: datetime, grace_s: float = GRACE_S) -> object | None:
    """Timed events in progress or starting within ``grace_s``; the one starting last wins."""
    ts = now.timestamp()
    current = [
        e
        for e in events
        if not e.isAllDay()
        and e.startDate().timeIntervalSince1970() <= ts + grace_s
        and ts < e.endDate().timeIntervalSince1970()
    ]
    return max(current, key=lambda e: e.startDate().timeIntervalSince1970(), default=None)


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


def current_meeting(now: datetime | None = None) -> Meeting | None:
    """Return the meeting in progress, or None. Raises CalendarError if access is unavailable."""
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
    event = pick_current(list(store.eventsMatchingPredicate_(predicate) or []), now)
    if event is None:
        return None
    participants = []
    for p in event.attendees() or []:
        url = p.URL()
        label = participant_label(p.name(), url.absoluteString() if url else None)
        if label and label not in participants:
            participants.append(label)
    return Meeting(title=one_line(event.title() or ""), participants=participants)
