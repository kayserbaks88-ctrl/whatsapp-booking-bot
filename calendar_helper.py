from datetime import datetime, timedelta
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
import os

SCOPES = ["https://www.googleapis.com/auth/calendar"]

creds = Credentials.from_service_account_file(
    "credentials.json",
    scopes=SCOPES
)

service = build("calendar", "v3", credentials=creds)

CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")


def is_free(start, end):

    events = service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=start.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True
    ).execute()

    return len(events.get("items", [])) == 0


def create_booking(name, service_name, start):

    end = start + timedelta(minutes=30)

    event = {
        "summary": f"{service_name} - {name}",
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()}
    }

    service.events().insert(
        calendarId=CALENDAR_ID,
        body=event
    ).execute()

    return True