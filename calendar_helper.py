import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

# ----------------------------
# Config
# ----------------------------
TIMEZONE = os.getenv("TIMEZONE_HINT", "Europe/London")
TZ = ZoneInfo(TIMEZONE)

CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")
MAX_BARBERS = int(os.getenv("MAX_BARBERS", "1"))

DEFAULT_SERVICE_MINUTES = int(os.getenv("DEFAULT_SERVICE_MINUTES", "45"))
SLOT_STEP_MINUTES = int(os.getenv("SLOT_STEP_MINUTES", "15"))

SCOPES = ["https://www.googleapis.com/auth/calendar"]

creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
service = build("calendar", "v3", credentials=creds)


def _to_tz(dt: datetime) -> datetime:
    """Ensure dt is timezone-aware and in shop TZ."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def _count_overlapping_events(start: datetime, end: datetime) -> int:
    """Counts events that overlap [start, end)."""
    start = _to_tz(start)
    end = _to_tz(end)

    resp = service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=start.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=50,
    ).execute()

    return len(resp.get("items", []))


# ----------------------------
# Public helpers used by bot
# ----------------------------
def is_time_available(when: datetime, duration_mins: int = DEFAULT_SERVICE_MINUTES) -> bool:
    """Returns True if slot has remaining capacity (< MAX_BARBERS)."""
    when = _to_tz(when)
    end = when + timedelta(minutes=duration_mins)

    existing = _count_overlapping_events(when, end)
    return existing < MAX_BARBERS


def next_available_slots(
    start_from: datetime,
    duration_mins: int = DEFAULT_SERVICE_MINUTES,
    count: int = 4,
    window_hours: int = 8,
):
    """
    Returns a list of the next available slot datetimes.
    Searches forward in SLOT_STEP_MINUTES increments for up to window_hours.
    """
    start_from = _to_tz(start_from)

    slots = []
    cursor = start_from
    end_search = start_from + timedelta(hours=window_hours)

    while cursor <= end_search and len(slots) < count:
        if is_time_available(cursor, duration_mins):
            slots.append(cursor)
        cursor += timedelta(minutes=SLOT_STEP_MINUTES)

    return slots


def create_booking_event(service_name: str, when: datetime, name="Customer", phone=""):
    """
    Creates booking ONLY if available.
    Returns: (ok: bool, msg: str, link: str|None)
    """
    when = _to_tz(when)

    if not is_time_available(when, DEFAULT_SERVICE_MINUTES):
        return False, "❌ Sorry, that time is already fully booked.", None

    end = when + timedelta(minutes=DEFAULT_SERVICE_MINUTES)

    event = {
        "summary": f"{service_name} - {name}",
        "description": f"Phone: {phone}",
        "start": {"dateTime": when.isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": end.isoformat(), "timeZone": TIMEZONE},
    }

    created = service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
    return True, "✅ Booking confirmed!", created.get("htmlLink")


def delete_booking_event(event_id: str):
    """Optional: delete an event by ID (only needed if your bot supports cancellations)."""
    service.events().delete(calendarId=CALENDAR_ID, eventId=event_id).execute()
    return True
