import os
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

TIMEZONE_HINT = os.getenv("TIMEZONE_HINT", "Europe/London").strip()
TZ = ZoneInfo(TIMEZONE_HINT)

GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "").strip()
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()

SCOPES = ["https://www.googleapis.com/auth/calendar"]

_service = None


def _calendar_service():
    global _service
    if _service is not None:
        return _service

    if not GOOGLE_CALENDAR_ID:
        raise RuntimeError("Missing GOOGLE_CALENDAR_ID")
    if not GOOGLE_CREDENTIALS_JSON:
        raise RuntimeError("Missing GOOGLE_CREDENTIALS_JSON")

    info = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    _service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return _service


def _to_rfc3339(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ).isoformat()


def is_time_available(start_dt: datetime, duration_min: int) -> bool:
    svc = _calendar_service()
    end_dt = start_dt + timedelta(minutes=duration_min)

    fb = svc.freebusy().query(
        body={
            "timeMin": _to_rfc3339(start_dt),
            "timeMax": _to_rfc3339(end_dt),
            "items": [{"id": GOOGLE_CALENDAR_ID}],
        }
    ).execute()

    busy = fb.get("calendars", {}).get(GOOGLE_CALENDAR_ID, {}).get("busy", [])
    return len(busy) == 0


def next_available_slots(start_dt: datetime, duration_min: int, step_min: int = 15, count: int = 5):
    slots = []
    cur = start_dt
    step = timedelta(minutes=step_min)
    limit = start_dt + timedelta(days=7)

    while cur < limit and len(slots) < count:
        if is_time_available(cur, duration_min):
            slots.append(cur)
        cur += step

    return slots


def create_booking_event(start_dt: datetime, duration_min: int, service_name: str, phone: str, price=None):
    svc = _calendar_service()
    end_dt = start_dt + timedelta(minutes=duration_min)

    summary = service_name
    if price is not None and price != 0:
        summary = f"{service_name} (£{price})"

    event = {
        "summary": summary,
        "start": {"dateTime": _to_rfc3339(start_dt), "timeZone": TIMEZONE_HINT},
        "end": {"dateTime": _to_rfc3339(end_dt), "timeZone": TIMEZONE_HINT},
        "extendedProperties": {"private": {"phone": phone}},
    }

    created = svc.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
    return created.get("id"), created.get("htmlLink")


def list_bookings_for_phone(phone: str, max_results: int = 20):
    svc = _calendar_service()
    now = datetime.now(TZ)
    time_min = _to_rfc3339(now)

    events = svc.events().list(
        calendarId=GOOGLE_CALENDAR_ID,
        timeMin=time_min,
        singleEvents=True,
        orderBy="startTime",
        maxResults=max_results,
    ).execute()

    out = []
    for e in events.get("items", []):
        priv = (e.get("extendedProperties") or {}).get("private") or {}
        if priv.get("phone") != phone:
            continue

        start = e.get("start", {}).get("dateTime")
        if not start:
            continue

        start_dt = datetime.fromisoformat(start).astimezone(TZ)
        out.append(
            {
                "event_id": e["id"],
                "summary": e.get("summary", "Booking"),
                "start_dt": start_dt,
                "htmlLink": e.get("htmlLink"),
            }
        )
    return out


def cancel_booking_by_index(phone: str, index_1based: int):
    bookings = list_bookings_for_phone(phone)
    if index_1based < 1 or index_1based > len(bookings):
        return False, "Invalid selection"

    eid = bookings[index_1based - 1]["event_id"]
    svc = _calendar_service()
    svc.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=eid).execute()
    return True, "Cancelled"


def reschedule_booking_by_index(phone: str, index_1based: int, new_start_dt: datetime, duration_min: int):
    bookings = list_bookings_for_phone(phone)
    if index_1based < 1 or index_1based > len(bookings):
        return False, "Invalid selection"

    eid = bookings[index_1based - 1]["event_id"]
    svc = _calendar_service()

    event = svc.events().get(calendarId=GOOGLE_CALENDAR_ID, eventId=eid).execute()
    new_end_dt = new_start_dt + timedelta(minutes=duration_min)

    event["start"] = {"dateTime": _to_rfc3339(new_start_dt), "timeZone": TIMEZONE_HINT}
    event["end"] = {"dateTime": _to_rfc3339(new_end_dt), "timeZone": TIMEZONE_HINT}

    updated = svc.events().update(calendarId=GOOGLE_CALENDAR_ID, eventId=eid, body=event).execute()
    return True, updated.get("htmlLink")