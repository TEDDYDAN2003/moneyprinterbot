from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class LogisticBaseline:
    learning_rate: float = 0.05
    epochs: int = 500
    l2: float = 1.0
    weights: np.ndarray | None = None
    mean_: np.ndarray | None = None
    scale_: np.ndarray | None = None

    def fit(self, x: np.ndarray, y: np.ndarray) -> "LogisticBaseline":
        features = np.asarray(x, dtype=float)
        target = np.asarray(y, dtype=float)
        if features.ndim != 2 or target.ndim != 1 or len(features) != len(target):
            raise ValueError("x must be 2-D and y must be a matching 1-D array")
        if len(np.unique(target)) < 2:
            raise ValueError("Training requires both classes")
        self.mean_ = np.nanmean(features, axis=0)
        self.scale_ = np.nanstd(features, axis=0)
        self.scale_[self.scale_ == 0] = 1.0
        normalized = np.nan_to_num((features - self.mean_) / self.scale_)
        design = np.column_stack([np.ones(len(normalized)), normalized])
        weights = np.zeros(design.shape[1])
        for _ in range(self.epochs):
            probabilities = _sigmoid(design @ weights)
            gradient = design.T @ (probabilities - target) / len(target)
            gradient[1:] += self.l2 * weights[1:] / len(target)
            weights -= self.learning_rate * gradient
        self.weights = weights
        return self

    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        if self.weights is None or self.mean_ is None or self.scale_ is None:
            raise RuntimeError("Model must be fitted before prediction")
        features = np.asarray(x, dtype=float)
        normalized = np.nan_to_num((features - self.mean_) / self.scale_)
        design = np.column_stack([np.ones(len(normalized)), normalized])
        positive = _sigmoid(design @ self.weights)
        return np.column_stack([1.0 - positive, positive])


def _sigmoid(values: np.ndarray) -> np.ndarray:
    clipped = np.clip(values, -40, 40)
    return 1.0 / (1.0 + np.exp(-clipped))
