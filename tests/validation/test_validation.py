"""Tests for validation checker and ML loader."""

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

    def test_check_spatial_alignment_returns_passed(self, tmp_path):
        import numpy as np
        import xarray as xr

        checker = ArchiveChecker(tmp_path)
        ds = xr.Dataset({"flood_fraction": xr.DataArray(np.zeros((10, 10)))})
        result = checker.check_spatial_alignment(ds)
        assert isinstance(result, ValidationResult)
        assert result.passed is True

    def test_check_nan_patterns_returns_passed(self, tmp_path):
        import numpy as np
        import xarray as xr

        checker = ArchiveChecker(tmp_path)
        ds = xr.Dataset({"flood_fraction": xr.DataArray(np.ones((10, 10)))})
        result = checker.check_nan_patterns(ds)
        assert isinstance(result, ValidationResult)
        assert result.passed is True

    def test_check_crs_consistency_returns_passed(self, tmp_path):
        import numpy as np
        import xarray as xr

        checker = ArchiveChecker(tmp_path)
        ds = xr.Dataset({"flood_fraction": xr.DataArray(np.zeros((10, 10)))})
        result = checker.check_crs_consistency(ds)
        assert isinstance(result, ValidationResult)
        assert result.passed is True

    def test_check_value_ranges_returns_passed(self, tmp_path):
        import numpy as np
        import xarray as xr

        checker = ArchiveChecker(tmp_path)
        ds = xr.Dataset({"flood_fraction": xr.DataArray(np.array([[0.0, 0.5, 1.0]]))})
        result = checker.check_value_ranges(ds, "flood_fraction", 0.0, 1.0)
        assert isinstance(result, ValidationResult)
        assert result.passed is True

    def test_run_all_checks(self, tmp_path):
        import numpy as np
        import xarray as xr

        checker = ArchiveChecker(tmp_path)
        ds = xr.Dataset({"flood_fraction": xr.DataArray(np.zeros((5, 5)))})
        results = checker.run_all_checks(ds)
        assert len(results) == 3
        assert all(isinstance(r, ValidationResult) for r in results)
        assert all(r.passed for r in results)


class TestMLLoaderValidator:
    def test_init(self, tmp_path):
        validator = MLLoaderValidator(tmp_path)
        assert validator.archive_root == tmp_path

    def test_dataset_creation_returns_false(self, tmp_path):
        validator = MLLoaderValidator(tmp_path)
        assert validator.test_dataset_creation("event_001", "viirs") is False

    def test_dataloader_batching_returns_false(self, tmp_path):
        validator = MLLoaderValidator(tmp_path)
        assert validator.test_dataloader_batching("event_001", "viirs", batch_size=32) is False

    def test_gpu_transfer_returns_false(self, tmp_path):
        validator = MLLoaderValidator(tmp_path)
        assert validator.test_gpu_transfer("event_001", "viirs") is False

    def test_validate_all(self, tmp_path):
        validator = MLLoaderValidator(tmp_path)
        results = validator.validate_all("event_001", "viirs")
        assert isinstance(results, dict)
        assert set(results.keys()) == {"dataset_creation", "dataloader_batching", "gpu_transfer"}
        assert all(v is False for v in results.values())
