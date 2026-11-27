"""Opt-in target editor for a full checkout. No scanner/arming command is exposed."""

import json
import subprocess
import sys
import threading
from pathlib import Path

import yaml

from bot.config.loader import load_yaml
from bot.hull.config import HullConfig
from bot.hull.target_settings import (
    TargetSettingsError,
    read_settings,
    revision,
    save_changes,
    sidecar,
)


class TargetPanel:
    def __init__(self, config):
        self.config = Path(config).resolve(strict=True)
        if not sidecar(self.config):
            raise TargetSettingsError(
                "Target editor requires an existing private *.local.yaml."
            )
        self.busy = threading.Lock()

    def _settings(self):
        before = self.config.read_bytes()
        try:
            hull = HullConfig.model_validate(load_yaml(self.config)["hull"])
            values = read_settings(self.config, hull.symbols)
            rev = revision(self.config, values)
        except (KeyError, ValueError, yaml.YAMLError) as exc:
            raise TargetSettingsError(
                "Unreadable private config or target settings. No files changed."
            ) from exc
        if before != self.config.read_bytes():
            raise TargetSettingsError(
                "Private config changed during review. Reload settings."
            )
        return hull, values, rev

    def profiles(self):
        hull, values, rev = self._settings()
        return {
            "revision": rev,
            "profiles": [
                {
                    "symbol": key,
                    "lots": hull.symbols[key].lots,
                    "rate": hull.symbols[key].rate,
                    "yaml_base": hull.symbols[key].target_usd,
                    "override": values.get(key),
                    "effective_base": values.get(key, hull.symbols[key].target_usd),
                }
                for key in hull.enabled
            ],
        }

    def action(self, name, body, account_key, magic):
        if not self.busy.acquire(blocking=False):
            raise TargetSettingsError("A settings action is still in progress.")
        try:
            if not isinstance(body, dict) or set(body) != (
                {"revision", "changes"} if name == "save" else {"revision"}
            ):
                raise TargetSettingsError("Invalid target settings request.")
            hull, _, rev = self._settings()
            if rev != body["revision"]:
                raise TargetSettingsError(
                    "Settings changed. Reload and review the new values."
                )
            if hull.magic != magic:
                raise TargetSettingsError(
                    "Private config magic differs from this journal."
                )
            if name == "save":
                save_changes(
                    self.config, hull.symbols, hull.enabled, rev, body["changes"]
                )
                return self.profiles()
            if name != "preflight" or not account_key:
                raise TargetSettingsError(
                    "A verified live journal account is required for target preflight."
                )
            # Fixed read-only verb. No shell, browser-supplied arguments, raw config,
            # terminal credentials or unfiltered subprocess output on HTTP.
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "bot.cli",
                    "hull-preflight",
                    "--config",
                    str(self.config),
                    "--paired-confirm",
                ],
                cwd=self.config.parent,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=90,
                check=False,
            )
            if proc.returncode:
                raise TargetSettingsError(
                    "Preflight blocked. Inspect the read-only CLI preflight on the MT5 PC."
                )
            s = json.loads(proc.stdout)
            result = self.profiles()
            if (
                s["account_key"] != account_key
                or s["journal_magic"] != magic
                or s["account"] != "USD hedging demo"
                or s["mode"] != "READ ONLY"
                or result["revision"] != rev
            ):
                raise TargetSettingsError(
                    "Account, magic or settings changed; no preview accepted."
                )
            specs = {v["key"]: (broker, v) for broker, v in s["specs"].items()}
            if set(specs) != set(hull.enabled) or len(specs) != len(s["specs"]):
                raise TargetSettingsError(
                    "The preflight symbol selection differs from this journal setup."
                )
            for row in result["profiles"]:
                broker, spec = specs[row["symbol"]]
                if (spec["target_usd"], spec["lots"], spec["rate"]) != (
                    row["effective_base"],
                    row["lots"],
                    row["rate"],
                ):
                    raise TargetSettingsError(
                        "Effective profile differs from the reviewed values."
                    )
                row.update(
                    broker=broker,
                    quote_ms=(spec.get("preview_quote") or {}).get("ms"),
                    preview={
                        side: {
                            k: v
                            for k, v in spec["target_preview_by_side"][side].items()
                            if k in ("status", "initial_goal_usd", "entry_spread_usd")
                        }
                        for side in ("BUY", "SELL")
                    },
                )
            result["review"] = s["review_before_demo"]
            return result
        finally:
            self.busy.release()
