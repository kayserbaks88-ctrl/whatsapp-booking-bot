import os
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import List, Tuple, Dict

from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def get_service():
    raw = os.getenv("GOOGLE_CREDENTIALS_JSON")

    if not raw:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON not set in Render env")

    try:
        creds_info = json.loads(raw)
    except Exception as e:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON is not valid JSON: " + str(e))

    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)

    return build("calendar", "v3", credentials=creds, cache_discovery=False)


# ---------- AVAILABILITY CHECK (FREEBUSY) ----------

def get_busy(calendar_id: str, start: datetime, end: datetime):
    service = get_service()

    body = {
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "items": [{"id": calendar_id}],
    }

    result = service.freebusy().query(body=body).execute()

    busy = result["calendars"][calendar_id]["busy"]

    intervals = []
    for b in busy:
        s = datetime.fromisoformat(b["start"].replace("Z", "+00:00"))
        e = datetime.fromisoformat(b["end"].replace("Z", "+00:00"))
        intervals.append((s, e))

    return intervals


def overlaps(a_start, a_end, b_start, b_end):
    return a_start < b_end and a_end > b_start


def is_time_available(
    when: datetime,
    calendar_id: str,
    duration_minutes: int,
    timezone: str,
):
    tz = ZoneInfo(timezone)

    if when.tzinfo is None:
        when = when.replace(tzinfo=tz)
    else:
        when = when.astimezone(tz)

    end = when + timedelta(minutes=duration_minutes)

    busy = get_busy(
        calendar_id,
        when - timedelta(hours=6),
        end + timedelta(hours=6),
    )

    for b_start, b_end in busy:
        b_start = b_start.astimezone(tz)
        b_end = b_end.astimezone(tz)

        if overlaps(when, end, b_start, b_end):
            return False, f"Clashes with {b_start.strftime('%H:%M')}–{b_end.strftime('%H:%M')}"

    return True, "OK"


def next_available_slots(
    after_when: datetime,
    calendar_id: str,
    duration_minutes: int,
    timezone: str,
    step_minutes: int = 15,
    limit: int = 5,
):
    tz = ZoneInfo(timezone)

    cursor = after_when.astimezone(tz)

    found = []
    attempts = 0

    while len(found) < limit and attempts < 200:
        ok, _ = is_time_available(cursor, calendar_id, duration_minutes, timezone)

        if ok:
            found.append(cursor)

        cursor += timedelta(minutes=step_minutes)
        attempts += 1

    return found


# ---------- EVENT CREATION ----------

def create_booking_event(
    calendar_id: str,
    service_name: str,
    customer_name: str,
    start_dt: datetime,
    duration_minutes: int,
    phone: str,
    timezone: str,
):
    tz = ZoneInfo(timezone)

    start_dt = start_dt.astimezone(tz)
    end_dt = start_dt + timedelta(minutes=duration_minutes)

    body = {
        "summary": f"{service_name} — {customer_name}",
        "description": f"Customer: {customer_name}\nPhone: {phone}",
        "start": {
            "dateTime": start_dt.isoformat(),
            "timeZone": timezone,
        },
        "end": {
            "dateTime": end_dt.isoformat(),
            "timeZone": timezone,
        },
    }

    service = get_service()

    event = service.events().insert(
        calendarId=calendar_id,
        body=body
    ).execute()

    return {
        "event_id": event.get("id"),
        "html_link": event.get("htmlLink"),
    }


def delete_booking_event(calendar_id: str, event_id: str):
    service = get_service()

    service.events().delete(
        calendarId=calendar_id,
        eventId=event_id
    ).execute()

    return True
