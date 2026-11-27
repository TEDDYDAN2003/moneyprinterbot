"""Verify an exact clean Hull checkout, then run *only* its read-only MT5 preflight.

No network Git operations, file migration, scanner/Journal start, or arming option.
Run with the existing checkout's installed Python, from that same checkout's root.
"""

import argparse
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAIRS = {
    5: 4,
    10: 8,
    15: 12,
}  # release contract, independent of installed model defaults


class ReleaseCheckError(ValueError):
    pass


def check_git(root, revision):
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", revision):
        raise ReleaseCheckError(
            "Supply the full published commit SHA, not a branch or a short ID."
        )

    def git(*args):
        return subprocess.check_output(
            ["git", "--no-optional-locks", "-C", str(root), *args],
            text=True,
            timeout=20,
        ).strip()

    if git("rev-parse", "--verify", "HEAD") != revision:
        raise ReleaseCheckError(
            "Wrong checkout revision. No files changed and no MT5 preflight run."
        )
    changed = git("status", "--porcelain=v1", "--untracked-files=normal")
    if changed:
        raise ReleaseCheckError(
            "This is not the clean release. Back up local work; do not force-reset.\n"
            + "\n".join(changed.splitlines()[:10])
        )


def run_preflight(revision, config, *, root=ROOT):
    if Path.cwd().resolve() != root:
        raise ReleaseCheckError(f"Run from the intended existing checkout: {root}")
    check_git(root, revision)
    # Do not inject src on sys.path: catch the exact stale interpreter/editable install
    # that would otherwise be used again when the operator runs bot.cli.
    spec = importlib.util.find_spec("bot")
    if (
        not spec
        or not spec.origin
        or Path(spec.origin).resolve() != root / "src/bot/__init__.py"
    ):
        raise ReleaseCheckError(
            f"This interpreter is not loading bot from {root / 'src'}. Select/fix the checkout venv."
        )
    from bot.config.loader import load_yaml
    from bot.config.schema import validate_config
    from bot.hull import terminal
    from bot.hull.config import selection
    from bot.hull.target_settings import effective_config

    build = terminal.source_build()
    if (
        Path(build["source_module"]).resolve() != root / "src/bot/hull/terminal.py"
        or build["checkout_head_at_start"] != revision
    ):
        raise ReleaseCheckError(
            "Wrong imported Hull module or revision. No terminal call."
        )
    cfgfile = config.resolve(strict=True)
    raw = effective_config(cfgfile, load_yaml(cfgfile))
    model = validate_config(raw)
    h = model.hull
    if not (
        h.execution_mode == "paired_structure"
        and h.entry_route == "market_next_quote"
        and h.effective_timing == "intrabar_cross"
        and set(h.lanes()) == set(PAIRS)
        and h.pairs == PAIRS
    ):
        raise ReleaseCheckError(
            "Config does not request the published policy: paired_structure, market_next_quote, intrabar_cross (or by_route), M5:M4 / M10:M8 / M15:M12. Verify the existing hull section; no files were changed."
        )
    print(
        f"Checking {revision}\nPython: {sys.executable}\nImported: {build['source_module']}\nConfig: {cfgfile} ({model.run.mode})",
        flush=True,
    )
    # Same validated model, settings overlay, symbol selection and preflight path as
    # the CLI; pass this very snapshot instead of re-reading a mutable config twice.
    summary = terminal.run(
        model.model_dump() | {"hull": raw["hull"]},
        h,
        selection(h),
        armed=False,
        preflight=True,
    )
    if not (
        summary["mode"] == "READ ONLY"
        and summary["account"] == "USD hedging demo"
        and summary["pair_history_ready"]
        and summary["entry_timing"] == "intrabar_cross"
        and summary["parent_to_trigger"] == {f"M{p}": f"M{c}" for p, c in PAIRS.items()}
        and summary["checkout_head_at_start"] == revision
    ):
        raise ReleaseCheckError(
            "Unexpected preflight report. See the printed evidence above; do not arm."
        )
    check_git(root, revision)
    print(
        "Read-only preflight completed; no scanner was started. Review warnings, current exposure, sessions, lots and BUY/SELL target previews. This is not authorization to trade."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expect", required=True, help="full published Git commit")
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="exact reviewed config, including a private targets sidecar if present",
    )
    args = parser.parse_args()
    try:
        run_preflight(args.expect, args.config)
    except (
        OSError,
        RuntimeError,
        ValueError,
        ImportError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"Preflight stopped: {exc}", file=sys.stderr)
        sys.exit(1)
