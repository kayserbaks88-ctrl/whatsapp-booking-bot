import os
import json
from datetime import datetime, timedelta
from typing import Dict, Any, List

from google.oauth2 import service_account
from googleapiclient.discovery import build


TIMEZONE_HINT = os.getenv("TIMEZONE_HINT", "Europe/London")
GOOGLE_CALENDAR_ID = (os.getenv("GOOGLE_CALENDAR_ID") or "").strip()
MAX_BARBERS = int(os.getenv("MAX_BARBERS", "1"))

SA_JSON = (os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip()

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def get_calendar_service():

    if not SA_JSON:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON env variable missing")

    info = json.loads(SA_JSON)

    credentials = service_account.Credentials.from_service_account_info(
        info,
        scopes=SCOPES
    )

    service = build("calendar", "v3", credentials=credentials)

    return service


def is_free(start_dt: datetime, minutes: int = 30):

    service = get_calendar_service()

    end_dt = start_dt + timedelta(minutes=minutes)

    events_result = service.events().list(
        calendarId=GOOGLE_CALENDAR_ID,
        timeMin=start_dt.isoformat(),
        timeMax=end_dt.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    events = events_result.get("items", [])

    return len(events) < MAX_BARBERS


def create_booking(phone: str, service_name: str, start_dt: datetime, minutes: int = 30, name: str = None) -> Dict[str, Any]:

    service = get_calendar_service()

    end_dt = start_dt + timedelta(minutes=minutes)

    customer = name if name else phone

    events_result = service.events().list(
        calendarId=GOOGLE_CALENDAR_ID,
        timeMin=start_dt.isoformat(),
        timeMax=end_dt.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    events = events_result.get("items", [])

    if len(events) >= MAX_BARBERS:
        return {
            "status": "full"
        }

    event = {
        "summary": f"{service_name} | {customer}",
        "description": f"Customer phone: {phone}",
        "start": {
            "dateTime": start_dt.isoformat(),
            "timeZone": TIMEZONE_HINT,
        },
        "end": {
            "dateTime": end_dt.isoformat(),
            "timeZone": TIMEZONE_HINT,
        },
    }

    created_event = service.events().insert(
        calendarId=GOOGLE_CALENDAR_ID,
        body=event
    ).execute()

    return {
        "status": "confirmed",
        "event_id": created_event["id"],
        "start": start_dt,
        "end": end_dt
    }


def cancel_booking(event_id):

    service = get_calendar_service()

    service.events().delete(
        calendarId=GOOGLE_CALENDAR_ID,
        eventId=event_id
    ).execute()

    return {"status": "cancelled"}


def list_upcoming(limit=10):

    service = get_calendar_service()

    now = datetime.utcnow().isoformat() + "Z"

    events_result = service.events().list(
        calendarId=GOOGLE_CALENDAR_ID,
        timeMin=now,
        maxResults=limit,
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    return events_result.get("items", [])


def next_available_slots(start_dt, minutes=30, limit=3):

    slots = []
    current = start_dt

    while len(slots) < limit:

        if is_free(current, minutes):
            slots.append(current)

        current = current + timedelta(minutes=15)

    return slots