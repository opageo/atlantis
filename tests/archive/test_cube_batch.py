"""Tests for the resume-safe streaming cube batch and the writer session."""

from datetime import date, datetime

import numpy as np
import pytest

from atlantis.archive import grid
from atlantis.archive.cube_batch import _payload_to_dataset, _to_date, run_cube_batch
from atlantis.archive.reader import ArchiveReader
from atlantis.archive.writer import ArchiveWriter
from atlantis.batch import BatchConfig

# Default AOI window on the canonical global grid.
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


def window_bbox(row0: int = _ROW0, col0: int = _COL0, h: int = _H, w: int = _W):
    """Geographic bbox (west, south, east, north) of a grid window."""
    res = grid.GLOBAL_RESOLUTION
    west = grid.ORIGIN_LON + col0 * res
    east = grid.ORIGIN_LON + (col0 + w) * res
    north = grid.ORIGIN_LAT - row0 * res
    south = grid.ORIGIN_LAT - (row0 + h) * res
    return (west, south, east, north)


# ── Streaming write session (fast, no Dask) ───────────────────────────────────


class TestWriteSession:
    def test_session_streams_many_slices_then_consolidates_once(self, tmp_path):
        writer = ArchiveWriter(tmp_path)
        with writer.session("viirs", ("water_fraction",)) as session:
            session.write(aligned_dataset(0.2), time=date(2020, 1, 1))
            session.write(aligned_dataset(0.8), time=date(2020, 1, 2))

        reader = ArchiveReader(tmp_path)
        full = reader.read("viirs", bbox=window_bbox())
        assert full.sizes["time"] == 2
        first = reader.read("viirs", bbox=window_bbox(), start=date(2020, 1, 1), end=date(2020, 1, 1))
        np.testing.assert_allclose(float(first["water_fraction"].mean()), 0.2, atol=1e-6)
        second = reader.read("viirs", bbox=window_bbox(), start=date(2020, 1, 2), end=date(2020, 1, 2))
        np.testing.assert_allclose(float(second["water_fraction"].mean()), 0.8, atol=1e-6)

    def test_session_records_bounded_provenance_no_bookmark(self, tmp_path):
        import zarr

        writer = ArchiveWriter(tmp_path)
        with writer.session("viirs") as session:
            session.write(aligned_dataset(0.5), time=date(2020, 1, 1))

        attrs = dict(zarr.open_group(str(tmp_path / "datacube.zarr"), mode="r")["viirs"].attrs)
        assert attrs["source_id"] == "viirs"
        assert "last_updated" in attrs
        assert attrs["atlantis_events"] == {}

    def test_session_close_is_idempotent(self, tmp_path):
        writer = ArchiveWriter(tmp_path)
        session = writer.session("viirs")
        session.write(aligned_dataset(0.5), time=date(2020, 1, 1))
        store1 = session.close()
        store2 = session.close()  # second close must be a no-op
        assert str(store1) == str(store2)
        assert ArchiveReader(tmp_path).read("viirs", bbox=window_bbox()).sizes["time"] == 1

    def test_session_write_requires_time(self, tmp_path):
        writer = ArchiveWriter(tmp_path)
        with writer.session("viirs") as session, pytest.raises(ValueError, match="requires `time`"):
            session.write(aligned_dataset(0.5))


# ── Module helpers (fast) ─────────────────────────────────────────────────────


class TestHelpers:
    def test_to_date_from_iso_string(self):
        assert _to_date("2020-01-01") == date(2020, 1, 1)

    def test_to_date_from_datetime(self):
        assert _to_date(datetime(2020, 1, 1, 12, 30)) == date(2020, 1, 1)

    def test_to_date_from_datetime64(self):
        assert _to_date(np.datetime64("2020-01-01")) == date(2020, 1, 1)

    def test_payload_to_dataset_builds_all_viirs_layers(self):
        payload = {
            "task_id": "g000",
            "date": "2020-01-01",
            "aoi_id": 0,
            "y": np.arange(4.0),
            "x": np.arange(5.0),
            "water_fraction": np.full((4, 5), 30, dtype="uint8"),
            "exclusion_mask": np.full((4, 5), 0, dtype="uint8"),
            "reference_water": np.full((4, 5), 1, dtype="uint8"),
            "cloud_mask": np.full((4, 5), 0, dtype="uint8"),
            "snow_ice": np.full((4, 5), 0, dtype="uint8"),
            "shadow": np.full((4, 5), 0, dtype="uint8"),
        }
        ds = _payload_to_dataset(payload)
        assert set(ds.data_vars) == set(_VIIRS_LAYERS)
        for name in _VIIRS_LAYERS:
            assert ds[name].dims == ("y", "x")
        assert ds.sizes == {"y": 4, "x": 5}

    def test_payload_to_dataset_builds_gfm_layers(self):
        payload = {
            "task_id": "gfm-20241101-EU020M_E036N009T3",
            "date": "2024-11-01",
            "equi7_tile": "EU020M_E036N009T3",
            "y": np.arange(4.0),
            "x": np.arange(5.0),
            "water_fraction": np.full((4, 5), 30, dtype="uint8"),
            "exclusion_mask": np.full((4, 5), 0, dtype="uint8"),
            "reference_water": np.full((4, 5), 1, dtype="uint8"),
        }
        ds = _payload_to_dataset(payload)
        assert set(ds.data_vars) == set(_GFM_LAYERS)
        for name in _GFM_LAYERS:
            assert ds[name].dims == ("y", "x")
        assert ds.sizes == {"y": 4, "x": 5}


# ── Dask cube batch (slow integration) ────────────────────────────────────────

#: The full set of VIIRS derived layers written by
#: :func:`atlantis.fetchers.viirs.batch_processor.harmonise_granule_payload`
#: and streamed into the cube by :func:`atlantis.archive.cube_batch.run_viirs_cube_batch`.
_VIIRS_LAYERS = ("water_fraction", "exclusion_mask", "reference_water", "cloud_mask", "snow_ice", "shadow")


def _fake_payload_produce(task: dict) -> dict:
    """Module-level (picklable) fake producer — builds a synthetic AOI payload.

    Each ``aoi_id`` maps to a distinct, non-overlapping 16-row band on the global
    grid, with a constant uint8 water-fraction percent derived from the id and
    matching 0/1 mask channels for every other VIIRS derived layer.
    """
    import numpy as np

    from atlantis.archive import grid

    h = w = 16
    row0 = 4000 + int(task["aoi_id"]) * h
    col0 = 10000
    y = np.asarray(grid.global_y_coords()[row0 : row0 + h], dtype="float64")
    x = np.asarray(grid.global_x_coords()[col0 : col0 + w], dtype="float64")
    water_fraction = np.full((h, w), (int(task["aoi_id"]) + 1) * 10, dtype="uint8")
    mask_val = np.full((h, w), int(task["aoi_id"]) % 2, dtype="uint8")
    return {
        "task_id": task["task_id"],
        "date": task["date"],
        "aoi_id": int(task["aoi_id"]),
        "water_fraction": water_fraction,
        "exclusion_mask": mask_val,
        "reference_water": mask_val,
        "cloud_mask": mask_val,
        "snow_ice": mask_val,
        "shadow": mask_val,
        "y": y,
        "x": x,
    }


def _aoi_bbox(aoi_id: int, *, h: int = 16, w: int = 16, col0: int = 10000):
    res = grid.GLOBAL_RESOLUTION
    row0 = 4000 + aoi_id * h
    west = grid.ORIGIN_LON + col0 * res
    east = grid.ORIGIN_LON + (col0 + w) * res
    north = grid.ORIGIN_LAT - row0 * res
    south = grid.ORIGIN_LAT - (row0 + h) * res
    return (west, south, east, north)


def _make_tasks(n: int) -> list[dict]:
    return [{"task_id": f"g{i:03d}", "date": "2020-01-01", "aoi_id": i} for i in range(n)]


@pytest.fixture()
def cfg(tmp_path):
    return BatchConfig(
        db_path=tmp_path / "cube_tracker.db",
        workers_min=2,
        workers_max=2,
        retries=1,
        log_every=5,
        dashboard_port=0,  # disable dashboard in tests
    )


def _run(tmp_path, cfg, tasks):
    """Wire the fake producer to a real writer session (mirrors run_viirs_cube_batch)."""
    archive_root = str(tmp_path / "cube")
    writer = ArchiveWriter(archive_root)
    with writer.session("viirs", _VIIRS_LAYERS) as session:

        def consume(payload):
            session.write(_payload_to_dataset(payload), time=_to_date(payload["date"]))
            return f"{archive_root}#viirs/{payload['date']}/aoi{payload['aoi_id']:03d}"

        final = run_cube_batch(tasks, _fake_payload_produce, consume, cfg)
    return archive_root, final


@pytest.mark.e2e
def test_cube_batch_streams_writes_and_tracks(tmp_path, cfg):
    pytest.importorskip("dask.distributed", reason="batch extras (dask/distributed) not installed")
    tasks = _make_tasks(6)
    archive_root, final = _run(tmp_path, cfg, tasks)

    assert final.get("DONE", 0) == 6
    assert final.get("FAILED", 0) == 0

    reader = ArchiveReader(archive_root)
    assert reader.list_sources() == ["viirs"]
    # A specific AOI band round-trips to its expected decoded value.
    # aoi_id=2 → water_fraction=(2+1)*10=30 (CF-scaled to 0.30); masks=2%2=0 (raw, unscaled).
    band = reader.read("viirs", bbox=_aoi_bbox(2))
    assert set(_VIIRS_LAYERS) <= set(band.data_vars)
    np.testing.assert_allclose(float(band["water_fraction"].mean()), 0.30, atol=1e-6)
    for name in ("exclusion_mask", "reference_water", "cloud_mask", "snow_ice", "shadow"):
        np.testing.assert_allclose(float(band[name].mean()), 0.0, atol=1e-6)

    # aoi_id=3 → masks=3%2=1 (raw, unscaled) confirms the odd/even mask values round-trip too.
    odd_band = reader.read("viirs", bbox=_aoi_bbox(3))
    for name in ("exclusion_mask", "reference_water", "cloud_mask", "snow_ice", "shadow"):
        np.testing.assert_allclose(float(odd_band[name].mean()), 1.0, atol=1e-6)


@pytest.mark.e2e
def test_cube_batch_resume_skips_done(tmp_path, cfg):
    pytest.importorskip("dask.distributed", reason="batch extras (dask/distributed) not installed")
    from atlantis.batch.tracker import init_db, mark_done, stats

    tasks = _make_tasks(4)
    init_db(cfg.db_path)
    for t in tasks[:2]:  # pretend the first two were written in a prior run
        mark_done(cfg.db_path, t["task_id"], "s3://pre")

    _run(tmp_path, cfg, tasks)

    s = stats(cfg.db_path)
    assert s.get("DONE", 0) == 4  # 2 pre-seeded + 2 newly streamed


@pytest.mark.e2e
def test_cube_batch_all_done_is_noop(tmp_path, cfg):
    pytest.importorskip("dask.distributed", reason="batch extras (dask/distributed) not installed")
    from atlantis.batch.tracker import init_db, mark_done

    tasks = _make_tasks(3)
    init_db(cfg.db_path)
    for t in tasks:
        mark_done(cfg.db_path, t["task_id"], "s3://pre")

    # Nothing pending → returns immediately without spinning a cluster.
    archive_root = str(tmp_path / "cube")
    writer = ArchiveWriter(archive_root)
    with writer.session("viirs"):

        def consume(payload):  # pragma: no cover - must never be called
            raise AssertionError("consume should not run when all tasks are DONE")

        final = run_cube_batch(tasks, _fake_payload_produce, consume, cfg)
    assert final.get("DONE", 0) == 3


# ── GFM cube batch wiring (Dask integration, mirrors the VIIRS block above) ──

#: The GFM layers persisted in the cube (see docs/layers.md — companions
#: ``ensemble_likelihood`` / ``advisory_flags`` are not part of the cube schema).
_GFM_LAYERS = ("water_fraction", "exclusion_mask", "reference_water")

#: Fixed EQUI7-tile stand-ins (no real STAC lookups needed for this fake producer).
_GFM_TILES = ["EU020M_E036N009T3", "EU020M_E036N006T3", "EU020M_E036N012T3"]


def _fake_gfm_payload_produce(task: dict) -> dict:
    """Module-level (picklable) fake producer for a GFM ``(date, equi7_tile)`` cell.

    Mirrors :func:`_fake_payload_produce` but keyed by ``equi7_tile`` instead of
    ``aoi_id``, matching :func:`atlantis.fetchers.gfm.batch_processor.harmonise_gfm_payload`'s
    payload shape (water_fraction/exclusion_mask/reference_water only).
    """
    import numpy as np

    from atlantis.archive import grid

    tile_index = _GFM_TILES.index(task["equi7_tile"])
    h = w = 16
    row0 = 4000 + tile_index * h
    col0 = 10000
    y = np.asarray(grid.global_y_coords()[row0 : row0 + h], dtype="float64")
    x = np.asarray(grid.global_x_coords()[col0 : col0 + w], dtype="float64")
    water_fraction = np.full((h, w), (tile_index + 1) * 10, dtype="uint8")
    mask_val = np.full((h, w), tile_index % 2, dtype="uint8")
    return {
        "task_id": task["task_id"],
        "date": task["date"],
        "equi7_tile": task["equi7_tile"],
        "water_fraction": water_fraction,
        "exclusion_mask": mask_val,
        "reference_water": mask_val,
        "y": y,
        "x": x,
    }


def _gfm_tile_bbox(tile_index: int, *, h: int = 16, w: int = 16, col0: int = 10000):
    res = grid.GLOBAL_RESOLUTION
    row0 = 4000 + tile_index * h
    west = grid.ORIGIN_LON + col0 * res
    east = grid.ORIGIN_LON + (col0 + w) * res
    north = grid.ORIGIN_LAT - row0 * res
    south = grid.ORIGIN_LAT - (row0 + h) * res
    return (west, south, east, north)


def _make_gfm_tasks(n: int) -> list[dict]:
    return [
        {"task_id": f"gfm-20200101-{_GFM_TILES[i]}", "date": "2020-01-01", "equi7_tile": _GFM_TILES[i]}
        for i in range(n)
    ]


def _run_gfm(tmp_path, cfg, tasks):
    """Wire the fake GFM producer to a real writer session (mirrors run_gfm_cube_batch)."""
    archive_root = str(tmp_path / "cube")
    writer = ArchiveWriter(archive_root)
    with writer.session("gfm", _GFM_LAYERS) as session:

        def consume(payload):
            session.write(_payload_to_dataset(payload), time=_to_date(payload["date"]))
            return f"{archive_root}#gfm/{payload['date']}/{payload['equi7_tile']}"

        final = run_cube_batch(tasks, _fake_gfm_payload_produce, consume, cfg)
    return archive_root, final


@pytest.mark.e2e
def test_gfm_cube_batch_streams_writes_and_tracks(tmp_path, cfg):
    pytest.importorskip("dask.distributed", reason="batch extras (dask/distributed) not installed")
    tasks = _make_gfm_tasks(3)
    archive_root, final = _run_gfm(tmp_path, cfg, tasks)

    assert final.get("DONE", 0) == 3
    assert final.get("FAILED", 0) == 0

    reader = ArchiveReader(archive_root)
    assert reader.list_sources() == ["gfm"]
    # tile index 1 (EU020M_E036N006T3) → water_fraction=(1+1)*10=20 (CF-scaled to 0.20); masks=1%2=1.
    band = reader.read("gfm", bbox=_gfm_tile_bbox(1))
    assert set(_GFM_LAYERS) <= set(band.data_vars)
    np.testing.assert_allclose(float(band["water_fraction"].mean()), 0.20, atol=1e-6)
    for name in ("exclusion_mask", "reference_water"):
        np.testing.assert_allclose(float(band[name].mean()), 1.0, atol=1e-6)
