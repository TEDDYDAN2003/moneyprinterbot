from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo


def _inside(now, lo, hi):
    return lo <= now < hi if lo < hi else now >= lo or now < hi


def effective_sessions(cfg):
    # Pre-cleanup private configs kept these *entry blackout* bounds under pilot.
    # Honor actual overrides; do not silently widen hours on an upgrade.
    s = dict(cfg.get("sessions", {}))
    old = cfg.get("pilot", {})
    for key, previous, default in (
        ("maintenance_start", "daily_flatten_time", "23:45"),
        ("maintenance_end", "daily_reopen_time", "01:05"),
    ):
        if (
            key in s
            and previous in old
            and time.fromisoformat(s[key]) != time.fromisoformat(old[previous])
        ):
            raise ValueError(f"Conflicting sessions.{key} and legacy pilot.{previous}")
        s.setdefault(key, old.get(previous, default))
    return s


class Hours:
    """Additional broker maintenance, weekend and optional entry blackouts. Never an exit."""

    def __init__(self, cfg):
        run, sessions = cfg.get("run", {}), effective_sessions(cfg)
        self.tz = ZoneInfo(run.get("market_close_timezone", "UTC"))
        self.close = time.fromisoformat(sessions["maintenance_start"])
        self.reopen = time.fromisoformat(sessions["maintenance_end"])
        blackout = sessions.get("rollover_blackout")
        self.blackout = self._window(blackout) if blackout else None
        self.allowed = [
            self._window(sessions["killzones"][name])
            for name in sessions.get("trade_only_in", [])
        ]

    @staticmethod
    def _window(v):
        return (
            ZoneInfo(v.get("tz", "UTC")),
            time.fromisoformat(v["start"]),
            time.fromisoformat(v["end"]),
        )

    def status(self, sec):
        utc = datetime.fromtimestamp(sec, UTC)
        local = utc.astimezone(self.tz)
        closed = local.weekday() >= 5 or _inside(local.time(), self.close, self.reopen)
        if closed:
            return "scheduled_break", True

        def active(w):
            return _inside(utc.astimezone(w[0]).time(), w[1], w[2])

        if self.blackout and active(self.blackout):
            return "rollover_blackout", False
        if self.allowed and not any(active(w) for w in self.allowed):
            return "outside_session", False
        return "", False
