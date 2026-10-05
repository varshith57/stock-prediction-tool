"""A group of different models for signals A and C ("ensemble").

Members, chosen to make different kinds of mistakes:

* ``lightgbm``: gradient-boosted trees (the original model; also gives the "why" lines);
* ``xgboost``: gradient-boosted trees with a different tree-growing strategy;
* ``catboost``: gradient-boosted symmetric trees, robust on noisy tabular data;
* ``extra_trees``: a bagged forest of randomised trees, each grown on a bootstrap sample;
* ``logistic``: a linear model on standardised features.

Each member is calibrated on its own (isotonic, on the calibration block), so all of them speak
in real probabilities; the group's score is their average. The caller calibrates that average
once more on the same block, exactly as it does for a single model, so the gate and the app
treat the group like any other model. Averaging differently-wrong models mostly steadies the
ranking; it can't create information the features don't have.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import polars as pl
from sklearn.ensemble import ExtraTreesClassifier
from sklearn.impute import SimpleImputer
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

MEMBERS = ("lightgbm", "xgboost", "catboost", "extra_trees", "logistic")
SEED = 7


def _matrix(df: pl.DataFrame, features: list[str]) -> np.ndarray:
    return df.select(pl.col(features).cast(pl.Float32)).to_numpy()


def make_member(name: str, pos_weight: float):
    """An unfitted member. Trees handle missing values themselves; the others impute medians."""
    if name == "lightgbm":
        import lightgbm as lgb

        from stockapp.models.walkforward import PARAMS

        return lgb.LGBMClassifier(**PARAMS, scale_pos_weight=pos_weight, random_state=SEED)
    if name == "xgboost":
        from xgboost import XGBClassifier

        return XGBClassifier(
            n_estimators=300,
            learning_rate=0.05,
            max_depth=5,
            min_child_weight=50,
            subsample=0.8,
            colsample_bytree=0.8,
            reg_lambda=1.0,
            tree_method="hist",
            scale_pos_weight=pos_weight,
            n_jobs=4,
            random_state=SEED,
            verbosity=0,
        )
    if name == "catboost":
        from catboost import CatBoostClassifier

        return CatBoostClassifier(
            iterations=300,
            learning_rate=0.08,
            depth=6,
            l2_leaf_reg=3.0,
            scale_pos_weight=pos_weight,
            thread_count=4,
            random_seed=SEED,
            verbose=False,
            allow_writing_files=False,
        )
    if name == "extra_trees":
        return make_pipeline(
            SimpleImputer(strategy="median"),
            ExtraTreesClassifier(
                n_estimators=200,
                min_samples_leaf=100,
                max_features="sqrt",
                bootstrap=True,
                max_samples=0.5,
                class_weight="balanced_subsample",
                n_jobs=4,
                random_state=SEED,
            ),
        )
    if name == "logistic":
        return make_pipeline(
            SimpleImputer(strategy="median"),
            StandardScaler(),
            LogisticRegression(max_iter=1000, C=0.1, class_weight="balanced"),
        )
    raise ValueError(f"unknown member {name}")


@dataclass
class Ensemble:
    """Average of calibrated member probabilities. Behaves like a sklearn classifier."""

    members: dict[str, object]
    calibrators: dict[str, IsotonicRegression]
    features: list[str] = field(default_factory=list)

    def member_probabilities(self, x: np.ndarray) -> dict[str, np.ndarray]:
        return {
            name: self.calibrators[name].predict(m.predict_proba(x)[:, 1])  # type: ignore[attr-defined]
            for name, m in self.members.items()
        }

    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        p = np.mean(list(self.member_probabilities(x).values()), axis=0)
        return np.column_stack([1 - p, p])

    @property
    def booster_(self):
        """The LightGBM member's booster, for the per-stock 'why' lines (SHAP contributions)."""
        return self.members["lightgbm"].booster_  # type: ignore[attr-defined]


def fit_ensemble(
    train: pl.DataFrame,
    calib: pl.DataFrame,
    features: list[str],
    label: str,
    members: tuple[str, ...] = MEMBERS,
) -> Ensemble:
    x, y = _matrix(train, features), train[label].cast(pl.Int8).to_numpy()
    xc, yc = _matrix(calib, features), calib[label].cast(pl.Int8).to_numpy()
    pos = max(int(y.sum()), 1)
    fitted, calibrators = {}, {}
    for name in members:
        m = make_member(name, (len(y) - pos) / pos)
        m.fit(x, y)
        iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        iso.fit(m.predict_proba(xc)[:, 1], yc)
        fitted[name], calibrators[name] = m, iso
    return Ensemble(fitted, calibrators, list(features))
