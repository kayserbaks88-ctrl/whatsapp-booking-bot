from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

# ---- helpers ----

def overlaps(a_start, a_end, b_start, b_end):
    # True if time ranges overlap at all
    return a_start < b_end and a_end > b_start

def ceil_to_step(dt: datetime, step_minutes: int) -> datetime:
    # rounds up to the next step boundary (e.g. 15 mins)
    discard = (dt.minute % step_minutes)
    if discard == 0 and dt.second == 0 and dt.microsecond == 0:
        return dt
    minutes_to_add = step_minutes - discard
    dt2 = dt.replace(second=0, microsecond=0) + timedelta(minutes=minutes_to_add)
    return dt2

def within_hours(dt: datetime, tz: ZoneInfo,
                 open_days={"mon","tue","wed","thu","fri","sat"},
                 open_time=dtime(9,0),
                 close_time=dtime(18,0)) -> bool:
    local = dt.astimezone(tz)
    day = local.strftime("%a").lower()[:3]
    if day not in open_days:
        return False
    return open_time <= local.time() < close_time

def end_within_hours(start_dt: datetime, end_dt: datetime, tz: ZoneInfo,
                     open_time=dtime(9,0),
                     close_time=dtime(18,0)) -> bool:
    # ensures the full service fits before closing time
    s = start_dt.astimezone(tz)
    e = end_dt.astimezone(tz)
    if s.date() != e.date():
        return False
    return (open_time <= s.time()) and (e.time() <= close_time)

# ---- Google Calendar busy times ----

def get_busy_times(calendar_id: str, start: datetime, end: datetime, timezone: str):
    """
    Returns list of (busy_start, busy_end) in the requested timezone.
    Uses FreeBusy which includes all events.
    """
    tz = ZoneInfo(timezone)

    # Make sure inputs are timezone-aware and in tz
    start = start.astimezone(tz)
    end = end.astimezone(tz)

    body = {
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "items": [{"id": calendar_id}],
    }

    result = service.freebusy().query(body=body).execute()
    busy = result["calendars"][calendar_id].get("busy", [])

    intervals = []
    for b in busy:
        # Google returns ISO with offset (sometimes Z). Handle both safely.
        bs = datetime.fromisoformat(b["start"].replace("Z", "+00:00")).astimezone(tz)
        be = datetime.fromisoformat(b["end"].replace("Z", "+00:00")).astimezone(tz)
        intervals.append((bs, be))

    return intervals

# ---- Availability API (used by WhatsApp_bot.py) ----

def is_time_available(start_dt: datetime, calendar_id: str, duration_minutes: int, timezone: str):
    tz = ZoneInfo(timezone)

    start_dt = start_dt.astimezone(tz)
    end_dt = start_dt + timedelta(minutes=duration_minutes)

    # Full service must be within shop hours
    if not within_hours(start_dt, tz) or not end_within_hours(start_dt, end_dt, tz):
        return False, "Outside opening hours"

    # Query busy for the window (buffer optional; keep simple)
    busy = get_busy_times(calendar_id, start_dt, end_dt, timezone)

    for bs, be in busy:
        if overlaps(start_dt, end_dt, bs, be):
            # format reason in local tz
            return False, f"Clashes with {bs.strftime('%H:%M')}–{be.strftime('%H:%M')}"
    return True, "OK"

def next_available_slots(from_dt: datetime,
                         calendar_id: str,
                         duration_minutes: int,
                         timezone: str,
                         step_minutes: int = 15,
                         max_results: int = 5,
                         search_days: int = 7):
    """
    Returns next available start times (datetime list) after from_dt.
    Only returns slots where the FULL service fits and does not overlap busy times.
    """
    tz = ZoneInfo(timezone)
    from_dt = ceil_to_step(from_dt.astimezone(tz), step_minutes)

    results = []
    cursor = from_dt

    # search up to N days ahead
    end_search = from_dt + timedelta(days=search_days)

    while cursor < end_search and len(results) < max_results:
        # if day is closed or before opening, jump to next open time
        day = cursor.strftime("%a").lower()[:3]
        if day not in {"mon","tue","wed","thu","fri","sat"}:
            cursor = (cursor + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
            continue

        # enforce opening window start
        if cursor.time() < dtime(9,0):
            cursor = cursor.replace(hour=9, minute=0, second=0, microsecond=0)

        # if at/after closing, go next day 9am
        if cursor.time() >= dtime(18,0):
            cursor = (cursor + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
            continue

        slot_start = cursor
        slot_end = slot_start + timedelta(minutes=duration_minutes)

        # must fully fit before close
        if not end_within_hours(slot_start, slot_end, tz):
            cursor = (cursor + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
            continue

        # check busy overlap (query only for this slot window)
        busy = get_busy_times(calendar_id, slot_start, slot_end, timezone)

        clash = any(overlaps(slot_start, slot_end, bs, be) for bs, be in busy)

        if not clash:
            results.append(slot_start)

        cursor = cursor + timedelta(minutes=step_minutes)

    return results
