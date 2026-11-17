"""The error of a walk-forward evaluation, in the manifest's measures.

Scoring is `utilsforecast.evaluation.evaluate`: each measure per series over
every fold, then one number across series. The two measures the library does
not have are written in the form it takes. The residuals a backtest leaves
behind are the quantiles of its absolute error per step ahead, from which a
family without intervals of its own gets them.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

import pandas as pd
import pyarrow as pa
from utilsforecast.evaluation import evaluate
from utilsforecast.losses import mae, mape, rmse

from ..protocol.families import DS, MEAN, UNIQUE_ID, Y, high, low
from .errors import bad_params

CUTOFF = "cutoff"
RESIDUALS_MEDIA_TYPE = "application/vnd.apache.arrow.file"
QUANTILES = (0.5, 0.8, 0.9, 0.95, 0.99)
RESIDUAL_COLUMNS = ("step", "quantile", "abs_error")


def max_abs(
    df: pd.DataFrame,
    models: list[str],
    id_col: str = UNIQUE_ID,
    target_col: str = Y,
    cutoff_col: str = CUTOFF,
) -> pd.DataFrame:
    """The largest absolute error of each series."""
    by = [df[name] for name in (cutoff_col, id_col) if name in df.columns]
    errors = df[models].sub(df[target_col], axis=0).abs()
    return errors.groupby(by, observed=True).max().reset_index()


def error_rate(
    df: pd.DataFrame,
    models: list[str],
    id_col: str = UNIQUE_ID,
    target_col: str = Y,
    cutoff_col: str = CUTOFF,
) -> pd.DataFrame:
    """The share of each series' predictions that, rounded to a whole number, are not
    the truth."""
    by = [df[name] for name in (cutoff_col, id_col) if name in df.columns]
    wrong = df[models].round().ne(df[target_col], axis=0).astype(float)
    return wrong.groupby(by, observed=True).mean().reset_index()


# Per measure: the function `evaluate` takes, and how its values per series become one.
MEASURES = {
    "mae": (mae, "mean"),
    "rmse": (rmse, "mean"),
    "mape": (mape, "mean"),
    "max_abs": (max_abs, "max"),
    "error_rate": (error_rate, "mean"),
}


def check_metrics(metrics: Sequence[str], target: pd.Series) -> None:
    """Refuse a measure the twin does not have, or one the target cannot be scored in."""
    unknown = [name for name in metrics if name not in MEASURES]
    if unknown:
        raise bad_params(
            f"unknown error measure {', '.join(unknown)}; the measures are {', '.join(MEASURES)}",
            param="metrics",
        )
    values = target.dropna()
    if "error_rate" in metrics and not (values == values.round()).all():
        raise bad_params(
            "error_rate counts predictions that round to another whole number than the "
            "truth; the target holds values that are not whole numbers",
            param="metrics",
        )


def score(
    folds: pd.DataFrame, models: Sequence[str], metrics: Sequence[str]
) -> dict[str, dict[str, float]]:
    """Per model column, the error in each measure over every fold and series."""
    # Without the cutoff the library scores a series over all its folds at once.
    pooled = folds[[UNIQUE_ID, DS, Y, *models]]
    found: dict[str, dict[str, float]] = {model: {} for model in models}
    for across in ("mean", "max"):
        functions = [MEASURES[name][0] for name in metrics if MEASURES[name][1] == across]
        if not functions:
            continue
        scored = evaluate(pooled, metrics=functions, models=list(models), agg_fn=across)
        for row in scored.to_dict("records"):
            for model in models:
                found[model][row["metric"]] = float(row[model])
    for model, values in found.items():
        undefined = [name for name in metrics if pd.isna(values[name])]
        if undefined:
            raise bad_params(
                f"{', '.join(undefined)} is not defined on these folds ({model}): mape needs "
                "a truth that is not zero",
                param="metrics",
            )
    return {model: {name: values[name] for name in metrics} for model, values in found.items()}


def residual_quantiles(folds: pd.DataFrame, step: timedelta) -> pa.Table:
    """Per step ahead, the quantiles of the absolute error over folds and series."""
    ahead = ((folds[DS] - folds[CUTOFF]) // pd.Timedelta(step)).astype("int64")
    errors = (folds[MEAN] - folds[Y]).abs()
    found = errors.groupby(ahead).quantile(list(QUANTILES))
    steps, quantiles = zip(*found.index, strict=True)
    return pa.table(
        {
            "step": pa.array(steps, pa.int64()),
            "quantile": pa.array(quantiles, pa.float64()),
            "abs_error": pa.array(found.to_numpy(), pa.float64()),
        }
    )


def residuals_file(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def read_residuals(content: bytes) -> pd.DataFrame:
    try:
        table = pa.ipc.open_file(pa.BufferReader(content)).read_all()
    except (pa.ArrowInvalid, OSError) as exc:
        raise bad_params(
            "the residuals named are not a table of residuals", param="residuals"
        ) from exc
    if tuple(table.column_names) != RESIDUAL_COLUMNS:
        raise bad_params("the residuals named are not a table of residuals", param="residuals")
    return table.to_pandas()


def intervals_from_residuals(
    prediction: pd.DataFrame, residuals: pd.DataFrame, levels: Sequence[float]
) -> pd.DataFrame:
    """The prediction with, for each level, the mean less and plus the stored quantile of
    the absolute error at each row's step ahead."""
    result = prediction.sort_values([UNIQUE_ID, DS], kind="stable").reset_index(drop=True)
    ahead = result.groupby(UNIQUE_ID, sort=False).cumcount() + 1
    for level in levels:
        stored = residuals[(residuals["quantile"] - level / 100.0).abs() < 1e-9]
        width = ahead.map(stored.set_index("step")["abs_error"])
        if stored.empty or width.isna().any():
            have = sorted({round(float(q) * 100, 6) for q in residuals["quantile"]})
            raise bad_params(
                f"the residuals hold no quantile for level {level:g} at every step of the "
                f"horizon (levels stored: {', '.join(f'{found:g}' for found in have)}; steps "
                f"1 to {int(residuals['step'].max()) if len(residuals) else 0})",
                param="levels",
            )
        result[low(level)] = result[MEAN] - width.to_numpy()
        result[high(level)] = result[MEAN] + width.to_numpy()
    return result
