"""Tests for the consolidated Zarr datacube archive."""

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from atlantis.archive import grid
from atlantis.archive.reader import ArchiveReader
from atlantis.archive.writer import ArchiveWriter
from atlantis.config import ArchiveConfig
from atlantis.models.event import FloodEvent

# Default AOI window on the canonical global grid used across tests.
_ROW0, _COL0, _H, _W = 4000, 10000, 50, 60


def aligned_dataset(value: float = 0.5, *, row0: int = _ROW0, col0: int = _COL0, h: int = _H, w: int = _W):
    """Build a harmonised-style float dataset aligned to the global 1-arcmin grid."""
    import xarray as xr

    y = grid.global_y_coords()[row0 : row0 + h]
    x = grid.global_x_coords()[col0 : col0 + w]
    data = np.full((h, w), value, dtype="float32")
    return xr.Dataset(
        {"water_fraction": xr.DataArray(data, dims=["y", "x"], coords={"y": y, "x": x})},
        attrs={"crs": "EPSG:4326"},
    )


def _zarr_array(store, source_id: str, var: str):
    import zarr

    return zarr.open_group(store, mode="r")[source_id][var]


def window_bbox(row0: int = _ROW0, col0: int = _COL0, h: int = _H, w: int = _W):
    """Geographic bbox (west, south, east, north) of a grid window."""
    res = grid.GLOBAL_RESOLUTION
    west = grid.ORIGIN_LON + col0 * res
    east = grid.ORIGIN_LON + (col0 + w) * res
    north = grid.ORIGIN_LAT - row0 * res
    south = grid.ORIGIN_LAT - (row0 + h) * res
    return (west, south, east, north)


@pytest.fixture()
def event() -> FloodEvent:
    return FloodEvent(
        event_id="test_event",
        bbox=window_bbox(),
        start_date=date(2020, 1, 1),
        end_date=date(2020, 1, 5),
        sources=["viirs"],
    )


@pytest.fixture()
def simple_dataset():
    return aligned_dataset(0.5)


# ── ArchiveWriter (datacube) ──────────────────────────────────────────────────


class TestArchiveWriter:
    def test_write_creates_consolidated_sharded_cube(self, tmp_path, simple_dataset):
        store = ArchiveWriter(tmp_path).write(simple_dataset, "viirs", time=date(2020, 1, 1))
        # A single consolidated store, grouped by source, on the global grid.
        assert Path(store).name == "datacube.zarr"
        assert Path(store).exists()
        arr = _zarr_array(store, "viirs", "water_fraction")
        assert arr.shape[1:] == (grid.GLOBAL_HEIGHT, grid.GLOBAL_WIDTH)
        assert arr.chunks == (1, 256, 256)
        assert arr.shards == (1, 2048, 2048)

    def test_write_empty_dataset_raises(self, tmp_path):
        import xarray as xr

        with pytest.raises(ValueError, match="Dataset is empty"):
            ArchiveWriter(tmp_path).write(xr.Dataset(), "viirs", time=date(2020, 1, 1))

    def test_write_requires_time_or_event(self, tmp_path, simple_dataset):
        with pytest.raises(ValueError, match="requires `time`"):
            ArchiveWriter(tmp_path).write(simple_dataset, "viirs")

    def test_write_rejects_unaligned(self, tmp_path):
        import xarray as xr

        y = np.linspace(40.0, 20.0, 50)
        x = np.linspace(10.0, 30.0, 60)
        ds = xr.Dataset(
            {"water_fraction": xr.DataArray(np.zeros((50, 60), "float32"), dims=["y", "x"], coords={"y": y, "x": x})}
        )
        with pytest.raises(ValueError, match="not aligned"):
            ArchiveWriter(tmp_path).write(ds, "viirs", time=date(2020, 1, 1))

    def test_write_without_masks_stores_flood_only(self, tmp_path, simple_dataset):
        import zarr

        store = ArchiveWriter(tmp_path).write(simple_dataset, "viirs", time=date(2020, 1, 1))
        group = zarr.open_group(store, mode="r")["viirs"]
        assert "water_fraction" in group
        assert "quality_mask" not in group
        assert "permanent_water" not in group

    def test_write_ensure_masks_generates_channels(self, tmp_path, simple_dataset):
        import zarr

        store = ArchiveWriter(tmp_path).write(simple_dataset, "viirs", time=date(2020, 1, 1), ensure_masks=True)
        group = zarr.open_group(store, mode="r")["viirs"]
        assert "quality_mask" in group
        assert "permanent_water" in group

    def test_daily_write_bounded_provenance_no_bookmark(self, tmp_path, simple_dataset):
        import zarr

        store = ArchiveWriter(tmp_path).write(simple_dataset, "viirs", time=date(2020, 1, 1))
        attrs = dict(zarr.open_group(store, mode="r")["viirs"].attrs)
        assert attrs["source_id"] == "viirs"
        assert "last_updated" in attrs
        # Daily writes never pile up the event registry.
        assert attrs["atlantis_events"] == {}


# ── Config-identity guarantee ──────────────────────────────────────────────────


class TestConfigIdentityGuarantee:
    def test_same_config_repeated_write_ok(self, tmp_path, simple_dataset):
        writer = ArchiveWriter(tmp_path)
        writer.write(simple_dataset, "viirs", time=date(2020, 1, 1))
        writer.write(simple_dataset, "viirs", time=date(2020, 1, 2))  # no raise

    def test_chunk_drift_raises(self, tmp_path, simple_dataset):
        from atlantis.archive.datacube import ConfigMismatchError

        ArchiveWriter(tmp_path).write(simple_dataset, "viirs", time=date(2020, 1, 1))
        drifted = ArchiveWriter(tmp_path, ArchiveConfig(chunk_size=128, shard_size=1024))
        with pytest.raises(ConfigMismatchError, match="ArchiveConfig drift"):
            drifted.write(simple_dataset, "viirs", time=date(2020, 1, 2))

    def test_scale_factor_drift_raises(self, tmp_path, simple_dataset):
        from atlantis.archive.datacube import ConfigMismatchError

        ArchiveWriter(tmp_path).write(simple_dataset, "viirs", time=date(2020, 1, 1))
        drifted = ArchiveWriter(tmp_path, ArchiveConfig(scale_factor=0.1))
        with pytest.raises(ConfigMismatchError, match="ArchiveConfig drift"):
            drifted.write(simple_dataset, "viirs", time=date(2020, 1, 2))

    def test_pre_existing_group_without_fingerprint_adopts_baseline(self, tmp_path, simple_dataset):
        """A group written before this guard shipped has no recorded fingerprint yet."""
        import zarr

        store = ArchiveWriter(tmp_path).write(simple_dataset, "viirs", time=date(2020, 1, 1))
        group = zarr.open_group(store, mode="a")["viirs"]
        del group.attrs["archive_config"]

        ArchiveWriter(tmp_path).write(simple_dataset, "viirs", time=date(2020, 1, 2))  # no raise
        assert "archive_config" in zarr.open_group(store, mode="r")["viirs"].attrs


# ── ArchiveReader (datacube) ──────────────────────────────────────────────────


class TestArchiveReader:
    def test_init(self, tmp_path):
        assert ArchiveReader(tmp_path).archive_root == str(tmp_path)

    def test_read_missing_store_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="Datacube not found"):
            ArchiveReader(tmp_path).read("viirs", bbox=window_bbox())

    def test_read_by_bbox_cf_decode(self, tmp_path):
        ArchiveWriter(tmp_path).write(aligned_dataset(0.5), "viirs", time=date(2020, 1, 1))
        ds = ArchiveReader(tmp_path).read("viirs", bbox=window_bbox())
        assert ds.sizes["y"] == _H and ds.sizes["x"] == _W
        # uint8 50 decodes via scale_factor 0.01 -> 0.5
        np.testing.assert_allclose(float(ds["water_fraction"].mean()), 0.5, atol=1e-6)

    def test_read_full_grid_when_no_bbox(self, tmp_path):
        ArchiveWriter(tmp_path).write(aligned_dataset(0.5), "viirs", time=date(2020, 1, 1))
        ds = ArchiveReader(tmp_path).read("viirs")
        assert ds.sizes["y"] == grid.GLOBAL_HEIGHT and ds.sizes["x"] == grid.GLOBAL_WIDTH

    def test_read_time_range(self, tmp_path):
        writer = ArchiveWriter(tmp_path)
        writer.write(aligned_dataset(0.3), "viirs", time=date(2020, 1, 1))
        writer.write(aligned_dataset(0.7), "viirs", time=date(2020, 1, 3))
        reader = ArchiveReader(tmp_path)
        assert reader.read("viirs", bbox=window_bbox()).sizes["time"] == 2
        one = reader.read("viirs", bbox=window_bbox(), start=date(2020, 1, 3), end=date(2020, 1, 3))
        assert one.sizes["time"] == 1
        np.testing.assert_allclose(float(one["water_fraction"].mean()), 0.7, atol=1e-6)

    def test_read_resolves_crs(self, tmp_path):
        import rioxarray  # noqa: F401  (registers the .rio accessor)

        ArchiveWriter(tmp_path).write(aligned_dataset(0.5), "viirs", time=date(2020, 1, 1))
        ds = ArchiveReader(tmp_path).read("viirs", bbox=window_bbox())
        assert ds.rio.crs is not None
        assert ds.rio.crs.to_epsg() == 4326

    def test_sparse_unwritten_is_nodata(self, tmp_path):
        import xarray as xr

        store = ArchiveWriter(tmp_path).write(aligned_dataset(0.5), "viirs", time=date(2020, 1, 1))
        full = xr.open_zarr(store, group="viirs", consolidated=True)
        # A pixel far outside the AOI window decodes to NaN (chunk never written).
        assert np.isnan(float(full["water_fraction"].isel(time=0, y=0, x=0)))

    def test_list_sources(self, tmp_path):
        writer = ArchiveWriter(tmp_path)
        writer.write(aligned_dataset(0.5), "viirs", time=date(2020, 1, 1))
        writer.write(aligned_dataset(0.5), "gfm", time=date(2020, 1, 1))
        assert ArchiveReader(tmp_path).list_sources() == ["gfm", "viirs"]

    def test_list_empty(self, tmp_path):
        reader = ArchiveReader(tmp_path)
        assert reader.list_sources() == []
        assert reader.list_events() == []

    def test_read_tiles_by_bbox(self, tmp_path):
        cfg = ArchiveConfig(chunk_size=32, shard_size=64)
        ArchiveWriter(tmp_path, cfg).write(aligned_dataset(0.5, h=64, w=64), "viirs", time=date(2020, 1, 1))
        out = ArchiveReader(tmp_path, cfg).read("viirs", bbox=window_bbox(h=64, w=64), tiles=[(0, 0), (1, 1)])
        assert out.sizes["tile"] == 2
        assert out.sizes["y"] == 32 and out.sizes["x"] == 32


# ── Pre-filled year axis (per-event GFM backfill) ─────────────────────────────


class TestPrefillYear:
    def _assert_date_value(self, tmp_path, day, expected):
        ds = ArchiveReader(tmp_path).read("gfm", bbox=window_bbox(), start=day, end=day)
        np.testing.assert_allclose(float(ds["water_fraction"].mean()), expected, atol=1e-6)

    def test_prefill_full_year_and_out_of_order_writes(self, tmp_path):
        from atlantis.archive import datacube
        from atlantis.archive.ordering import unsorted_spans

        writer = ArchiveWriter(tmp_path)
        with writer.session("gfm", ["water_fraction"], prefill_year=2024) as session:
            session.write(aligned_dataset(0.3), time=date(2024, 10, 29))
            session.write(aligned_dataset(0.7), time=date(2024, 9, 15))

        import zarr

        group = zarr.open_group(tmp_path / "datacube.zarr", mode="r")["gfm"]
        times = np.asarray(group["time"][:])
        assert times.shape == (366,)
        assert group["water_fraction"].shape[0] == 366
        epoch = ArchiveConfig().time_epoch
        written = {datacube.date_to_int(d, epoch) for d in (date(2024, 9, 15), date(2024, 10, 29))}
        assert written <= set(times.tolist())
        assert unsorted_spans(times) == []
        self._assert_date_value(tmp_path, date(2024, 9, 15), 0.7)
        self._assert_date_value(tmp_path, date(2024, 10, 29), 0.3)

        # second run: pre-fill is a no-op and a re-write overwrites in place
        with writer.session("gfm", ["water_fraction"], prefill_year=2024) as session:
            session.write(aligned_dataset(0.9), time=date(2024, 10, 29))
        group = zarr.open_group(tmp_path / "datacube.zarr", mode="r")["gfm"]
        assert group["time"].shape[0] == 366
        assert group["water_fraction"].shape[0] == 366
        self._assert_date_value(tmp_path, date(2024, 10, 29), 0.9)
        self._assert_date_value(tmp_path, date(2024, 9, 15), 0.7)

    def test_prefill_upgrades_empty_scaffold_in_place(self, tmp_path):
        """The 2025-style empty scaffold (shape 0, no data) upgrades to 366."""
        writer = ArchiveWriter(tmp_path)
        with writer.session("gfm", ["water_fraction"]):
            pass  # creates the empty scaffold: time (0,), data (0, ...)

        import zarr

        group = zarr.open_group(tmp_path / "datacube.zarr", mode="r")["gfm"]
        assert group["time"].shape[0] == 0
        assert group["water_fraction"].shape[0] == 0

        with writer.session("gfm", ["water_fraction"], prefill_year=2024) as session:
            session.write(aligned_dataset(0.5), time=date(2024, 10, 29))
            session.write(aligned_dataset(0.6), time=date(2024, 9, 15))

        group = zarr.open_group(tmp_path / "datacube.zarr", mode="r")["gfm"]
        times = np.asarray(group["time"][:])
        assert times.shape == (366,)
        assert group["water_fraction"].shape[0] == 366
        # out-of-order dates land in their pre-filled slots
        self._assert_date_value(tmp_path, date(2024, 10, 29), 0.5)
        self._assert_date_value(tmp_path, date(2024, 9, 15), 0.6)

    def test_prefill_writes_exact_days_per_year(self, tmp_path):
        """365 slots for a common year, 366 for a leap year, first/last ints correct."""
        from atlantis.archive import datacube

        epoch = ArchiveConfig().time_epoch
        for year, expected_days in ((2020, 366), (2021, 365), (2024, 366), (2025, 365)):
            root = tmp_path / str(year)
            writer = ArchiveWriter(root)
            with writer.session("gfm", ["water_fraction"], prefill_year=year):
                pass

            import zarr

            group = zarr.open_group(root / "datacube.zarr", mode="r")["gfm"]
            times = np.asarray(group["time"][:], dtype="int64")
            assert times.shape == (expected_days,)
            assert group["water_fraction"].shape[0] == expected_days
            assert times[0] == datacube.date_to_int(date(year, 1, 1), epoch)
            assert times[-1] == datacube.date_to_int(date(year + 1, 1, 1) - timedelta(days=1), epoch)
            assert (np.diff(times) == 1).all()

    def test_prefill_sets_marker_on_resize_not_noop(self, tmp_path):
        """``atlantis_time_prefill`` is written on the resize, never on a no-op."""
        import zarr

        writer = ArchiveWriter(tmp_path)
        with writer.session("gfm", ["water_fraction"], prefill_year=2024):
            pass
        group = zarr.open_group(tmp_path / "datacube.zarr", mode="r")["gfm"]
        assert group.attrs["atlantis_time_prefill"] == "2024"

        # a re-run is a no-op: axis stays 366, marker unchanged
        with writer.session("gfm", ["water_fraction"], prefill_year=2025):
            pass
        group = zarr.open_group(tmp_path / "datacube.zarr", mode="r")["gfm"]
        assert group["time"].shape[0] == 366  # legacy 366-slot group untouched
        assert group.attrs["atlantis_time_prefill"] == "2024"

        # a never-prefilled group carries no marker
        other = ArchiveWriter(tmp_path / "plain")
        with other.session("gfm", ["water_fraction"]):
            pass
        group = zarr.open_group(tmp_path / "plain" / "datacube.zarr", mode="r")["gfm"]
        assert "atlantis_time_prefill" not in group.attrs

    def test_prefill_skips_partially_written_group(self, tmp_path):
        """0 < n < days is never prefilled (existing data would misalign)."""
        import zarr

        writer = ArchiveWriter(tmp_path)
        with writer.session("gfm", ["water_fraction"]) as session:
            session.write(aligned_dataset(0.5), time=date(2024, 3, 1))

        with writer.session("gfm", ["water_fraction"], prefill_year=2024):
            pass

        group = zarr.open_group(tmp_path / "datacube.zarr", mode="r")["gfm"]
        assert group["time"].shape[0] == 1
        assert group["water_fraction"].shape[0] == 1
        assert "atlantis_time_prefill" not in group.attrs
        self._assert_date_value(tmp_path, date(2024, 3, 1), 0.5)


# ── Masked region writes (GFM rotated-tile mosaic) ─────────────────────────────


class TestMaskedRegionWrite:
    """NODATA in an incoming write must never erase already-valid cube data.

    GFM EQUI7 native tiles are rotated squares in EPSG:4326, so the axis-aligned
    envelopes of adjacent tiles overlap. Each task region-writes its full
    envelope rectangle — valid data only inside the rotated footprint, NODATA
    (255) in the corners. Without masking, the last-written tile's corner
    wedges clobber the earlier tile's data (the "GFM wedge" bug, docs/gfm/
    memory-root-cause.md). Valid data must win over NODATA regardless of write
    order.
    """

    def _read(self, tmp_path, row0=_ROW0, col0=_COL0, h=_H, w=_W, day=date(2020, 1, 1)):
        import zarr

        root = zarr.open_group(tmp_path / "datacube.zarr", mode="r")["gfm"]
        from atlantis.archive import datacube

        epoch = ArchiveConfig().time_epoch
        t = int(np.where(root["time"][:] == datacube.date_to_int(day, epoch))[0][0])
        return np.asarray(root["water_fraction"][t, row0 : row0 + h, col0 : col0 + w])

    def test_nodata_does_not_clobber_valid_data(self, tmp_path):
        """A later write's NODATA corner wedge keeps an earlier tile's valid pixels."""
        import xarray as xr

        writer = ArchiveWriter(tmp_path)
        with writer.session("gfm", ["water_fraction"]) as session:
            # Tile A: valid 0.20 over the whole window.
            session.write(aligned_dataset(0.20), time=date(2020, 1, 1))
            # Tile B: same grid window, valid 0.70 except a top-left NODATA wedge.
            y = grid.global_y_coords()[_ROW0 : _ROW0 + _H]
            x = grid.global_x_coords()[_COL0 : _COL0 + _W]
            data = np.full((_H, _W), 0.70, dtype="float32")
            rr, cc = np.mgrid[0:_H, 0:_W]
            data[rr + cc < _H // 2] = np.nan  # top-left triangle -> NODATA
            ds_b = xr.Dataset({"water_fraction": xr.DataArray(data, dims=["y", "x"], coords={"y": y, "x": x})})
            session.write(ds_b, time=date(2020, 1, 1))

        sub = self._read(tmp_path)
        # NW corner: B's wedge is NODATA, A's 20 must survive.
        assert sub[0, 0] == 20, "later NODATA clobbered earlier valid data"
        # SE corner: B's valid 70 must have overwritten A's 20.
        assert sub[-1, -1] == 70, "later valid data did not win"
        # On-diagonal transition pixel: still B's valid value.
        assert 20 <= sub[_H // 2, _W // 2] <= 70

    def test_valid_overwrites_valid_in_overlap(self, tmp_path):
        """Two valid writes to the same pixel: last writer wins (unchanged)."""

        writer = ArchiveWriter(tmp_path)
        with writer.session("gfm", ["water_fraction"]) as session:
            session.write(aligned_dataset(0.30), time=date(2020, 1, 1))
            session.write(aligned_dataset(0.80), time=date(2020, 1, 1))
        sub = self._read(tmp_path)
        assert (sub == 80).all(), "last valid write should win everywhere"

    def test_no_nodata_write_is_still_plain_overwrite(self, tmp_path):
        """A fully-valid later write still replaces an earlier one in place."""
        import zarr

        writer = ArchiveWriter(tmp_path)
        with writer.session("gfm", ["water_fraction"]) as session:
            session.write(aligned_dataset(0.30), time=date(2020, 1, 1))
            session.write(aligned_dataset(0.80), time=date(2020, 1, 1))
        group = zarr.open_group(tmp_path / "datacube.zarr", mode="r")["gfm"]
        assert group["water_fraction"].shape[0] == 1  # no spurious time slots


# ── Optional event bookmarks ──────────────────────────────────────────────────


class TestEventBookmark:
    def test_write_event_registers_bookmark(self, tmp_path, event, simple_dataset):
        ArchiveWriter(tmp_path).write(simple_dataset, "viirs", event=event)
        assert ArchiveReader(tmp_path).list_events() == ["test_event"]

    def test_read_by_event(self, tmp_path, event):
        ArchiveWriter(tmp_path).write(aligned_dataset(0.5), "viirs", time=date(2020, 1, 1), event=event)
        ds = ArchiveReader(tmp_path).read("viirs", event="test_event")
        assert ds.sizes["y"] == _H and ds.sizes["x"] == _W
        np.testing.assert_allclose(float(ds["water_fraction"].mean()), 0.5, atol=1e-6)

    def test_read_unknown_event_raises(self, tmp_path, event, simple_dataset):
        ArchiveWriter(tmp_path).write(simple_dataset, "viirs", event=event)
        with pytest.raises(KeyError):
            ArchiveReader(tmp_path).read("viirs", event="nope")


# ── ArchiveWriterCheckpoint (unchanged) ───────────────────────────────────────


class TestArchiveWriterCheckpoint:
    def test_write_checkpoint_creates_file(self, tmp_path):
        from datetime import date

        writer = ArchiveWriter(tmp_path)
        event = FloodEvent(
            event_id="event_001",
            bbox=(10.0, 20.0, 30.0, 40.0),
            start_date=date(2020, 1, 1),
            end_date=date(2020, 1, 1),
            sources=["viirs"],
        )
        path = writer.write_checkpoint(event, "viirs", "fetch")
        assert path.exists()
        assert path.name == "viirs_fetch.done"

    def test_is_checkpointed_true(self, tmp_path):
        from datetime import date

        writer = ArchiveWriter(tmp_path)
        event = FloodEvent(
            event_id="event_001",
            bbox=(10.0, 20.0, 30.0, 40.0),
            start_date=date(2020, 1, 1),
            end_date=date(2020, 1, 1),
            sources=["viirs"],
        )
        writer.write_checkpoint(event, "viirs", "fetch")
        assert writer.is_checkpointed(event, "viirs", "fetch") is True

    def test_is_checkpointed_false(self, tmp_path):
        from datetime import date

        writer = ArchiveWriter(tmp_path)
        event = FloodEvent(
            event_id="event_001",
            bbox=(10.0, 20.0, 30.0, 40.0),
            start_date=date(2020, 1, 1),
            end_date=date(2020, 1, 1),
            sources=["viirs"],
        )
        assert writer.is_checkpointed(event, "viirs", "harmonise") is False

    def test_multiple_stages_independent(self, tmp_path):
        from datetime import date

        writer = ArchiveWriter(tmp_path)
        event = FloodEvent(
            event_id="event_002",
            bbox=(10.0, 20.0, 30.0, 40.0),
            start_date=date(2020, 1, 1),
            end_date=date(2020, 1, 1),
            sources=["viirs"],
        )
        writer.write_checkpoint(event, "viirs", "fetch")
        assert writer.is_checkpointed(event, "viirs", "fetch") is True
        assert writer.is_checkpointed(event, "viirs", "harmonise") is False
        assert writer.is_checkpointed(event, "viirs", "archive") is False

        writer.write_checkpoint(event, "viirs", "harmonise")
        assert writer.is_checkpointed(event, "viirs", "harmonise") is True

    def test_write_checkpoint_returns_path(self, tmp_path):
        from datetime import date

        writer = ArchiveWriter(tmp_path)
        event = FloodEvent(
            event_id="event_003",
            bbox=(10.0, 20.0, 30.0, 40.0),
            start_date=date(2020, 1, 1),
            end_date=date(2020, 1, 1),
            sources=["viirs"],
        )
        result = writer.write_checkpoint(event, "viirs", "fetch")
        assert isinstance(result, Path)
        assert result.parent.exists()
