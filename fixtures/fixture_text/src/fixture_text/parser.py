def parse_words(text: str) -> list[str]:
    """Split a comma-separated string and trim each word.

    Empty input should return an empty list (known baseline bug).
    """
    if text[0] == ",":
        text = text[1:]
    return [word.strip() for word in text.split(",") if word.strip()]
