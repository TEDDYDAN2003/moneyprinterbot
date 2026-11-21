"""Offline-only v4 gates; never a scanner fallback."""

from ..orders import Block, make_plan as protected_plan


def make_plan(d, p, grid, bid, ask, cash):
    if d.entry_side and (why := d.structure.veto(d.side, ask if d.side == 1 else bid)):
        raise Block("quote_" + why)
    return protected_plan(d, p, grid, bid, ask, cash)


def choose(ds):
    """One non-overlapping batch per symbol; never multiply a shared timestamp."""
    ready = [d for d in ds if d.side]
    if not ready:
        return None, "no_executable_label"
    t = max(d.signal_msc for d in ready)
    same = [d for d in ready if d.signal_msc == t]
    if len({d.side for d in same}) > 1:
        return None, "opposite_labels_same_trigger" if any(
            d.trigger for d in same
        ) else "opposite_labels_same_close"
    return max(same, key=lambda d: d.tf), "ready"


def select_labels(ds):
    """Independent picks: existing exit arbitration, and structure-qualified entry.

    Keep the original opportunity timestamp. An older valid signal must not be
    revived by a newer veto. Only qualified lanes vote on a NEW position.
    """
    original, why = choose(ds)
    latest = max((d.signal_msc for d in ds if d.side), default=None)
    if latest is None:
        return original, None, why
    valid = [d for d in ds if d.signal_msc == latest and d.entry_side]
    if not valid:
        return (
            original,
            None,
            (original.entry_reason if original else "no_structure_qualified_label"),
        )
    entry, entry_why = choose(valid)
    return original, entry, entry_why
