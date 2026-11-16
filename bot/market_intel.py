from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class MarketIntelLogger:
    """Writes one structured JSON record per evaluated candle close."""

    def __init__(self, enabled: bool, path: str) -> None:
        self.enabled = enabled
        self.path = Path(path)

    def write(self, record: dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
        except OSError:
            logger.exception("Unable to write market-intel record to %s", self.path)
