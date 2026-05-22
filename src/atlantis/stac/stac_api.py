"""Query STAC api for source Sentinel-1 GRD scenes using the KuroSiwo catalogue."""

import json
import os
import subprocess
from datetime import timedelta
from pathlib import Path

import geopandas as gpd
from loguru import logger
from pystac_client import Client

os.environ["GDAL_HTTP_TCP_KEEPALIVE"] = "YES"
os.environ["AWS_PROFILE"] = "eodata"  # Loads credentials from ~/.aws/credentials
os.environ["AWS_S3_ENDPOINT"] = "eodata.dataspace.copernicus.eu"
os.environ["AWS_HTTPS"] = "YES"
os.environ["AWS_VIRTUAL_HOSTING"] = "FALSE"
os.environ["GDAL_HTTP_UNSAFESSL"] = "YES"

CATALOGUE_PATH = Path(__file__).parent.parent.parent / "assets" / "ks_catalogue.gpkg"
URL = "https://stac.dataspace.copernicus.eu/v1"
COLLECTION = "sentinel-1-grd"
KS_CRS = "EPSG:3857"
WGS84 = "EPSG:4326"


def get_catalogue(catalogue_path: Path = CATALOGUE_PATH) -> gpd.GeoDataFrame:
    def is_lfs_pointer(path: Path) -> bool:
        try:
            with open(path, "r", encoding="utf8") as fh:
                first = fh.readline()
            return first.startswith("version https://git-lfs.github.com/spec")
        except Exception:
            return False

    # Ensure the asset is present and not an LFS pointer; try to fetch if needed
    if catalogue_path.exists():
        if is_lfs_pointer(catalogue_path):
            try:
                subprocess.run(["git", "lfs", "pull"], check=True)
            except subprocess.CalledProcessError:
                raise RuntimeError(
                    "`git lfs pull` failed; probably git lfs is not installed. Try: git lfs install, then retry."
                )
            else:
                logger.info(f"Catalogue ready at {catalogue_path}")
    else:
        raise FileNotFoundError(f"Catalogue not found at {catalogue_path}. Run `git lfs pull` to fetch it.")

    return gpd.read_file(str(catalogue_path))


def get_scenes_for_actid(actid: int, catalogue_path: Path = CATALOGUE_PATH) -> list[dict]:
    """Extract unique Sentinel-1 scenes for a given activation ID from the catalogue.

    Groups catalogue entries by ``source_date``, computes the WGS84 bounding box
    of all tile geometries, and collects the associated ``s1_ids`` for each scene.

    Returns a list of dicts with keys: ``source_date``, ``s1_ids``, ``bbox``.
    """
    gdf = get_catalogue(catalogue_path)
    sub = gdf[gdf["actid"] == actid]
    if sub.empty:
        raise ValueError(f"No catalogue entries found for actid={actid}")

    sub_wgs84 = sub.to_crs(WGS84)

    scenes = []
    for source_date, group in sub_wgs84.groupby("source_date"):
        s1_ids: set[str] = set()
        for ids_str in group["s1_ids"]:
            s1_ids.update(json.loads(ids_str))

        bbox = group.geometry.total_bounds.tolist()  # [minx, miny, maxx, maxy]
        scenes.append(
            {
                "source_date": source_date,
                "s1_ids": sorted(s1_ids),
                "bbox": bbox,
            }
        )

    logger.info(f"actid={actid}: {len(scenes)} unique scene(s) found in catalogue")
    return scenes


def query_stac_for_scenes(scenes: list[dict]) -> list[dict]:
    """Query the STAC API for Sentinel-1 GRD items matching the given scenes.

    For each scene, searches by the WGS84 bounding box and a full-day datetime
    window derived from ``source_date``.
    """
    cat = Client.open(URL)
    cat.add_conforms_to("ITEM_SEARCH")

    all_items: list[dict] = []
    for scene in scenes:
        date = scene["source_date"]
        date_str = str(date)[:10]  # YYYY-MM-DD
        next_date = (date + timedelta(days=1)).strftime("%Y-%m-%d") if hasattr(date, "strftime") else date_str
        datetime_range = f"{date_str}T00:00:00Z/{next_date}T00:00:00Z"

        params = {
            "collections": [COLLECTION],
            "datetime": datetime_range,
            "bbox": scene["bbox"],
        }
        items = list(cat.search(**params).items_as_dicts())
        logger.info(f"source_date={date_str}, bbox={[round(v, 4) for v in scene['bbox']]} -> {len(items)} item(s)")
        all_items.extend(items)

    return all_items


def main():
    actid = 1111002

    scenes = get_scenes_for_actid(actid)
    items = query_stac_for_scenes(scenes)

    logger.info(f"Total STAC items retrieved: {len(items)}")
    for item in items:
        print(item["id"], item.get("properties", {}).get("datetime", ""))


if __name__ == "__main__":
    main()
