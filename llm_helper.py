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

    for key in SERVICES:
        if key in text:
            return SERVICES[key]

    return None


def detect_datetime(text):
    dt = dateparser.parse(
        text,
        settings={"PREFER_DATES_FROM": "future"}
    )

    return dt


def llm_extract(text):

    service = detect_service(text)
    dt = detect_datetime(text)

    return {
        "service": service,
        "datetime": dt
    }