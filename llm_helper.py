import dateparser

SERVICES = {
    "haircut": ("Haircut", 18),
    "fade": ("Skin Fade", 22),
    "shape": ("Shape Up", 12),
    "beard": ("Beard Trim", 10),
    "towel": ("Hot Towel Shave", 25),
    "blow": ("Blow Dry", 20),
}

def detect_service(text):

    text = text.lower()

    for key in SERVICES:
        if key in text:
            return SERVICES[key]

    return None


def detect_time(text, timezone):

    dt = dateparser.parse(
        text,
        settings={
            "TIMEZONE": timezone,
            "TO_TIMEZONE": timezone,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
            "DATE_ORDER": "DMY"
        }
    )

    return dt


def llm_extract(text, timezone):

    service = detect_service(text)
    time = detect_time(text, timezone)

    return service, time