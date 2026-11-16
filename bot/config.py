from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from bot.models import (
    BotConfig,
    DollarExitConfig,
    ExternalBridgeConfig,
    MT5Config,
    RiskConfig,
    SessionConfig,
    StrategyConfig,
    TelegramConfig,
)


def _load_dotenv_file(env_path: Path) -> None:
    """Loads .env key-value pairs using python-dotenv if installed, else a clean stdlib parser."""
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(env_path, override=False)
        return
    except ImportError:
        pass

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def _strip_yaml_comment(line: str) -> str:
    in_single = False
    in_double = False
    for idx, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            return line[:idx]
    return line


def _parse_yaml_scalar(raw: str) -> Any:
    val = raw.strip()
    if val in ("", "null", "None", "~"):
        return None
    if val.lower() == "true":
        return True
    if val.lower() == "false":
        return False
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        return val[1:-1]
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        return [_parse_yaml_scalar(item) for item in inner.split(",")]
    try:
        if "." in val:
            return float(val)
        return int(val)
    except ValueError:
        return val


def _fallback_parse_yaml(text: str) -> dict[str, Any]:
    """Minimal indentation-based YAML parser so config loading works even without PyYAML."""
    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]

    for raw_line in text.splitlines():
        cleaned = _strip_yaml_comment(raw_line).rstrip()
        if not cleaned.strip():
            continue
        indent = len(cleaned) - len(cleaned.lstrip(" "))
        stripped = cleaned.strip()

        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()

        parent = stack[-1][1]
        if stripped.startswith("- "):
            item_val = _parse_yaml_scalar(stripped[2:])
            if isinstance(parent, list):
                parent.append(item_val)
            continue

        if ":" in stripped:
            key, rest = stripped.split(":", 1)
            key = key.strip()
            rest = rest.strip()
            if rest == "":
                # Peek if next non-empty line is a list item or mapping; list handled lazily if needed
                container: Any = {}
                # Simple heuristic for allowed_utc_hours
                if key == "allowed_utc_hours":
                    container = []
                if isinstance(parent, dict):
                    parent[key] = container
                stack.append((indent, container))
            else:
                if isinstance(parent, dict):
                    parent[key] = _parse_yaml_scalar(rest)

    return root


def load_yaml_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(text)
        return loaded if isinstance(loaded, dict) else {}
    except ImportError:
        return _fallback_parse_yaml(text)


def dump_yaml_dict(data: dict[str, Any]) -> str:
    try:
        import yaml  # type: ignore

        return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    except ImportError:
        lines: list[str] = []
        for k, v in data.items():
            if isinstance(v, dict):
                lines.append(f"{k}:")
                for sk, sv in v.items():
                    lines.append(f"  {sk}: {sv}")
            else:
                lines.append(f"{k}: {v}")
        return "\n".join(lines) + "\n"


def _apply_section(target: Any, raw: dict[str, Any] | None) -> None:
    if not raw:
        return
    for key, value in raw.items():
        if hasattr(target, key) and value is not None:
            setattr(target, key, value)


def load_config(config_path: str | Path = "config.yaml", env_path: str | Path = ".env") -> BotConfig:
    _load_dotenv_file(Path(env_path))
    raw = load_yaml_file(Path(config_path))

    cfg = BotConfig()
    _apply_section(cfg.mt5, raw.get("mt5"))
    _apply_section(cfg.strategy, raw.get("strategy"))
    _apply_section(cfg.risk, raw.get("risk"))
    _apply_section(cfg.dollar_exits, raw.get("dollar_exits"))
    _apply_section(cfg.sessions, raw.get("sessions"))
    _apply_section(cfg.external_bridge, raw.get("external_bridge"))
    _apply_section(cfg.telegram, raw.get("telegram"))

    if cfg.sessions.allowed_utc_hours:
        cfg.sessions.allowed_utc_hours = [
            (int(item[0]), int(item[1])) for item in cfg.sessions.allowed_utc_hours
        ]

    # Environment overrides for secrets & terminal credentials
    if os.getenv("MT5_SYMBOL"):
        cfg.mt5.symbol = os.environ["MT5_SYMBOL"]
    if os.getenv("MT5_LOGIN"):
        try:
            cfg.mt5.login = int(os.environ["MT5_LOGIN"])
        except ValueError:
            pass
    if os.getenv("MT5_PASSWORD"):
        cfg.mt5.password = os.environ["MT5_PASSWORD"]
    if os.getenv("MT5_SERVER"):
        cfg.mt5.server = os.environ["MT5_SERVER"]
    if os.getenv("MT5_TERMINAL_PATH"):
        cfg.mt5.terminal_path = os.environ["MT5_TERMINAL_PATH"]

    if os.getenv("TELEGRAM_BOT_TOKEN"):
        cfg.telegram.bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    if os.getenv("TELEGRAM_ADMIN_CHAT_ID"):
        try:
            cfg.telegram.admin_chat_id = int(os.environ["TELEGRAM_ADMIN_CHAT_ID"])
        except ValueError:
            pass
    if os.getenv("WEBHOOK_SECRET"):
        cfg.external_bridge.webhook_secret = os.environ["WEBHOOK_SECRET"]

    return cfg
