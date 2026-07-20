"""Reizman Suzuki-Miyaura reaction-condition benchmark (plan step 1d.3).

A real reaction-optimization problem: maximize Suzuki coupling **yield** over the
catalyst/ligand (categorical) plus continuous conditions (residence time,
temperature, catalyst loading). The raw data (Reizman et al. 2016, vendored from
Summit — see data/NOTICE.md) is a discrete experimental grid, so we fit a light
RandomForest surrogate to give a continuous objective for the BO loop — the same
idea as Summit's ExperimentalEmulator, in a Python-3.11-native stack.

`load_benchmark()` returns the `OptimizationProblem` plus an async objective that
`bo.campaign.optimize` can drive.
"""

from collections.abc import Awaitable, Callable
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor

from bo.problem import (
    CategoricalParameter,
    ContinuousParameter,
    Objective,
    OptimizationProblem,
    ParamValue,
)

_DATA = Path(__file__).resolve().parent / "data" / "reizman_suzuki_case_1.csv"
_CATALYST = "catalyst"
_CONTINUOUS = ["t_res", "temperature", "catalyst_loading"]
_OBJECTIVE = "yld"


def load_dataset() -> pd.DataFrame:
    """Load the Reizman case-1 experiments, dropping Summit's `TYPE` metadata row."""
    frame = pd.read_csv(_DATA)
    frame = frame[frame["NAME"] != "TYPE"].reset_index(drop=True)
    for column in [*_CONTINUOUS, _OBJECTIVE]:
        frame[column] = frame[column].astype(float)
    return frame


def build_problem(frame: pd.DataFrame) -> OptimizationProblem:
    """Build the maximize-yield problem from the dataset's variables and ranges."""
    categories = sorted(frame[_CATALYST].unique().tolist())
    parameters: list[ContinuousParameter | CategoricalParameter] = [
        CategoricalParameter(name=_CATALYST, categories=categories)
    ]
    for column in _CONTINUOUS:
        parameters.append(
            ContinuousParameter(
                name=column, lower=float(frame[column].min()), upper=float(frame[column].max())
            )
        )
    return OptimizationProblem(
        parameters=parameters, objective=Objective(name=_OBJECTIVE, direction="maximize")
    )


class YieldSurrogate:
    """A RandomForest yield model over (catalyst one-hot + continuous conditions).

    Stands in for a physical experiment so the BO loop has a continuous objective;
    predictions are bounded by the training data, exactly as an emulator should be.
    """

    def __init__(self, model: RandomForestRegressor, categories: list[str]) -> None:
        """Hold the fitted model and the category order used for one-hot encoding."""
        self._model = model
        self._categories = categories

    @classmethod
    def fit(cls, frame: pd.DataFrame) -> "YieldSurrogate":
        """Fit the surrogate on the dataset."""
        categories = sorted(frame[_CATALYST].unique().tolist())
        features = np.array([cls._encode(row.to_dict(), categories) for _, row in frame.iterrows()])
        model = RandomForestRegressor(n_estimators=200, random_state=0)
        model.fit(features, frame[_OBJECTIVE].to_numpy())
        return cls(model, categories)

    @staticmethod
    def _encode(params: dict[str, ParamValue], categories: list[str]) -> list[float]:
        """One-hot the catalyst then append the continuous conditions, fixed order."""
        one_hot = [1.0 if params[_CATALYST] == category else 0.0 for category in categories]
        return one_hot + [float(params[column]) for column in _CONTINUOUS]

    def predict(self, params: dict[str, ParamValue]) -> float:
        """Predict yield (%) for one candidate's parameters."""
        features = np.array([self._encode(params, self._categories)])
        return float(self._model.predict(features)[0])


def load_benchmark() -> tuple[
    OptimizationProblem, Callable[[dict[str, ParamValue]], Awaitable[float]]
]:
    """Return the Reizman problem and an async yield objective for `optimize`."""
    frame = load_dataset()
    problem = build_problem(frame)
    surrogate = YieldSurrogate.fit(frame)

    async def objective(params: dict[str, ParamValue]) -> float:
        return surrogate.predict(params)

    return problem, objective
