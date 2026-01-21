import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

# Google API imports
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

TIMEZONE = os.getenv("TIMEZONE_HINT", "Europe/London")
DEFAULT_SERVICE_MINUTES = int(os.getenv("DEFAULT_SERVICE_MINUTES", "45"))

# Optional per-service durations (minutes)
SERVICE_DURATIONS = {
    "Haircut": int(os.getenv("DUR_HAIRCUT", "45")),
    "Skin Fade": int(os.getenv("DUR_SKIN_FADE", "45")),
    "Beard Trim": int(os.getenv("DUR_BEARD_TRIM", "30")),
    "Kids Cut": int(os.getenv("DUR_KIDS_CUT", "30")),
    "Shape Up": int(os.getenv("DUR_SHAPE_UP", "30")),
}

GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _get_calendar_service():
    if not os.path.exists(GOOGLE_CREDENTIALS_FILE):
        raise FileNotFoundError(
            f"Missing {GOOGLE_CREDENTIALS_FILE}. Put your service account json file there "
            f"or set GOOGLE_CREDENTIALS_FILE in .env"
        )

    creds = Credentials.from_service_account_file(
        GOOGLE_CREDENTIALS_FILE, scopes=SCOPES
    )
    return build("calendar", "v3", credentials=creds)


def create_booking_event(
    service_name: str,
    when: datetime,
    name: str = "Customer",
    phone: str = "",
    calendar_id: str = "primary",
    timezone_hint: str = TIMEZONE,
):
    """
    Returns: (ok: bool, message: str, link: str|None)
    """
    try:
        tz = ZoneInfo(timezone_hint)

        if when is None:
            return False, "Missing booking time.", None

        # Ensure tz-aware
        if when.tzinfo is None:
            when = when.replace(tzinfo=tz)
        else:
            when = when.astimezone(tz)

        minutes = SERVICE_DURATIONS.get(service_name, DEFAULT_SERVICE_MINUTES)
        end = when + timedelta(minutes=minutes)

        service = _get_calendar_service()

        summary = f"{service_name} - {name}"
        description_lines = [
            f"Service: {service_name}",
            f"Name: {name}",
        ]
        if phone:
            description_lines.append(f"WhatsApp: {phone}")

        event = {
            "summary": summary,
            "description": "\n".join(description_lines),
            "start": {"dateTime": when.isoformat(), "timeZone": timezone_hint},
            "end": {"dateTime": end.isoformat(), "timeZone": timezone_hint},
        }

        created = service.events().insert(calendarId=calendar_id, body=event).execute()
        link = created.get("htmlLink")

        return True, "Created", link

    except Exception as e:
        return False, str(e), None
