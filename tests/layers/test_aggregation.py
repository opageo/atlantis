"""Tests for the shared aggregation operators."""

from __future__ import annotations

import numpy as np
import pytest

from atlantis.layers.aggregation import aggregate_layer


class TestNanmean:
    def test_simple_mean(self):
        stack = np.array([[[1.0, 2.0], [3.0, 4.0]], [[5.0, 6.0], [7.0, 8.0]]], dtype=np.float32)
        result = aggregate_layer(stack, "nanmean")
        np.testing.assert_allclose(result, np.array([[3.0, 4.0], [5.0, 6.0]], dtype=np.float32))

    def test_ignores_nans(self):
        stack = np.array([[[1.0, np.nan], [3.0, 4.0]], [[np.nan, 6.0], [7.0, 8.0]]], dtype=np.float32)
        result = aggregate_layer(stack, "nanmean")
        np.testing.assert_allclose(result, np.array([[1.0, 6.0], [5.0, 6.0]], dtype=np.float32))

    def test_all_nan_becomes_nan(self):
        stack = np.full((2, 2, 2), np.nan, dtype=np.float32)
        result = aggregate_layer(stack, "nanmean")
        assert np.all(np.isnan(result))


class TestMean:
    def test_simple_mean(self):
        stack = np.array([[[1.0, 2.0], [3.0, 4.0]], [[5.0, 6.0], [7.0, 8.0]]], dtype=np.float32)
        result = aggregate_layer(stack, "mean")
        np.testing.assert_allclose(result, np.array([[3.0, 4.0], [5.0, 6.0]], dtype=np.float32))


class TestMode:
    def test_majority_wins(self):
        stack = np.array(
            [[[0, 1], [2, 3]], [[0, 1], [2, 3]], [[1, 1], [2, 4]]],
            dtype=np.uint8,
        )
        result = aggregate_layer(stack, "mode")
        np.testing.assert_array_equal(result, np.array([[0, 1], [2, 3]], dtype=np.uint8))

    def test_tie_breaks_to_lowest_value(self):
        stack = np.array([[[1, 2]], [[2, 1]]], dtype=np.uint8)
        result = aggregate_layer(stack, "mode")
        np.testing.assert_array_equal(result, np.array([[1, 1]], dtype=np.uint8))


class TestMax:
    def test_numeric_max(self):
        stack = np.array([[[1, 2], [3, 4]], [[5, 1], [2, 8]]], dtype=np.uint8)
        result = aggregate_layer(stack, "max")
        np.testing.assert_array_equal(result, np.array([[5, 2], [3, 8]], dtype=np.uint8))


class TestMaskedMax:
    def test_both_valid_returns_max(self):
        stack = np.array([[[0, 1], [2, 0]], [[1, 0], [0, 1]]], dtype=np.uint8)
        result = aggregate_layer(stack, "masked_max", nodata=255)
        np.testing.assert_array_equal(result, np.array([[1, 1], [2, 1]], dtype=np.uint8))

    def test_one_nodata_uses_other(self):
        stack = np.array([[[255, 1], [255, 0]], [[0, 255], [1, 255]]], dtype=np.uint8)
        result = aggregate_layer(stack, "masked_max", nodata=255)
        np.testing.assert_array_equal(result, np.array([[0, 1], [1, 0]], dtype=np.uint8))

    def test_both_nodata_stays_nodata(self):
        stack = np.full((2, 2, 2), 255, dtype=np.uint8)
        result = aggregate_layer(stack, "masked_max", nodata=255)
        np.testing.assert_array_equal(result, np.full((2, 2), 255, dtype=np.uint8))

    def test_some_nodata_others_valid_max(self):
        stack = np.array(
            [[[255, 255], [255, 10]], [[5, 255], [255, 255]], [[255, 7], [20, 255]]],
            dtype=np.uint8,
        )
        result = aggregate_layer(stack, "masked_max", nodata=255)
        np.testing.assert_array_equal(result, np.array([[5, 7], [20, 10]], dtype=np.uint8))

    def test_requires_nodata(self):
        stack = np.zeros((2, 2, 2), dtype=np.uint8)
        with pytest.raises(ValueError, match="requires a nodata sentinel"):
            aggregate_layer(stack, "masked_max")


class TestMaskedOr:
    def test_bitwise_or_valid(self):
        stack = np.array([[[1, 2], [4, 8]], [[2, 4], [8, 1]]], dtype=np.uint8)
        result = aggregate_layer(stack, "masked_or", nodata=255)
        np.testing.assert_array_equal(result, np.array([[3, 6], [12, 9]], dtype=np.uint8))

    def test_nodata_ignored(self):
        stack = np.array([[[255, 2], [4, 255]], [[1, 255], [255, 8]]], dtype=np.uint8)
        result = aggregate_layer(stack, "masked_or", nodata=255)
        np.testing.assert_array_equal(result, np.array([[1, 2], [4, 8]], dtype=np.uint8))

    def test_all_nodata_stays_nodata(self):
        stack = np.full((2, 2, 2), 255, dtype=np.uint8)
        result = aggregate_layer(stack, "masked_or", nodata=255)
        np.testing.assert_array_equal(result, np.full((2, 2), 255, dtype=np.uint8))

    def test_requires_nodata(self):
        stack = np.zeros((2, 2, 2), dtype=np.uint8)
        with pytest.raises(ValueError, match="requires a nodata sentinel"):
            aggregate_layer(stack, "masked_or")


class TestAllTrue:
    def test_all_true_only_if_every_obs_truthy(self):
        stack = np.array([[[1, 1], [1, 0]], [[1, 0], [0, 1]]], dtype=np.uint8)
        result = aggregate_layer(stack, "all_true")
        np.testing.assert_array_equal(result, np.array([[1, 0], [0, 0]], dtype=np.uint8))


class TestMajority:
    def test_majority_of_valid(self):
        stack = np.array(
            [[[1, 1], [1, 0]], [[1, 0], [0, 0]], [[0, 1], [1, 0]]],
            dtype=np.uint8,
        )
        valid = np.array([[[True, True], [True, True]], [[True, True], [True, False]], [[True, True], [True, True]]])
        result = aggregate_layer(stack, "majority", valid_stack=valid)
        np.testing.assert_array_equal(result, np.array([[1, 1], [1, 0]], dtype=np.uint8))

    def test_fifty_percent_is_not_majority(self):
        stack = np.array([[[1, 1]], [[0, 0]]], dtype=np.uint8)
        valid = np.ones((2, 1, 2), dtype=bool)
        result = aggregate_layer(stack, "majority", valid_stack=valid)
        np.testing.assert_array_equal(result, np.array([[0, 0]], dtype=np.uint8))

    def test_no_valid_observations_is_zero(self):
        stack = np.array([[[1, 0]], [[0, 1]]], dtype=np.uint8)
        valid = np.zeros((2, 1, 2), dtype=bool)
        result = aggregate_layer(stack, "majority", valid_stack=valid)
        np.testing.assert_array_equal(result, np.array([[0, 0]], dtype=np.uint8))

    def test_requires_valid_stack(self):
        stack = np.zeros((2, 2, 2), dtype=np.uint8)
        with pytest.raises(ValueError, match="requires a valid_stack"):
            aggregate_layer(stack, "majority")


class TestValidation:
    def test_unknown_op(self):
        stack = np.zeros((2, 2, 2), dtype=np.uint8)
        with pytest.raises(ValueError, match="Unknown aggregation operator"):
            aggregate_layer(stack, "not_an_op")  # type: ignore[arg-type]

    def test_allowed_ops_listed_in_error(self):
        stack = np.zeros((2, 2, 2), dtype=np.uint8)
        with pytest.raises(ValueError, match="Allowed:.*'all_true'.*'majority'.*'masked_max'"):
            aggregate_layer(stack, "unknown")  # type: ignore[arg-type]
