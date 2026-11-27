"""Optional target-only companion to a private Hull YAML. Never rewrite that YAML.

No terminals, selections, orders, defaults or cross-account ticket state here.
Both the CLI and the opt-in Journal editor resolve the same path from --config.
"""

import hashlib
import json
import os
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile


class TargetSettingsError(ValueError):
    pass


def sidecar(config):
    path = Path(config)
    return (
        path.with_suffix(".targets.json") if path.name.endswith(".local.yaml") else None
    )


def validate_values(values, allowed, *, inherit=False):
    if not isinstance(values, dict) or values.keys() - set(allowed):
        raise TargetSettingsError(
            "Targets must name only existing configured symbol keys."
        )
    for key, value in values.items():
        if inherit and value is None:
            continue
        if type(value) not in (float, int) or not 0 < value <= sys.float_info.max:
            raise TargetSettingsError(
                f"{key}: enter a finite, positive USD base per ticket."
            )


def unique(pairs):
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise TargetSettingsError("Duplicate key in target settings.")
    return obj


def read_settings(config, symbols):
    file = sidecar(config)
    if file is None:
        return {}
    try:
        with file.open("rb") as stream:
            content = stream.read(65537)
    except FileNotFoundError:
        return {}
    if len(content) > 65536:
        raise TargetSettingsError("Target settings too large; file left untouched.")
    data = json.loads(content, object_pairs_hook=unique)
    if (
        not isinstance(data, dict)
        or set(data) != {"version", "targets"}
        or type(data["version"]) is not int
        or data["version"] != 1
    ):
        raise TargetSettingsError("Unrecognized target settings; file left untouched.")
    validate_values(data["targets"], symbols)
    return data["targets"]


def effective_config(path, raw):
    if sidecar(path) is None:
        return raw
    hull = raw.get("hull", {})
    symbols = hull.get("symbols", {})
    values = read_settings(path, symbols)
    if not values:
        return raw
    patched = dict(symbols)
    for key, value in values.items():
        patched[key] = symbols[key] | {"target_usd": value}
    return raw | {"hull": hull | {"symbols": patched}}


def revision(config, values):
    return hashlib.sha256(
        Path(config).read_bytes() + b"\0" + json.dumps(values, sort_keys=True).encode()
    ).hexdigest()


def save_changes(config, symbols, enabled, expected, changes):
    file = sidecar(config)
    if file is None:
        raise TargetSettingsError(
            "Choose a private *.local.yaml; never edit the shipped template."
        )
    validate_values(changes, enabled, inherit=True)
    lock = file.with_suffix(".json.lock")
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise TargetSettingsError(
            "Another settings writer has the file. Review it before retrying."
        ) from exc
    temp = None
    try:
        os.close(fd)
        values = read_settings(config, symbols)
        if expected != revision(config, values):
            raise TargetSettingsError(
                "Settings changed since review. Reload before saving."
            )
        for key, value in changes.items():
            if value is None:
                values.pop(key, None)
            else:
                values[key] = value
        with NamedTemporaryFile(
            "w", encoding="utf-8", dir=file.parent, prefix=file.name + ".", delete=False
        ) as handle:
            temp = Path(handle.name)
            json.dump(
                {"version": 1, "targets": values},
                handle,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, file)
    finally:
        if temp:
            temp.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)
