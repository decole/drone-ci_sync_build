"""Schedule calculation and management for repository poller workers."""
import json
import logging
from datetime import datetime, time, timezone
from typing import Dict, Any, Tuple, Optional
import zoneinfo

logger = logging.getLogger("drone_sync.schedule")

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

DAY_NAMES_RU = {
    "mon": "Понедельник",
    "tue": "Вторник",
    "wed": "Среда",
    "thu": "Четверг",
    "fri": "Пятница",
    "sat": "Суббота",
    "sun": "Воскресенье",
}

DAY_SHORT_RU = {
    "mon": "Пн",
    "tue": "Вт",
    "wed": "Ср",
    "thu": "Чт",
    "fri": "Пт",
    "sat": "Сб",
    "sun": "Вс",
}

TIMEZONE_CHOICES = [
    ("Europe/Moscow", "Москва, СПб (UTC+3)"),
    ("Europe/Kaliningrad", "Калининград (UTC+2)"),
    ("Europe/Samara", "Самара (UTC+4)"),
    ("Asia/Yekaterinburg", "Екатеринбург (UTC+5)"),
    ("Asia/Omsk", "Омск (UTC+6)"),
    ("Asia/Novosibirsk", "Новосибирск (UTC+7)"),
    ("Asia/Krasnoyarsk", "Красноярск (UTC+7)"),
    ("Asia/Irkutsk", "Иркутск (UTC+8)"),
    ("Asia/Yakutsk", "Якутск (UTC+9)"),
    ("Asia/Vladivostok", "Владивосток (UTC+10)"),
    ("Asia/Magadan", "Магадан (UTC+11)"),
    ("Asia/Kamchatka", "Камчатка (UTC+12)"),
    ("UTC", "UTC (Всемирное время)"),
    ("Europe/London", "Лондон (UTC+0 / +1)"),
    ("Europe/Berlin", "Берлин, Париж (UTC+1 / +2)"),
    ("America/New_York", "Нью-Йорк (UTC-5 / -4)"),
    ("America/Los_Angeles", "Лос-Анджелес (UTC-8 / -7)"),
]


def get_default_schedule() -> Dict[str, Dict[str, Any]]:
    """Return default week schedule: Mon-Fri 09:00-18:00, Sat-Sun disabled."""
    return {
        "mon": {"enabled": True, "start": "09:00", "end": "18:00"},
        "tue": {"enabled": True, "start": "09:00", "end": "18:00"},
        "wed": {"enabled": True, "start": "09:00", "end": "18:00"},
        "thu": {"enabled": True, "start": "09:00", "end": "18:00"},
        "fri": {"enabled": True, "start": "09:00", "end": "18:00"},
        "sat": {"enabled": False, "start": "09:00", "end": "18:00"},
        "sun": {"enabled": False, "start": "09:00", "end": "18:00"},
    }


def parse_time_string(val: str, default: str = "00:00") -> time:
    """Parse 'HH:MM' string to datetime.time."""
    if not val or not isinstance(val, str):
        val = default
    try:
        parts = val.strip().split(":")
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        return time(max(0, min(23, h)), max(0, min(59, m)))
    except Exception:
        parts = default.split(":")
        return time(int(parts[0]), int(parts[1]))


def is_time_in_range(start_str: str, end_str: str, current_time: time) -> bool:
    """
    Check if current_time falls between start_time and end_time.
    Supports:
    - start == end: 24h active (whole day)
    - start < end: daytime window (e.g. 09:00 -> 18:00)
    - start > end: overnight window crossing midnight (e.g. 22:00 -> 06:00)
    """
    start_t = parse_time_string(start_str, "00:00")
    end_t = parse_time_string(end_str, "23:59")

    if start_t == end_t:
        # Full day
        return True

    if start_t < end_t:
        return start_t <= current_time <= end_t
    else:
        # Overnight range: e.g. 22:00 to 06:00
        return current_time >= start_t or current_time <= end_t


def normalize_schedule_dict(raw: Any) -> Dict[str, Dict[str, Any]]:
    """Validate and normalize a raw schedule dictionary."""
    defaults = get_default_schedule()
    if not isinstance(raw, dict):
        return defaults

    result = {}
    for day in DAY_KEYS:
        day_raw = raw.get(day)
        if isinstance(day_raw, dict):
            enabled = bool(day_raw.get("enabled", False))
            start = str(day_raw.get("start", "09:00")).strip()
            end = str(day_raw.get("end", "18:00")).strip()
            # Validate time format
            try:
                parse_time_string(start)
            except Exception:
                start = "09:00"
            try:
                parse_time_string(end)
            except Exception:
                end = "18:00"
            result[day] = {
                "enabled": enabled,
                "start": start,
                "end": end,
            }
        else:
            result[day] = defaults[day]
    return result


def get_timezone_obj(tz_name: Optional[str]) -> zoneinfo.ZoneInfo:
    """Safely get ZoneInfo, fallback to Europe/Moscow or UTC."""
    if not tz_name:
        tz_name = "Europe/Moscow"
    try:
        return zoneinfo.ZoneInfo(tz_name)
    except Exception:
        try:
            return zoneinfo.ZoneInfo("Europe/Moscow")
        except Exception:
            return zoneinfo.ZoneInfo("UTC")


def check_schedule_window(
    schedule: Dict[str, Dict[str, Any]],
    tz_name: str,
    check_dt: Optional[datetime] = None
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Check if the given schedule allows polling at check_dt (or now).
    Returns (is_allowed, reason_message, info_dict).
    """
    tz = get_timezone_obj(tz_name)
    if check_dt is None:
        local_now = datetime.now(tz)
    else:
        if check_dt.tzinfo is None:
            # Assume UTC if naive
            check_dt = check_dt.replace(tzinfo=timezone.utc)
        local_now = check_dt.astimezone(tz)

    weekday_idx = local_now.weekday()  # 0 = Monday, 6 = Sunday
    day_key = DAY_KEYS[weekday_idx]
    day_name_ru = DAY_NAMES_RU[day_key]
    day_short_ru = DAY_SHORT_RU[day_key]

    day_config = schedule.get(day_key, {"enabled": False, "start": "09:00", "end": "18:00"})
    is_day_enabled = bool(day_config.get("enabled", False))
    start_time_str = day_config.get("start", "09:00")
    end_time_str = day_config.get("end", "18:00")

    curr_time = local_now.time()
    curr_time_str = curr_time.strftime("%H:%M")

    info = {
        "day_key": day_key,
        "day_name": day_name_ru,
        "day_short": day_short_ru,
        "current_time": curr_time_str,
        "timezone": tz_name,
        "day_enabled": is_day_enabled,
        "start": start_time_str,
        "end": end_time_str,
    }

    if not is_day_enabled:
        return (
            False,
            f"Опрос отключен в {day_short_ru} ({day_name_ru})",
            info
        )

    in_range = is_time_in_range(start_time_str, end_time_str, curr_time)
    if in_range:
        return (
            True,
            f"В рабочем интервале ({day_short_ru} {start_time_str}–{end_time_str} {tz_name})",
            info
        )
    else:
        return (
            False,
            f"Вне рабочего интервала ({curr_time_str} вне {start_time_str}–{end_time_str} {day_short_ru})",
            info
        )


def is_repo_in_schedule_window(
    repo,
    db,
    check_dt: Optional[datetime] = None
) -> Tuple[bool, str]:
    """
    Determine if a repository is currently permitted to be polled based on:
    - repo.schedule_mode ("always", "global", "custom")
    - Global schedule settings (if mode == "global")
    - Custom schedule settings (if mode == "custom")
    """
    mode = getattr(repo, "schedule_mode", "global") or "global"

    if mode == "always":
        return True, "Режим 24/7 (всегда активно)"

    if mode == "custom":
        custom_tz = getattr(repo, "custom_schedule_timezone", "Europe/Moscow") or "Europe/Moscow"
        custom_json = getattr(repo, "custom_schedule_json", None)
        try:
            raw_sched = json.loads(custom_json) if custom_json else get_default_schedule()
        except Exception:
            raw_sched = get_default_schedule()
        schedule = normalize_schedule_dict(raw_sched)
        allowed, reason, _ = check_schedule_window(schedule, custom_tz, check_dt)
        return allowed, f"Индивидуальное: {reason}"

    # Default: "global"
    from app.models import get_worker_schedule_settings
    global_cfg = get_worker_schedule_settings(db)
    if not global_cfg.enabled:
        return True, "Глобальное расписание выключено (24/7)"

    global_tz = global_cfg.timezone or "Europe/Moscow"
    global_sched = global_cfg.get_schedule()
    allowed, reason, _ = check_schedule_window(global_sched, global_tz, check_dt)
    return allowed, f"Глобальное: {reason}"
