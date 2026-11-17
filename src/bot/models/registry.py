from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def dataset_hash(columns: list[str], row_count: int, config: dict[str, Any]) -> str:
    payload = json.dumps(
        {"columns": columns, "row_count": row_count, "config": config},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def write_model_metadata(path: str | Path, model_name: str, metrics: dict[str, float], data_hash: str) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "model_name": model_name,
        "data_hash": data_hash,
        "metrics": metrics,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    output.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
