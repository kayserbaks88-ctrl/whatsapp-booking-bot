import os
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import List, Optional, Tuple

from google.oauth2 import service_account
from googleapiclient.discovery import build


def _get_tz() -> ZoneInfo:
    tz_name = os.getenv("TIMEZONE_HINT", "Europe/London")
    return ZoneInfo(tz_name)


def _get_calendar_id() -> str:
    return os.getenv("GOOGLE_CALENDAR_ID", "primary")


def _load_service_account_info() -> dict:
    """
    Supports either:
      - GOOGLE_CREDENTIALS_JSON (full JSON string)
      - GOOGLE_CREDENTIALS_FILE (path to json file)
      - credentials.json in project root (fallback)
    """
    raw = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
    if raw:
        # It might be a JSON string
        return json.loads(raw)

    path = os.getenv("GOOGLE_CREDENTIALS_FILE", "").strip()
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    # fallback
    if os.path.exists("credentials.json"):
        with open("credentials.json", "r", encoding="utf-8") as f:
            return json.load(f)

    raise RuntimeError(
        "Google credentials missing. Set GOOGLE_CREDENTIALS_JSON or GOOGLE_CREDENTIALS_FILE "
        "or add credentials.json."
    )


def _get_service():
    info = _load_service_account_info()
    scopes = ["https://www.googleapis.com/auth/calendar"]
    creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _to_rfc3339(dt: datetime) -> str:
    # Ensure tz-aware
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_get_tz())
    return dt.isoformat()


def list_events_between(calendar_id: str, start_dt: datetime, end_dt: datetime) -> list:
    svc = _get_service()
    resp = svc.events().list(
        calendarId=calendar_id,
        timeMin=_to_rfc3339(start_dt),
        timeMax=_to_rfc3339(end_dt),
        singleEvents=True,
        orderBy="startTime",
        maxResults=250,
    ).execute()
    return resp.get("items", [])


def has_overlap(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    return start_a < end_b and start_b < end_a


def is_time_available(start_dt: datetime, minutes: int, calendar_id: Optional[str] = None) -> bool:
    cal_id = calendar_id or _get_calendar_id()
    end_dt = start_dt + timedelta(minutes=minutes)
    events = list_events_between(cal_id, start_dt - timedelta(minutes=1), end_dt + timedelta(minutes=1))

    for ev in events:
        s = ev.get("start", {}).get("dateTime")
        e = ev.get("end", {}).get("dateTime")
        if not s or not e:
            continue
        sdt = datetime.fromisoformat(s)
        edt = datetime.fromisoformat(e)
        if has_overlap(start_dt, end_dt, sdt, edt):
            return False

    return True


def next_available_slots(
    desired_dt: datetime,
    minutes: int,
    slot_step_minutes: int = 15,
    max_slots: int = 3,
    search_hours_ahead: int = 24,
    calendar_id: Optional[str] = None,
) -> List[datetime]:
    """
    Find next available slots starting at/after desired_dt, stepping by slot_step_minutes.
    Searches up to search_hours_ahead hours ahead.
    """
    cal_id = calendar_id or _get_calendar_id()
    slots: List[datetime] = []
    tz = _get_tz()

    cursor = desired_dt
    if cursor.tzinfo is None:
        cursor = cursor.replace(tzinfo=tz)

    end_search = cursor + timedelta(hours=search_hours_ahead)
    while cursor <= end_search and len(slots) < max_slots:
        if is_time_available(cursor, minutes, calendar_id=cal_id):
            slots.append(cursor)
        cursor = cursor + timedelta(minutes=slot_step_minutes)

    return slots


def create_booking_event(
    start_dt: datetime,
    end_dt: datetime,
    summary: str,
    description: str,
    from_number: str,
    service_name: str,
    calendar_id: Optional[str] = None,
) -> str:
    cal_id = calendar_id or _get_calendar_id()
    svc = _get_service()

    event_body = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": _to_rfc3339(start_dt)},
        "end": {"dateTime": _to_rfc3339(end_dt)},
        "extendedProperties": {
            "private": {
                "from_number": from_number,
                "service": service_name,
                "source": "trimtech_whatsapp",
            }
        },
    }

    created = svc.events().insert(calendarId=cal_id, body=event_body).execute()
    return created.get("id", "")