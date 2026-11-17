import numpy as np
import pandas as pd

from bot.models.baseline import LogisticBaseline
from bot.models.registry import dataset_hash
from bot.training.walkforward import purged_walk_forward


def test_baseline_learns_separable_data() -> None:
    x = np.array([[-2.0], [-1.0], [1.0], [2.0]])
    y = np.array([0, 0, 1, 1])
    model = LogisticBaseline(epochs=300).fit(x, y)
    assert model.predict_proba(np.array([[2.0]]))[0, 1] > 0.5


def test_walk_forward_applies_embargo() -> None:
    index = pd.date_range("2026-01-01", periods=20, freq="D", tz="UTC")
    folds = list(purged_walk_forward(index, train_days=5, test_days=3, embargo_minutes=60))
    assert folds
    assert folds[0].train[-1] < folds[0].test[0]


def test_dataset_hash_is_stable() -> None:
    assert dataset_hash(["a"], 10, {"seed": 42}) == dataset_hash(["a"], 10, {"seed": 42})
