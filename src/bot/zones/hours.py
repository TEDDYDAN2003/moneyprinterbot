from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo


def _inside(now, lo, hi):
    return lo <= now < hi if lo < hi else now >= lo or now < hi


class Hours:
    """Mirror configured broker-local weekend/maintenance and UTC entry blackout.

    Scheduled exits are attempted on the next executable quote, never on a
    synthetic pre-gap close. Verify hours with the broker for all enabled symbols.
    """

    def __init__(self, cfg):
        run, pilot, sessions = (cfg.get(k, {}) for k in ("run", "pilot", "sessions"))
        self.tz = ZoneInfo(run.get("market_close_timezone", "UTC"))
        self.close = time.fromisoformat(pilot.get("daily_flatten_time", "23:45"))
        self.reopen = time.fromisoformat(pilot.get("daily_reopen_time", "01:05"))
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
