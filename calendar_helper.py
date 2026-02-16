import os
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build


TIMEZONE = os.getenv("TIMEZONE_HINT", "Europe/London")
TZ = ZoneInfo(TIMEZONE)

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _get_creds():
    # Prefer env var JSON, else file credentials.json
    env_json = os.getenv("GOOGLE_CREDENTIALS_JSON") or os.getenv("GOOGLE_CREDENTIALS")
    if env_json:
        info = json.loads(env_json)
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)

    path = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
    return service_account.Credentials.from_service_account_file(path, scopes=SCOPES)


def _svc():
    creds = _get_creds()
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def is_time_available(calendar_id: str, start_dt: datetime, end_dt: datetime) -> bool:
    service = _svc()
    body = {
        "timeMin": start_dt.isoformat(),
        "timeMax": end_dt.isoformat(),
        "items": [{"id": calendar_id}],
    }
    fb = service.freebusy().query(body=body).execute()
    busy = fb["calendars"][calendar_id]["busy"]
    return len(busy) == 0


def next_available_slots(calendar_id: str, start_dt: datetime, duration_min: int, step_minutes: int = 15, limit: int = 3):
    out = []
    cursor = start_dt
    # search up to ~3 days ahead
    end_search = start_dt + timedelta(days=3)

    while cursor < end_search and len(out) < limit:
        end_dt = cursor + timedelta(minutes=duration_min)
        if is_time_available(calendar_id, cursor, end_dt):
            out.append(cursor)
        cursor = cursor + timedelta(minutes=step_minutes)

    return out


def create_booking_event(
    calendar_id: str,
    start_dt: datetime,
    end_dt: datetime,
    summary: str,
    description: str = "",
    phone: str | None = None,
    customer_name: str | None = None,
):
    """
    NOTE: phone/customer_name are optional and safe.
    (Fixes your: unexpected keyword argument 'phone')
    """
    service = _svc()

    extra = []
    if customer_name:
        extra.append(f"Name: {customer_name}")
    if phone:
        extra.append(f"Phone: {phone}")
    if extra:
        description = (description + "\n" + "\n".join(extra)).strip()

    event = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": TIMEZONE},
    }

    return service.events().insert(calendarId=calendar_id, body=event).execute()
