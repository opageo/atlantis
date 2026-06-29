"""Tests for plotting utilities."""

import numpy as np

from atlantis.utils.plot import (
    VIIRS_CODES,
    date_from_filename,
    legend_patches,
    pixel_stats_classified,
    pixel_stats_raw,
)


class TestDateFromFilename:
    def test_standard_viirs_filename(self):
        assert date_from_filename("KuroSiwo_1111004_20170828_viirs_flood_extent.tif") == "2017-08-28"

    def test_filename_with_iso_date(self):
        assert date_from_filename("VIIRS-Flood-1day-GLB077_s20200722.tif") == "2020-07-22"

    def test_no_date_returns_unknown(self):
        assert date_from_filename("random_file.tif") == "unknown"

    def test_empty_string(self):
        assert date_from_filename("") == "unknown"

    def test_at_boundary_with_digit_boundary(self):
        """8-digit token at word boundary should still be extracted."""
        assert date_from_filename("data_20210115_output.tif") == "2021-01-15"


class TestPixelStatsRaw:
    def test_output_prints(self, capsys):
        data = np.array([[17, 17, 30], [160, 160, 160]], dtype=np.uint8)
        pixel_stats_raw(data, name="test")
        captured = capsys.readouterr()
        assert "test:" in captured.out
        assert "160" in captured.out  # flood code

    def test_all_nodata(self, capsys):
        data = np.zeros((5, 5), dtype=np.uint8)
        pixel_stats_raw(data, name="nodata_test")
        captured = capsys.readouterr()
        assert "all pixels are nodata" in captured.out


class TestPixelStatsClassified:
    def test_returns_flooded_count(self):
        data = np.array([[0, 0, 1], [0, 1, 1]], dtype=np.float32)
        count = pixel_stats_classified(data, name="test_flood")
        assert count == 3  # three non-zero pixels

    def test_all_nan(self, capsys):
        data = np.full((3, 3), np.nan, dtype=np.float32)
        count = pixel_stats_classified(data)
        assert count == 0
        captured = capsys.readouterr()
        assert "all NaN" in captured.out

    def test_all_zero(self):
        data = np.zeros((5, 5), dtype=np.float32)
        count = pixel_stats_classified(data)
        assert count == 0


class TestLegendPatches:
    def test_returns_correct_number_of_patches(self):
        patches = legend_patches()
        assert len(patches) == len(VIIRS_CODES)

    def test_patches_have_correct_labels(self):
        patches = legend_patches()
        labels = [p.get_label() for p in patches]
        assert "160: Flood (≥60% frac)" in labels
        assert "99: Permanent water" in labels
        assert "17: Vegetation" in labels

    def test_all_patches_are_patch_objects(self):
        from matplotlib.patches import Patch

        patches = legend_patches()
        for p in patches:
            assert isinstance(p, Patch)


class TestViirsCodes:
    def test_expected_codes_present(self):
        expected_codes = {1, 17, 20, 30, 99, 160}
        assert set(VIIRS_CODES.keys()) == expected_codes

    def test_code_values_are_tuples(self):
        for code, (label, color) in VIIRS_CODES.items():
            assert isinstance(code, int)
            assert isinstance(label, str)
            assert isinstance(color, str)
            assert color.startswith("#")
