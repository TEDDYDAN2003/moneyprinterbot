from __future__ import annotations

import pandas as pd


def make_training_frame(features: pd.DataFrame, labels: pd.Series) -> pd.DataFrame:
    if not features.index.equals(labels.index):
        raise ValueError("Features and labels must have identical timestamp indexes")
    frame = features.copy()
    frame["label"] = labels
    frame = frame.dropna(subset=["label"])
    if frame.empty:
        raise ValueError("No labeled rows remain after removing unavailable outcomes")
    return frame
