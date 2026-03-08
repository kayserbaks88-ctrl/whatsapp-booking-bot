import dateparser


def llm_extract(message):

    dt = dateparser.parse(
        message,
        settings={"PREFER_DATES_FROM": "future"}
    )

    return {
        "datetime": dt
    }