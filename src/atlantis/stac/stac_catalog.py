r"""Build a self-contained STAC catalog for the KuroSiwo SAR flood dataset stored in S3.

S3 input layout
---------------
s3://{bucket}/{kurosiwo_prefix}/{actid}/
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
    # PoC — single event (default bucket=atlantis, prefix=kurosiwo, output=stac)
    python -m atlantis.stac.stac_catalog --event 1111002

    # All events
    python -m atlantis.stac.stac_catalog --event 1111002 --event 1111003 ...

    # Custom S3 locations
    python -m atlantis.stac.stac_catalog \
        --bucket my-bucket \
        --prefix datasets/kurosiwo \
        --output catalogs/kurosiwo-stac \
        --event 1111002
"""

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import boto3
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
    "43 real flood events. "
    "Source: Orion-AI-Lab/KuroSiwo — SAR Sentinel-1 GRD, EPSG:3857, 224×224 px tiles (~2.24 km)."
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
# S3 tile discovery
# ---------------------------------------------------------------------------


def _iter_tile_prefixes(
    s3: Any,
    bucket: str,
    actid_prefix: str,
) -> Iterator[tuple[str, set[str]]]:
    """Yield ``(tile_prefix, sibling_keys)`` for every tile under *actid_prefix* in S3.

    *tile_prefix* is the S3 key prefix of the tile directory (ending with ``/``),
    e.g. ``kurosiwo/1111002/01/09dd5f42e6845c5782d8141078640c62/``.

    *sibling_keys* is the set of all S3 keys sharing that prefix (used to check
    asset existence without additional ``head_object`` calls).

    Handles both on-disk layouts as stored in S3:
    - unlabeled (``00/``): 2-level hash-prefix sharding  → ``00/{2chars}/{hash}/``
    - labeled   (``01/``, ``02/``, ...): flat            → ``{aoiid}/{hash}/``
    """
    paginator = s3.get_paginator("list_objects_v2")

    # Collect all keys under the actid prefix in one paginated pass.
    # Group them by their tile prefix (everything up to and including the
    # hash-directory component).
    tile_keys: dict[str, set[str]] = {}

    for page in paginator.paginate(Bucket=bucket, Prefix=actid_prefix):
        for obj in page.get("Contents", []):
            key: str = obj["Key"]
            # Determine the tile prefix from the key depth.
            # Key structure relative to actid_prefix:
            #   labeled:   {aoiid}/{hash}/{filename}        → depth 3 from actid_prefix
            #   unlabeled: 00/{2chars}/{hash}/{filename}    → depth 4 from actid_prefix
            relative = key[len(actid_prefix) :]  # strip leading prefix
            parts = relative.split("/")

            aoiid = parts[0]
            if not aoiid.isdigit():
                continue  # skip unexpected keys

            if int(aoiid) == 0 and len(parts) >= 4:
                # unlabeled: parts = ['00', '<2chars>', '<hash>', '<filename>']
                tile_prefix = actid_prefix + "/".join(parts[:3]) + "/"
            elif int(aoiid) != 0 and len(parts) >= 3:
                # labeled: parts = ['<aoiid>', '<hash>', '<filename>']
                tile_prefix = actid_prefix + "/".join(parts[:2]) + "/"
            else:
                continue

            tile_keys.setdefault(tile_prefix, set()).add(key)

    for tile_prefix, keys in sorted(tile_keys.items()):
        if tile_prefix + "info.json" in keys:
            yield tile_prefix, keys


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


def build_item(
    tile_prefix: str,
    bucket: str,
    info: dict,
    existing_keys: set[str],
) -> pystac.Item:
    """Build a :class:`pystac.Item` from an S3 tile prefix and its parsed ``info.json``.

    Parameters
    ----------
    tile_prefix:
        S3 key prefix of the tile directory (ending with ``/``).
    bucket:
        S3 bucket name.
    info:
        Parsed contents of the tile's ``info.json``.
    existing_keys:
        Set of S3 keys known to exist under *tile_prefix* (used to skip
        missing assets without additional ``head_object`` calls).
    """
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
        tif_key = f"{tile_prefix}{ds_name}.tif"

        if tif_key not in existing_keys:
            logger.warning(f"Asset key missing in S3, skipping: s3://{bucket}/{tif_key}")
            continue

        title_base, media_type = _ASSET_META.get(pname, (pname, pystac.MediaType.GEOTIFF))
        roles = roles_map.get(pname, ["data"])

        item.add_asset(
            key=_asset_key(ds_name, actid),
            asset=pystac.Asset(
                href=f"s3://{bucket}/{tif_key}",
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
    s3: Any,
    bucket: str,
    kurosiwo_prefix: str,
) -> tuple[pystac.Collection | None, pystac.Collection | None]:
    """Build labeled and unlabeled sub-collections for a single flood event from S3.

    Parameters
    ----------
    actid:
        Flood event activation ID.
    s3:
        Boto3 S3 client.
    bucket:
        S3 bucket name.
    kurosiwo_prefix:
        S3 key prefix for the kurosiwo root (e.g. ``kurosiwo/``).

    Returns:
    -------
    (labeled_collection, unlabeled_collection)
        Either may be ``None`` if no tiles of that type exist in S3.
    """
    actid_prefix = f"{kurosiwo_prefix}{actid}/"
    labeled_items: list[pystac.Item] = []
    unlabeled_items: list[pystac.Item] = []

    for tile_prefix, existing_keys in _iter_tile_prefixes(s3, bucket, actid_prefix):
        info_key = f"{tile_prefix}info.json"
        try:
            body = s3.get_object(Bucket=bucket, Key=info_key)["Body"].read()
            info = json.loads(body)
        except Exception as exc:
            logger.error(f"Failed to read s3://{bucket}/{info_key}: {exc}")
            continue

        try:
            item = build_item(tile_prefix, bucket, info, existing_keys)
        except Exception as exc:
            logger.error(f"Failed to build item for s3://{bucket}/{tile_prefix}: {exc}")
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


def _save_catalog_to_s3(
    catalog: pystac.Catalog,
    s3: Any,
    bucket: str,
    output_prefix: str,
) -> None:
    """Serialize a STAC catalog to S3.

    Uses a temporary local directory as an intermediate staging area (required
    by pystac's ``save()`` API), then uploads every ``.json`` file to S3 under
    *output_prefix*.

    Parameters
    ----------
    catalog:
        The fully-assembled in-memory STAC catalog.
    s3:
        Boto3 S3 client.
    bucket:
        Destination S3 bucket.
    output_prefix:
        S3 key prefix for the catalog root (e.g. ``stac/kurosiwo``).
        A trailing ``/`` is added if absent.
    """
    if not output_prefix.endswith("/"):
        output_prefix += "/"

    with tempfile.TemporaryDirectory() as tmp_dir:
        catalog.normalize_hrefs(tmp_dir)
        catalog.save(catalog_type=pystac.CatalogType.SELF_CONTAINED)

        tmp_root = Path(tmp_dir)
        uploaded = 0
        for json_path in tmp_root.rglob("*.json"):
            relative = json_path.relative_to(tmp_root)
            s3_key = output_prefix + str(relative)
            s3.upload_file(
                Filename=str(json_path),
                Bucket=bucket,
                Key=s3_key,
                ExtraArgs={"ContentType": "application/json"},
            )
            uploaded += 1

    logger.info(f"Catalog saved → s3://{bucket}/{output_prefix} ({uploaded} JSON files)")


def build_catalog(
    bucket: str,
    kurosiwo_prefix: str,
    events: list[int],
    output_prefix: str,
    labeled_only: bool = True,
) -> pystac.Catalog:
    """Build and save a self-contained STAC catalog for the given events from S3.

    Parameters
    ----------
    bucket:
        S3 bucket that holds the KuroSiwo data (e.g. ``atlantis``).
    kurosiwo_prefix:
        S3 key prefix for the kurosiwo root within the bucket
        (e.g. ``kurosiwo``).  A trailing ``/`` is added if absent.
    events:
        List of ``actid`` integers to include.
    output_prefix:
        S3 key prefix where the STAC catalog JSON tree will be written
        (e.g. ``stac``).
    labeled_only:
        When ``True`` (default) only the labeled partition is built.
        Set to ``False`` to also include unlabeled background tiles.
    """
    if not kurosiwo_prefix.endswith("/"):
        kurosiwo_prefix += "/"

    s3: Any = boto3.client("s3")

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
        # Quick existence check: does any key exist under the actid prefix?
        actid_prefix = f"{kurosiwo_prefix}{actid}/"
        probe = s3.list_objects_v2(Bucket=bucket, Prefix=actid_prefix, MaxKeys=1)
        if not probe.get("Contents"):
            logger.warning(f"No S3 objects found under s3://{bucket}/{actid_prefix} — skipping")
            continue

        logger.info(f"Processing event actid={actid} …")
        labeled_col, unlabeled_col = build_event_collections(actid, s3, bucket, kurosiwo_prefix)

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

    _save_catalog_to_s3(catalog, s3, bucket, output_prefix)

    return catalog


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

app = typer.Typer(help="Generate a KuroSiwo STAC catalog from data stored in S3.")


@app.command()
def main(
    event: list[int] = typer.Option(
        [1111002],
        "--event",
        "-e",
        help="actid(s) to include. Repeat for multiple events.",
    ),
    bucket: str = typer.Option(
        "atlantis",
        "--bucket",
        "-b",
        help="S3 bucket that holds the KuroSiwo data.",
    ),
    prefix: str = typer.Option(
        "kurosiwo",
        "--prefix",
        "-p",
        help="S3 key prefix for the kurosiwo root within the bucket.",
    ),
    output: str = typer.Option(
        "stac",
        "--output",
        "-o",
        help="S3 key prefix where the STAC catalog JSON tree will be written.",
    ),
    labeled_only: bool = typer.Option(
        True,
        "--labeled/--all",
        help="Build only the labeled partition (default). Pass --all to also include unlabeled tiles.",
    ),
) -> None:
    """Build a KuroSiwo STAC catalogue for the requested events and upload it to S3."""
    build_catalog(bucket, prefix, list(event), output, labeled_only=labeled_only)


if __name__ == "__main__":
    app()
