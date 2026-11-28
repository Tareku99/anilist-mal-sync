"""Preserve watch dates at the precision both services support."""

from calendar import monthrange
from datetime import date

DATE_FIELDS = ("start_date", "finish_date")


def normalize_watch_date(value: dict | str | None) -> str | None:
    """Return YYYY, YYYY-MM, or YYYY-MM-DD without inventing missing parts."""
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        parts = [value.get(key) for key in ("year", "month", "day")]
        if any(part is not None and type(part) is not int for part in parts):
            raise ValueError("Invalid watch date components")
    elif isinstance(value, str):
        components = value.split("-")
        if len(components) > 3 or not all(part.isascii() and part.isdigit() for part in components):
            raise ValueError("Invalid watch date format")
        parts = [int(part) for part in components] + [None] * (3 - len(components))
    else:
        raise ValueError("Invalid watch date")
    year, month, day = parts
    if not any(parts):
        return None
    if not year or (day and not month):
        raise ValueError("A date without a preceding year or month cannot round-trip")
    date(year, month or 1, day or 1)
    return "-".join(f"{part:0{width}d}" for part, width in zip(parts, (4, 2, 2)) if part)


def anilist_watch_date(value: str | None) -> dict[str, int | None]:
    parts = [int(part) for part in value.split("-")] if value else []
    return dict(zip(("year", "month", "day"), parts + [None] * (3 - len(parts))))


def date_order_is_possible(start: str, finish: str) -> bool:
    """Reject only when even the earliest start is after the latest finish."""
    first = anilist_watch_date(start)
    last = anilist_watch_date(finish)
    last_month = last["month"] or 12
    return date(first["year"], first["month"] or 1, first["day"] or 1) <= date(
        last["year"], last_month, last["day"] or monthrange(last["year"], last_month)[1],
    )


def read_watch_dates(payload: dict, keys: tuple[str, str], *, keys_required: bool = False):
    values = {}
    unavailable = set()
    for field, key in zip(DATE_FIELDS, keys):
        try:
            if keys_required and key not in payload:
                raise ValueError("Date field missing from response")
            values[field] = normalize_watch_date(payload.get(key))
        except ValueError:
            unavailable.add(field)
    return values, unavailable
