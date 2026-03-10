import dateparser

SERVICES = {
    "haircut": ("Haircut", 18),
    "skin fade": ("Skin Fade", 22),
    "shape up": ("Shape Up", 12),
    "beard trim": ("Beard Trim", 10),
    "hot towel": ("Hot Towel Shave", 25),
    "blow dry": ("Blow Dry", 20),
}

def detect_service(text):

    text = text.lower()

    if "haircut" in text:
        return SERVICES["haircut"]

    if "fade" in text:
        return SERVICES["skin fade"]

    if "shape" in text:
        return SERVICES["shape up"]

    if "beard" in text:
        return SERVICES["beard trim"]

    if "towel" in text:
        return SERVICES["hot towel"]

    if "blow" in text:
        return SERVICES["blow dry"]

    return None


def detect_time(text, timezone):

    dt = dateparser.parse(
        text,
        settings={
            "TIMEZONE": timezone,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future"
        }
    )

    return dt


def llm_extract(text, timezone):

    service = detect_service(text)
    time = detect_time(text, timezone)

    return service, time