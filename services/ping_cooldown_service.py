from __future__ import annotations

import time
from dataclasses import dataclass

try:
    from config import PING_COOLDOWN_MINUTES
except ImportError:
    try:
        from config import ping_cooldown_minutes as PING_COOLDOWN_MINUTES
    except ImportError:
        PING_COOLDOWN_MINUTES = 15


@dataclass(frozen=True)
class CooldownResult:
    allowed: bool
    remaining_seconds: int
    cooldown_seconds: int


# Bot-local memory only. This resets whenever the bot restarts.
_LAST_USED_AT: dict[str, int] = {}


def now_ts() -> int:
    return int(time.time())


def cooldown_seconds() -> int:
    try:
        minutes = float(PING_COOLDOWN_MINUTES)
    except (TypeError, ValueError):
        minutes = 15

    return max(0, int(minutes * 60))


def format_cooldown(seconds: int) -> str:
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)

    if minutes and seconds:
        return f"{minutes}m {seconds:02d}s"

    if minutes:
        return f"{minutes}m"

    return f"{seconds}s"


def check_ping_cooldown(cooldown_key: str) -> CooldownResult:
    limit = cooldown_seconds()

    if limit <= 0:
        return CooldownResult(True, 0, 0)

    current = now_ts()
    last_used_at = int(_LAST_USED_AT.get(str(cooldown_key), 0))
    remaining = (last_used_at + limit) - current

    if remaining <= 0:
        return CooldownResult(True, 0, limit)

    return CooldownResult(False, int(remaining), limit)


def touch_ping_cooldown(cooldown_key: str) -> None:
    _LAST_USED_AT[str(cooldown_key)] = now_ts()


def try_ping_cooldown(cooldown_key: str) -> CooldownResult:
    result = check_ping_cooldown(cooldown_key)

    if result.allowed:
        touch_ping_cooldown(cooldown_key)

    return result


def clear_expired_ping_cooldowns() -> None:
    limit = cooldown_seconds()

    if limit <= 0:
        _LAST_USED_AT.clear()
        return

    cutoff = now_ts() - limit

    for key, last_used_at in list(_LAST_USED_AT.items()):
        if int(last_used_at) <= cutoff:
            _LAST_USED_AT.pop(key, None)
