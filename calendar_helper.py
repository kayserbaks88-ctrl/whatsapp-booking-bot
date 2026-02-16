import os
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build
from dotenv import load_dotenv

load_dotenv()

TIMEZONE = os.getenv("TIMEZONE_HINT", "Europe/London")
TZ = ZoneInfo(TIMEZONE)

SCOPES = ["https://www.googleapis.com/auth/calendar"]

def _get_calendar_service():
    """
    Supports:
    - GOOGLE_CREDENTIALS_JSON = full service account json text (recommended on Render)
    OR
    - credentials.json file in project (local)
    """
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON") or os.getenv("GOOGLE_CREDENTIALS")
    if creds_json:
        info = json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds = service_account.Credentials.from_service_account_file("credentials.json", scopes=SCOPES)

    return build("calendar", "v3", credentials=creds, cache_discovery=False)

def is_time_available(calendar_id: str, start_dt: datetime, end_dt: datetime) -> bool:
    service = _get_calendar_service()

    events = (
        service.events()
        .list(
            calendarId=calendar_id,
            timeMin=start_dt.isoformat(),
            timeMax=end_dt.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
        .get("items", [])
    )

    # if any event overlaps in the window -> not available
    return len(events) == 0

def next_available_slots(calendar_id: str, start_dt: datetime, duration_minutes: int, step_minutes: int = 15, limit: int = 3):
    """
    Find the next available slots starting from start_dt.
    """
    slots = []
    cursor = start_dt

    # search up to ~3 days ahead (plenty for your flow)
    for _ in range(int((3 * 24 * 60) / step_minutes)):
        end_dt = cursor + timedelta(minutes=duration_minutes)
        if is_time_available(calendar_id, cursor, end_dt):
            slots.append(cursor)
            if len(slots) >= limit:
                break
        cursor = cursor + timedelta(minutes=step_minutes)

    return slots

def create_booking_event(
    calendar_id: str,
    start_dt: datetime,
    end_dt: datetime,
    service_name: str,
    customer_name: str = None,
    phone: str = None,
    **kwargs,
):
    """
    Accepts phone (fixes your earlier crash) + ignores extra kwargs safely.
    """
    service = _get_calendar_service()

    title = f"{service_name}"
    if customer_name:
        title += f" - {customer_name}"

    description_parts = []
    if phone:
        description_parts.append(f"Phone: {phone}")
    if kwargs:
        # keep any future fields without breaking
        for k, v in kwargs.items():
            description_parts.append(f"{k}: {v}")

    event = {
        "summary": title,
        "description": "\n".join(description_parts).strip(),
        "start": {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": TIMEZONE},
    }

    created = service.events().insert(calendarId=calendar_id, body=event).execute()
    return created.get("id")
