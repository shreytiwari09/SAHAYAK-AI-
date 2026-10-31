"""Small helpers for turning internal names into words."""


def humanize(key: object) -> str:
    return str(key or "").lower().replace("_", " ").capitalize()
