from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REPORT_PERIODS = {"morning", "evening"}
REPORT_STATES = {"draft", "approved", "sent"}
REPORT_SECTIONS = {"done", "plan", "in_progress", "blocker", "merge_requests"}
MAX_REPORT_ITEMS_PER_SECTION = 25
MAX_REPORT_OVERRIDES = 50
MAX_REPORT_ITEM_TEXT = 500
MAX_RENDERED_REPORT_CHARS = 20_000
MAX_REPORT_ACTOR_LENGTH = 120
MAX_REPORT_OWNER_LENGTH = 120
MAX_REPORT_VERSION = 1_000_000
DEFAULT_REPORT_TIMEZONE = "Asia/Jakarta"

_SECTION_LABELS = {
    "done_morning": "Done kemarin",
    "done_evening": "Done hari ini",
    "plan": "Plan hari ini",
    "in_progress": "On progress",
    "blocker": "Blocker",
    "merge_requests": "Create Merge Request",
}


def parse_timezone(value: str) -> ZoneInfo:
    name = value.strip() or DEFAULT_REPORT_TIMEZONE
    if len(name) > 64:
        raise ValueError("timezone exceeds allowed length")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("unknown timezone") from exc


def parse_report_date(value: str | None, timezone_name: str) -> date:
    zone = parse_timezone(timezone_name)
    if not value:
        return datetime.now(zone).date()
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("report_date must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError("report_date must use YYYY-MM-DD")
    return parsed


def utc_date_window(report_date: date, timezone_name: str) -> tuple[datetime, datetime]:
    zone = parse_timezone(timezone_name)
    start = datetime.combine(report_date, time.min, tzinfo=zone)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def local_date(value: str, timezone_name: str) -> date:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(parse_timezone(timezone_name)).date()


def clean_text(value: Any, limit: int = MAX_REPORT_ITEM_TEXT) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].rstrip()


def validate_period(period: str) -> str:
    normalized = period.strip().lower()
    if normalized not in REPORT_PERIODS:
        raise ValueError("period must be morning or evening")
    return normalized


def validate_overrides(overrides: dict[str, Any] | None) -> dict[str, list[dict[str, str]]]:
    value = overrides or {}
    if not isinstance(value, dict) or set(value) - {"include", "exclude"}:
        raise ValueError("overrides may contain only include and exclude")
    normalized: dict[str, list[dict[str, str]]] = {"include": [], "exclude": []}
    total = 0
    for action in ("include", "exclude"):
        entries = value.get(action, [])
        if not isinstance(entries, list):
            raise ValueError(f"overrides.{action} must be a list")
        for entry in entries:
            total += 1
            if total > MAX_REPORT_OVERRIDES:
                raise ValueError("overrides contain too many entries")
            if not isinstance(entry, dict):
                raise ValueError("override entries must be objects")
            allowed = (
                {"section", "task_ref", "note"}
                if action == "include"
                else {
                    "section",
                    "task_ref",
                }
            )
            if set(entry) - allowed:
                raise ValueError("override entry contains unsupported fields")
            section = clean_text(entry.get("section"), 40)
            task_ref = clean_text(entry.get("task_ref"), 240)
            if section not in REPORT_SECTIONS or not task_ref:
                raise ValueError("override requires a valid section and task_ref")
            item = {"section": section, "task_ref": task_ref}
            if action == "include":
                note = clean_text(entry.get("note"), MAX_REPORT_ITEM_TEXT)
                if note:
                    item["note"] = note
            normalized[action].append(item)
    return normalized


def render_report(
    owner: str,
    report_date: date,
    period: str,
    sections: dict[str, list[dict[str, Any]]],
) -> str:
    period = validate_period(period)
    heading = "Pagi" if period == "morning" else "Sore"
    display_date = f"{report_date.month}/{report_date.day}/{report_date.year}"
    lines = [f"Internal Status - {heading} ({display_date})"]
    ordered = [
        ("done", _SECTION_LABELS[f"done_{period}"]),
        *(([("plan", _SECTION_LABELS["plan"])]) if period == "morning" else []),
        ("in_progress", _SECTION_LABELS["in_progress"]),
        ("blocker", _SECTION_LABELS["blocker"]),
        *(([("merge_requests", _SECTION_LABELS["merge_requests"])]) if period == "evening" else []),
    ]
    for key, label in ordered:
        items = sections.get(key, [])[:MAX_REPORT_ITEMS_PER_SECTION]
        lines.extend(["", f"{label}:"])
        if not items:
            if key == "plan":
                lines.append("- Belum ada task baru yang di-assign hari ini")
            else:
                lines.append("- Tidak ada")
            continue
        for item in items:
            task_key = clean_text(item.get("task_key"), 80)
            title = clean_text(item.get("note") or item.get("title"), MAX_REPORT_ITEM_TEXT)
            prefix = f"{task_key} - " if task_key else ""
            url = clean_text(item.get("url"), 2_000)
            suffix = f" ({url})" if url else ""
            lines.append(f"- {prefix}{title}{suffix}"[:2_500].rstrip())
    rendered = "\n".join(lines)
    if len(rendered) > MAX_RENDERED_REPORT_CHARS:
        rendered = rendered[: MAX_RENDERED_REPORT_CHARS - 1].rstrip() + "…"
    return rendered
