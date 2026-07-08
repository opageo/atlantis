"""Pure reduction operators for cross-date layer aggregation.

This module is intentionally lightweight: it depends only on NumPy and the
layer spec, with no raster I/O or xarray. Processors use it as a shared engine
while keeping their own tile dataclasses and orchestration logic.

The operator semantics are source-specific by design — the registry's
``aggregation`` field declares which operator a layer uses, but the operators
themselves encode the scientific rationale (e.g. GFM's masked-max avoids
nodata=255 dominating mixed blocks; VIIRS's conservative all-true for cloud
exclusion; MODIS mode for categorical masks).
"""

from __future__ import annotations

import warnings
from typing import Literal

import numpy as np

AggregationOp = Literal[
    "nanmean",
    "mean",
    "mode",
    "max",
    "masked_max",
    "masked_or",
    "all_true",
    "majority",
]

_ALLOWED_OPS = set(AggregationOp.__args__)  # type: ignore[attr-defined]


def _mode_uint8(stack: np.ndarray) -> np.ndarray:
    """Element-wise mode of a uint8 stack along axis 0.

    Ties are broken by the lowest value because ``np.bincount`` orders counts
    by value and ``argmax`` returns the first maximum index.

    Args:
        stack: Array of shape ``(time, ...)`` containing uint8 values.

    Returns:
        Array of shape ``stack.shape[1:]`` with the most frequent value per
        pixel position.
    """
    trailing_shape = stack.shape[1:]
    flat = stack.reshape(stack.shape[0], -1)
    modes = np.empty(flat.shape[1], dtype=np.uint8)
    for i in range(flat.shape[1]):
        counts = np.bincount(flat[:, i].astype(np.int16))
        modes[i] = counts.argmax()
    return modes.reshape(trailing_shape)


def _masked_max(stack: np.ndarray, nodata: int | float) -> np.ndarray:
    """Reduce a stack along axis 0 treating *nodata* as absent.

    A valid code always beats nodata. When at least one pixel is valid the
    numeric maximum of the valid values is returned. When every pixel is
    nodata the result is nodata.
    """
    valid = stack != nodata
    # Substitute 0 for nodata before taking max. 0 is <= every valid uint8 code,
    # so it never influences the maximum when any valid value is present.
    masked = np.where(valid, stack, 0)
    reduced = masked.max(axis=0)
    all_nodata = ~valid.any(axis=0)
    return np.where(all_nodata, nodata, reduced).astype(stack.dtype)


def _masked_or(stack: np.ndarray, nodata: int | float) -> np.ndarray:
    """Reduce a stack along axis 0 by bitwise OR, treating *nodata* as absent.

    Valid codes are OR-ed together; nodata values contribute nothing. When
    every pixel is nodata the result is nodata.
    """
    valid = stack != nodata
    masked = np.where(valid, stack, 0)
    reduced = np.bitwise_or.reduce(masked, axis=0)
    all_nodata = ~valid.any(axis=0)
    return np.where(all_nodata, nodata, reduced).astype(stack.dtype)


def _all_true(stack: np.ndarray) -> np.ndarray:
    """Return 1 only where every observation along axis 0 is truthy (>0)."""
    return np.all(stack > 0, axis=0).astype(np.uint8)


def _majority(stack: np.ndarray, valid_stack: np.ndarray) -> np.ndarray:
    """Return 1 where >50% of valid observations are truthy (>0).

    Pixels with no valid observations are set to 0.
    """
    valid_count = valid_stack.sum(axis=0)
    truthy_count = np.sum((stack > 0) & valid_stack, axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(valid_count > 0, (truthy_count / valid_count) > 0.5, 0).astype(np.uint8)


def aggregate_layer(
    stack: np.ndarray,
    op: AggregationOp,
    *,
    nodata: int | float | None = None,
    valid_stack: np.ndarray | None = None,
) -> np.ndarray:
    """Reduce a ``(T, H, W)`` stack using the requested operator.

    Args:
        stack: Input array of shape ``(time, height, width)``. Typically
            ``uint8`` for categorical masks / codes or ``float32`` for
            fractions.
        op: Reduction operator name. Must be one of the ``AggregationOp``
            literals.
        nodata: Sentinel value marking missing pixels. Required for
            ``masked_max`` and ``masked_or``; ignored otherwise.
        valid_stack: Boolean array of shape ``(time, height, width)``
            indicating which observations are usable. Required only by
            ``majority``.

    Returns:
        Reduced array of shape ``(height, width)``.

    Raises:
        ValueError: If *op* is unknown, if ``majority`` is called without
            *valid_stack*, or if ``masked_max``/``masked_or`` is called without
            *nodata*.
    """
    if op not in _ALLOWED_OPS:
        raise ValueError(f"Unknown aggregation operator '{op}'. Allowed: {sorted(_ALLOWED_OPS)}")

    if op in ("masked_max", "masked_or") and nodata is None:
        raise ValueError(f"Operator '{op}' requires a nodata sentinel.")

    if op == "majority" and valid_stack is None:
        raise ValueError("Operator 'majority' requires a valid_stack argument.")

    if op == "nanmean":
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            return np.nanmean(stack, axis=0).astype(np.float32)
    if op == "mean":
        return np.mean(stack, axis=0).astype(np.float32)
    if op == "mode":
        return _mode_uint8(stack.astype(np.uint8))
    if op == "max":
        return stack.max(axis=0).astype(stack.dtype)
    if op == "masked_max":
        return _masked_max(stack, nodata)  # type: ignore[arg-type]
    if op == "masked_or":
        return _masked_or(stack, nodata)  # type: ignore[arg-type]
    if op == "all_true":
        return _all_true(stack)
    if op == "majority":
        return _majority(stack, valid_stack)  # type: ignore[arg-type]

    # Unreachable because of the validation above, but keeps mypy happy.
    raise ValueError(f"Unhandled aggregation operator '{op}'.")
