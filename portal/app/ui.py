"""Шаблоны, фильтры и короткие сообщения интерфейса."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

FLASH_MESSAGES: dict[str, tuple[str, str]] = {
    "revoked": ("ok", "Ключ отозван."),
    "logout": ("ok", "Вы вышли из системы."),
}


def build_templates(timezone_name: str) -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    tz = ZoneInfo(timezone_name)

    def to_local(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(tz)

    def fmt_dt(value: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
        local = to_local(value)
        return local.strftime(fmt) if local else "—"

    def fmt_duration(hours: int | None) -> str:
        if not hours:
            return "—"
        if hours < 24:
            return f"{hours} ч"
        days, rest = divmod(hours, 24)
        return f"{days} дн" + (f" {rest} ч" if rest else "")

    def flash(request: Request) -> tuple[str, str] | None:
        raw = request.query_params.get("msg")
        return FLASH_MESSAGES.get(unquote(raw)) if raw else None

    def fmt_num(value: object) -> str:
        try:
            return f"{int(value):,}".replace(",", "\u00a0")
        except (TypeError, ValueError):
            return "—"

    def fmt_money(value: object) -> str:
        try:
            return "$" + f"{float(value):,.2f}".replace(",", "\u00a0")
        except (TypeError, ValueError):
            return "—"

    templates.env.filters["dt"] = fmt_dt
    templates.env.filters["money"] = fmt_money
    templates.env.filters["num"] = fmt_num
    templates.env.filters["duration"] = fmt_duration
    templates.env.globals["flash"] = flash
    templates.env.globals["now"] = lambda: to_local(datetime.now(timezone.utc))
    return templates
