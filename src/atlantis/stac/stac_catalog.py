"""Build a static self-contained STAC catalog for the KuroSiwo SAR flood dataset.

On-disk input layout
--------------------
assets/kurosiwo/{actid}/
    {aoiid_dir}/          '00' = unlabeled (aoiid=null), '01','02',... = labeled
        [{2char}/{hash}/] unlabeled: 2-level hash-sharded directories
        [{hash}/]         labeled:   flat tile directories
            info.json
            {DATASET_NAME}.tif  (e.g. MS1_IVV_1111002_01_20200915.tif)

STAC output hierarchy
---------------------
Catalog: kurosiwo
├── Collection: kurosiwo-labeled       role: ML training data (aoiid ≥ 1)
│   └── Collection: kurosiwo-labeled-{actid}
│       └── Item: kurosiwo-{actid}-{grid_id}
│           ├── Asset: ms1_ivv / ms1_ivh  flood-time VV/VH      role: data
│           ├── Asset: sl{n}_ivv / ivh    pre-flood slave VV/VH  role: data
│           ├── Asset: mk0_mna            flood mask (uint8)     role: label
│           └── Asset: mk0_dem / mk0_mlu / mk0_slope             role: auxiliary
│
└── Collection: kurosiwo-unlabeled     role: background / semi-supervised (aoiid=null)
    └── Collection: kurosiwo-unlabeled-{actid}
        └── Item: kurosiwo-{actid}-{grid_id}
            ├── Asset: ms1_ivv/ivh / sl{n}_ivv/ivh               role: data
            └── Asset: mk0_mna            water mask (uint8)     role: auxiliary

Item properties (ks: namespace)
--------------------------------
ks:actid, ks:grid_id, ks:pflood, ks:pwater, ks:pcovered,
ks:slavecov, ks:mastercov, ks:gvalid, ks:aoiid, ks:flood_date

Usage (CLI)
-----------
    # PoC — single event
    python -m atlantis.stac_catalog --event 1111002

    # All events (once downloaded)
    python -m atlantis.stac_catalog --event 1111002 --event 1111003 ...

    # Custom paths
    python -m atlantis.stac_catalog \\
        --root /data/kurosiwo \\
        --output /output/stac \\
        --event 1111002
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pyproj
import pystac
import typer
from loguru import logger
from pystac.extensions.sar import FrequencyBand, ObservationDirection, Polarization, SarExtension
from shapely import wkt
from shapely.geometry import mapping
from shapely.ops import transform

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KS_CATALOG_ID = "kurosiwo"
KS_LABELED_ID = "kurosiwo-labeled"
KS_UNLABELED_ID = "kurosiwo-unlabeled"

KS_DESCRIPTION = (
    "KuroSiwo: Large-scale multi-temporal SAR dataset for flood detection. "
    "43 real flood events, 1.73 M catalogue entries, ~1.6 M exported patches. "
    "Source: Orion-AI-Lab/KuroSiwo — SAR Sentinel-1 GRD, EPSG:3857, 256×256 px tiles (~2.24 km)."
)

# pname → (title, media_type)
_ASSET_META: dict[str, tuple[str, str]] = {
    "IVV": ("VV amplitude (float32, linear scale)", pystac.MediaType.GEOTIFF),
    "IVH": ("VH amplitude (float32, linear scale)", pystac.MediaType.GEOTIFF),
    "MNA": ("Flood / water mask (uint8: 0=dry, 1=flood/water, 255=nodata)", pystac.MediaType.GEOTIFF),
    "DEM": ("Digital Elevation Model (float32, metres)", pystac.MediaType.GEOTIFF),
    "MLU": ("Land Use classification (uint8)", pystac.MediaType.GEOTIFF),
    "SLOPE": ("Terrain slope (float32, degrees)", pystac.MediaType.GEOTIFF),
}

# Only MNA changes meaning between labeled and unlabeled; keep the others uniform.
_LABELED_ROLES: dict[str, list[str]] = {
    "IVV": ["data"],
    "IVH": ["data"],
    "MNA": ["label"],  # pixel-level flood mask → training label
    "DEM": ["auxiliary"],
    "MLU": ["auxiliary"],
    "SLOPE": ["auxiliary"],
}
_UNLABELED_ROLES: dict[str, list[str]] = {
    "IVV": ["data"],
    "IVH": ["data"],
    "MNA": ["data"],  # water mask only — no flood label context
}

# Numeric regex for aoiid directory names ("00", "01", ...)
_AOIID_DIR_RE = re.compile(r"^\d+$")

# Single shared EPSG:3857 → EPSG:4326 transformer (thread-safe after construction)
_TRANSFORMER = pyproj.Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)

# SAR extension constants — fixed for all KuroSiwo tiles (Sentinel-1 IW GRD)
_SAR_INSTRUMENT_MODE = "IW"
_SAR_FREQUENCY_BAND = FrequencyBand.C
_SAR_CENTER_FREQUENCY = 5.405  # GHz
_SAR_PRODUCT_TYPE = "GRD"
_SAR_OBSERVATION_DIRECTION = ObservationDirection.RIGHT
_SAR_PIXEL_SPACING_RANGE = 10.0  # metres
_SAR_PIXEL_SPACING_AZIMUTH = 10.0  # metres
_SAR_RESOLUTION_RANGE = 5.0  # metres
_SAR_RESOLUTION_AZIMUTH = 20.0  # metres

# pname → SAR Polarization (IVV = Intensity VV, IVH = Intensity VH)
_PNAME_TO_POLARIZATION: dict[str, Polarization] = {
    "IVV": Polarization.VV,
    "IVH": Polarization.VH,
}


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _to_wgs84(geom_wkt: str) -> tuple[dict, list[float]]:
    """Convert a WKT geometry in EPSG:3857 to a (GeoJSON dict, bbox) pair in WGS84."""
    geom = wkt.loads(geom_wkt)
    geom_wgs84 = transform(_TRANSFORMER.transform, geom)
    bbox = list(geom_wgs84.bounds)  # [minx, miny, maxx, maxy]
    return mapping(geom_wgs84), bbox


# ---------------------------------------------------------------------------
# Tile discovery
# ---------------------------------------------------------------------------


def _iter_tile_dirs(event_root: Path) -> Iterator[Path]:
    """Yield every tile directory (containing info.json) under an event root.

    Handles both layouts:
    - unlabeled (``00/``): 2-level hash-prefix sharding  → ``00/{2chars}/{hash}/``
    - labeled   (``01/``, ``02/``, ...): flat            → ``{aoiid}/{hash}/``
    """
    for aoiid_dir in sorted(event_root.iterdir()):
        if not aoiid_dir.is_dir() or not _AOIID_DIR_RE.match(aoiid_dir.name):
            continue

        if int(aoiid_dir.name) == 0:
            # Unlabeled: hash-sharded
            for prefix_dir in sorted(aoiid_dir.iterdir()):
                if prefix_dir.is_dir():
                    for tile_dir in sorted(prefix_dir.iterdir()):
                        if tile_dir.is_dir() and (tile_dir / "info.json").exists():
                            yield tile_dir
        else:
            # Labeled: flat
            for tile_dir in sorted(aoiid_dir.iterdir()):
                if tile_dir.is_dir() and (tile_dir / "info.json").exists():
                    yield tile_dir


# ---------------------------------------------------------------------------
# Item builder
# ---------------------------------------------------------------------------


def _asset_key(dataset_name: str, actid: int) -> str:
    """Derive a short asset key from a dataset name.

    ``MS1_IVH_1111002_NA_20200915`` → ``ms1_ivh``
    ``MK0_MNA_1111002_01_20200915`` → ``mk0_mna``
    """
    return dataset_name.split(f"_{actid}_")[0].lower()


def _parse_dt(date_str: str) -> datetime:
    """Parse ISO-like date strings to UTC-aware datetimes."""
    return datetime.fromisoformat(date_str.replace(" ", "T")).replace(tzinfo=timezone.utc)


def build_item(tile_dir: Path, info: dict) -> pystac.Item:
    """Build a :class:`pystac.Item` from a tile directory and its ``info.json``."""
    is_labeled = info.get("aoiid") is not None
    roles_map = _LABELED_ROLES if is_labeled else _UNLABELED_ROLES

    geom_geojson, bbox = _to_wgs84(info["geom"])
    flood_dt = _parse_dt(info["flood_date"])

    source_dates = [_parse_dt(s["source_date"]) for s in info["sources"].values()]

    item = pystac.Item(
        id=f"kurosiwo-{info['actid']}-{info['grid_id']}",
        geometry=geom_geojson,
        bbox=bbox,
        datetime=flood_dt,
        properties={
            "start_datetime": min(source_dates).isoformat(),
            "end_datetime": max(source_dates).isoformat(),
            # KuroSiwo-specific properties (ks: namespace)
            "ks:actid": info["actid"],
            "ks:grid_id": info["grid_id"],
            "ks:flood_date": info["flood_date"],
            "ks:pflood": info.get("pflood"),
            "ks:pwater": info.get("pwater"),
            "ks:pcovered": info.get("pcovered"),
            "ks:slavecov": info.get("slavecov"),
            "ks:mastercov": info.get("mastercov"),
            "ks:gvalid": info.get("gvalid"),
            "ks:aoiid": info.get("aoiid"),
        },
    )

    # --- SAR extension (Sentinel-1 IW GRD — constant across all KuroSiwo tiles) ---
    sar_ext = SarExtension.ext(item, add_if_missing=True)
    pnames = {ds["pname"] for ds in info["datasets"].values()}
    polarizations = [pol for pname, pol in _PNAME_TO_POLARIZATION.items() if pname in pnames]
    sar_ext.apply(
        instrument_mode=_SAR_INSTRUMENT_MODE,
        frequency_band=_SAR_FREQUENCY_BAND,
        center_frequency=_SAR_CENTER_FREQUENCY,
        polarizations=polarizations,
        product_type=_SAR_PRODUCT_TYPE,
        observation_direction=_SAR_OBSERVATION_DIRECTION,
        pixel_spacing_range=_SAR_PIXEL_SPACING_RANGE,
        pixel_spacing_azimuth=_SAR_PIXEL_SPACING_AZIMUTH,
        resolution_range=_SAR_RESOLUTION_RANGE,
        resolution_azimuth=_SAR_RESOLUTION_AZIMUTH,
    )
    # -------------------------------------------------------------------------------

    actid: int = info["actid"]
    for ds_name, ds_meta in info["datasets"].items():
        pname: str = ds_meta["pname"]
        tif_path = tile_dir / f"{ds_name}.tif"

        if not tif_path.exists():
            logger.warning(f"Asset file missing, skipping: {tif_path}")
            continue

        title_base, media_type = _ASSET_META.get(pname, (pname, pystac.MediaType.GEOTIFF))
        roles = roles_map.get(pname, ["data"])

        item.add_asset(
            key=_asset_key(ds_name, actid),
            asset=pystac.Asset(
                href=str(tif_path.resolve()),
                media_type=media_type,
                title=f"{title_base} [{ds_meta['source_date']}]",
                roles=roles,
                extra_fields={
                    "ks:ptype": ds_meta["ptype"],
                    "ks:pname": pname,
                    "ks:master": ds_meta.get("master", False),
                    "ks:source_date": ds_meta["source_date"],
                    "nodata": ds_meta.get("nodata"),
                    "data_type": ds_meta.get("dtype"),
                },
            ),
        )

    return item


# ---------------------------------------------------------------------------
# Collection builder
# ---------------------------------------------------------------------------


def _extent_from_items(items: list[pystac.Item]) -> pystac.Extent:
    bboxes = [item.bbox for item in items if item.bbox]
    datetimes = [item.datetime for item in items if item.datetime]
    spatial = pystac.SpatialExtent(
        bboxes=[
            [
                min(b[0] for b in bboxes),
                min(b[1] for b in bboxes),
                max(b[2] for b in bboxes),
                max(b[3] for b in bboxes),
            ]
        ]
    )
    temporal = pystac.TemporalExtent(intervals=[[min(datetimes), max(datetimes)]])
    return pystac.Extent(spatial=spatial, temporal=temporal)


def build_event_collections(
    actid: int,
    event_root: Path,
) -> tuple[pystac.Collection | None, pystac.Collection | None]:
    """Build labeled and unlabeled sub-collections for a single flood event.

    Returns
    -------
    (labeled_collection, unlabeled_collection)
    Either may be ``None`` if no tiles of that type exist on disk.
    """
    labeled_items: list[pystac.Item] = []
    unlabeled_items: list[pystac.Item] = []

    for tile_dir in _iter_tile_dirs(event_root):
        info_path = tile_dir / "info.json"
        try:
            info = json.loads(info_path.read_text())
        except Exception as exc:
            logger.error(f"Failed to read {info_path}: {exc}")
            continue

        try:
            item = build_item(tile_dir, info)
        except Exception as exc:
            logger.error(f"Failed to build item for {tile_dir}: {exc}")
            continue

        if info.get("aoiid") is not None:
            labeled_items.append(item)
        else:
            unlabeled_items.append(item)

    def _make(items: list[pystac.Item], labeled: bool) -> pystac.Collection:
        tag = "labeled" if labeled else "unlabeled"
        detail = (
            "flood-labeled tiles (aoiid≥1): SAR + flood mask + DEM/MLU/SLOPE"
            if labeled
            else "background tiles (aoiid=null): SAR + water mask only"
        )
        desc = f"KuroSiwo event {actid} — {detail}"
        col = pystac.Collection(
            id=f"kurosiwo-{tag}-{actid}",
            description=desc,
            extent=_extent_from_items(items),
            extra_fields={"ks:actid": actid, "ks:labeled": labeled},
        )
        for item in items:
            col.add_item(item)
        return col

    return (
        _make(labeled_items, labeled=True) if labeled_items else None,
        _make(unlabeled_items, labeled=False) if unlabeled_items else None,
    )


# ---------------------------------------------------------------------------
# Top-level catalog builder
# ---------------------------------------------------------------------------


def _placeholder_extent() -> pystac.Extent:
    """Placeholder extent for top-level grouping collections (updated on normalize)."""
    return pystac.Extent(
        spatial=pystac.SpatialExtent(bboxes=[[-180, -90, 180, 90]]),
        temporal=pystac.TemporalExtent(intervals=[[None, None]]),
    )


def build_catalog(
    kurosiwo_root: Path,
    events: list[int],
    output_dir: Path,
    labeled_only: bool = True,
) -> pystac.Catalog:
    """Build and save a self-contained STAC catalog for the given events.

    Parameters
    ----------
    kurosiwo_root:
        Directory that contains one sub-directory per ``actid``
        (e.g. ``assets/kurosiwo/``).
    events:
        List of ``actid`` integers to include.
    output_dir:
        Directory where the STAC JSON tree will be written
        (e.g. ``data/stac/``).
    labeled_only:
        When ``True`` (default) only the labeled partition (``kurosiwo-labeled``)
        is built and written.  Set to ``False`` to also include the unlabeled
        background tiles (``kurosiwo-unlabeled``).
    """
    catalog = pystac.Catalog(
        id=KS_CATALOG_ID,
        description=KS_DESCRIPTION,
        catalog_type=pystac.CatalogType.SELF_CONTAINED,
    )

    labeled_top = pystac.Collection(
        id=KS_LABELED_ID,
        description=(
            "KuroSiwo labeled partition — tiles with flood labels (aoiid≥1). "
            "Each item carries pflood (patch-level flood %) and a pixel-level flood mask (MNA). "
            "Use this collection for supervised flood-detection model training."
        ),
        extent=_placeholder_extent(),
    )
    unlabeled_top = pystac.Collection(
        id=KS_UNLABELED_ID,
        description=(
            "KuroSiwo unlabeled partition — background tiles (aoiid=null). "
            "SAR amplitude bands + water mask only; no flood labels. "
            "Suitable for self-supervised pre-training or domain adaptation."
        ),
        extent=_placeholder_extent(),
    )
    catalog.add_child(labeled_top)
    if not labeled_only:
        catalog.add_child(unlabeled_top)

    total_labeled = total_unlabeled = 0

    for actid in events:
        event_root = kurosiwo_root / str(actid)
        if not event_root.exists():
            logger.warning(f"Event directory not found: {event_root} — skipping")
            continue

        logger.info(f"Processing event actid={actid} …")
        labeled_col, unlabeled_col = build_event_collections(actid, event_root)

        if labeled_col is not None:
            labeled_top.add_child(labeled_col)
            n = sum(1 for _ in labeled_col.get_items())
            total_labeled += n
            logger.info(f"  actid={actid} labeled:   {n} items")

        if not labeled_only and unlabeled_col is not None:
            unlabeled_top.add_child(unlabeled_col)
            n = sum(1 for _ in unlabeled_col.get_items())
            total_unlabeled += n
            logger.info(f"  actid={actid} unlabeled: {n} items")

    logger.info(
        f"Catalog built — {total_labeled} labeled items"
        + (f", {total_unlabeled} unlabeled items" if not labeled_only else " (labeled-only mode)")
        + f" across {len(events)} event(s)."
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    catalog.normalize_hrefs(str(output_dir))
    catalog.save(catalog_type=pystac.CatalogType.SELF_CONTAINED)
    logger.info(f"Catalog saved → {output_dir}")

    return catalog


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

app = typer.Typer(help="Generate a KuroSiwo STAC catalog from the on-disk dataset.")


@app.command()
def main(
    event: list[int] = typer.Option(
        [1111002],
        "--event",
        "-e",
        help="actid(s) to include. Repeat for multiple events.",
    ),
    root: Path = typer.Option(
        Path("assets/kurosiwo"),
        "--root",
        "-r",
        help="KuroSiwo root directory (contains one sub-dir per actid).",
    ),
    output: Path = typer.Option(
        Path("data/stac"),
        "--output",
        "-o",
        help="Output directory for the static STAC catalog JSON tree.",
    ),
    labeled_only: bool = typer.Option(
        True,
        "--labeled/--all",
        help="Build only the labeled partition (default). Pass --all to also include unlabeled background tiles.",
    ),
) -> None:
    build_catalog(root, list(event), output, labeled_only=labeled_only)


if __name__ == "__main__":
    app()
