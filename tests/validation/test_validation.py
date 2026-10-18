"""Tests for validation checker and ML loader."""

import pytest

from atlantis.validation.checker import ArchiveChecker, ValidationResult
from atlantis.validation.ml_loader import MLLoaderValidator


class TestValidationResult:
    def test_init_passed(self):
        result = ValidationResult(passed=True, message="All good")
        assert result.passed is True
        assert result.message == "All good"
        assert result.details == {}

    def test_init_failed_with_details(self):
        result = ValidationResult(
            passed=False,
            message="Check failed",
            details={"info": "extra data"},
        )
        assert result.passed is False
        assert result.details == {"info": "extra data"}

    def test_init_default_details(self):
        result = ValidationResult(passed=True, message="ok")
        assert result.details == {}


class TestArchiveChecker:
    def test_init(self, tmp_path):
        checker = ArchiveChecker(tmp_path)
        assert checker.archive_root == tmp_path

    def test_check_spatial_alignment_raises_not_implemented(self, tmp_path):
        import numpy as np
        import xarray as xr

        checker = ArchiveChecker(tmp_path)
        ds = xr.Dataset({"flood_fraction": xr.DataArray(np.zeros((10, 10)))})
        with pytest.raises(NotImplementedError):
            checker.check_spatial_alignment(ds)

    def test_check_nan_patterns_raises_not_implemented(self, tmp_path):
        import numpy as np
        import xarray as xr

        checker = ArchiveChecker(tmp_path)
        ds = xr.Dataset({"flood_fraction": xr.DataArray(np.ones((10, 10)))})
        with pytest.raises(NotImplementedError):
            checker.check_nan_patterns(ds)

    def test_check_crs_consistency_raises_not_implemented(self, tmp_path):
        import numpy as np
        import xarray as xr

        checker = ArchiveChecker(tmp_path)
        ds = xr.Dataset({"flood_fraction": xr.DataArray(np.zeros((10, 10)))})
        with pytest.raises(NotImplementedError):
            checker.check_crs_consistency(ds)

    def test_check_value_ranges_raises_not_implemented(self, tmp_path):
        import numpy as np
        import xarray as xr

        checker = ArchiveChecker(tmp_path)
        ds = xr.Dataset({"flood_fraction": xr.DataArray(np.array([[0.0, 0.5, 1.0]]))})
        with pytest.raises(NotImplementedError):
            checker.check_value_ranges(ds, "flood_fraction", 0.0, 1.0)

    def test_run_all_checks_raises_not_implemented(self, tmp_path):
        import numpy as np
        import xarray as xr

        checker = ArchiveChecker(tmp_path)
        ds = xr.Dataset({"flood_fraction": xr.DataArray(np.zeros((5, 5)))})
        with pytest.raises(NotImplementedError):
            checker.run_all_checks(ds)


class TestMLLoaderValidator:
    def test_init(self, tmp_path):
        validator = MLLoaderValidator(tmp_path)
        assert validator.archive_root == tmp_path

    def test_dataset_creation_raises_not_implemented(self, tmp_path):
        validator = MLLoaderValidator(tmp_path)
        with pytest.raises(NotImplementedError):
            validator.test_dataset_creation("event_001", "viirs")

    def test_dataloader_batching_raises_not_implemented(self, tmp_path):
        validator = MLLoaderValidator(tmp_path)
        with pytest.raises(NotImplementedError):
            validator.test_dataloader_batching("event_001", "viirs", batch_size=32)

    def test_gpu_transfer_raises_not_implemented(self, tmp_path):
        validator = MLLoaderValidator(tmp_path)
        with pytest.raises(NotImplementedError):
            validator.test_gpu_transfer("event_001", "viirs")

    def test_validate_all_raises_not_implemented(self, tmp_path):
        validator = MLLoaderValidator(tmp_path)
        with pytest.raises(NotImplementedError):
            validator.validate_all("event_001", "viirs")
