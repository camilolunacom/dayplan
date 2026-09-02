"""Date helpers. Everything the app stores is a local YYYY-MM-DD string."""

from __future__ import annotations

from datetime import date, datetime, timedelta


def today_str() -> str:
    return date.today().isoformat()


def parse_day(value: str | None) -> str:
    """Accept 'today', 'tomorrow', 'yesterday', '+3', '-1' or YYYY-MM-DD."""
    if not value or value == "today":
        return today_str()
    value = value.strip().lower()
    if value == "tomorrow":
        return (date.today() + timedelta(days=1)).isoformat()
    if value == "yesterday":
        return (date.today() - timedelta(days=1)).isoformat()
    if value.startswith(("+", "-")):
        try:
            return (date.today() + timedelta(days=int(value))).isoformat()
        except ValueError:
            pass
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"cannot read a date from {value!r}") from exc


def iso_to_local_day(value: str | None, all_day: bool = False) -> str | None:
    """Turn a provider timestamp into a local YYYY-MM-DD, or None.

    All-day items keep the date exactly as sent: converting them to the local
    timezone is what makes a task due 'today' show up as due 'yesterday'.
    """
    if not value:
        return None
    text = value.strip()
    if len(text) == 10:  # already a plain date
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            return None
    normalized = text.replace("Z", "+00:00")
    # TickTick sends "+0000" rather than "+00:00"
    if len(normalized) > 5 and normalized[-5] in "+-" and normalized[-3] != ":":
        normalized = normalized[:-2] + ":" + normalized[-2:]
    try:
        stamp = datetime.fromisoformat(normalized)
    except ValueError:
        return text[:10] if len(text) >= 10 else None
    if all_day or stamp.tzinfo is None:
        return stamp.date().isoformat()
    return stamp.astimezone().date().isoformat()


def days_from_today(day: str | None) -> int | None:
    if not day:
        return None
    try:
        return (date.fromisoformat(day) - date.today()).days
    except ValueError:
        return None
