import glob
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import odc.stac
import rasterio
import rioxarray  # noqa
import xarray as xr
from pystac_client import Client
from rasterio.enums import Resampling
from rasterio.transform import from_bounds
from rasterio.windows import Window
from shapely.geometry import box

# %% [markdown]
# ## Search and load data
#
# We will define our area (AOI) and time range of interest for which we want
# to calculate the maximum flood extent for. For defining a bounding box, you can
# use [this web tool](http://bboxfinder.com).
#
# All GFM data is registered as a [STAC](https://stacspec.org/en/) collection.
# Please find more information about STAC in our [documentation](https://docs.eodc.eu/services/stac.html).


# %%
def search_subdomain(area, date, days):
    """Search the GFM collection on EODC STAC for the given area and time range.
    Returns a list of STAC items."""
    # Define the API URL to search the GFM stac
    api_url = "https://stac.eodc.eu/api/v1"

    # Define the STAC collection ID
    collection_id = "GFM"

    aoi = box(area[1], area[0], area[3], area[2])
    s = str(date)
    year = int(s[0:4])
    month = int(s[4:6])
    day = int(s[6:8])
    # Define the time range for the search
    #  time_range = (datetime(year, month, day, 0), datetime(year, month, day+days-1, 23))
    start = datetime(year, month, day, 0, 0, 0)
    end = start + timedelta(days=days) - timedelta(seconds=1)  # inclusive end
    print("Extraction period from", start, "to", end)
    time_range = (start, end)
    # Open the STAC catalog using the specified API URL
    eodc_catalog = Client.open(api_url)

    # Perform a search in the catalog with the specified parameters
    search = eodc_catalog.search(
        max_items=1000,  # Maximum number of items to return
        collections=collection_id,  # The collection to search within
        intersects=aoi,  # The area of interest
        datetime=time_range,  # The time range for the search
    )

    # Collect the found items into an item collection
    items = search.item_collection()

    return items


def extract_subdomain(aa, items, obs, msk, resolution_deg=1 / 60):
    """Extract the maximum flood extent for the given area and list of STAC items.
    Writes the output to the specified observation and mask files."""
    from odc.geo.geobox import GeoBox

    aoi = box(aa[1], aa[0], aa[3], aa[2])

    # Define a fixed output grid: all items will be loaded onto the same pixel grid
    gbox = GeoBox.from_bbox(aoi.bounds, crs="EPSG:4326", resolution=resolution_deg)
    print(f"Output GeoBox: {gbox.shape} pixels at {resolution_deg}° resolution")

    sum_flood = None
    sum_missing = None

    # Debug directory: native and resampled TIFs saved here for visual comparison
    # switch the the parent directory of current module
    debug_dir = Path(__file__).parent / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)

    # Tell ODC that 255 is the source nodata for both bands.
    # This causes those pixels to be masked *before* resampling, so max()
    # only considers valid flood class values (0=dry, 1=flood, 2=perm-water).
    gfm_stac_cfg = {
        "GFM": {
            "assets": {
                "ensemble_flood_extent": {"data_type": "uint8", "nodata": 255},
                "reference_water_mask": {"data_type": "uint8", "nodata": 255},
            }
        }
    }

    print("\n=== Running flood extraction ===")

    for idx, titem in enumerate(items):
        # yy = odc.stac.load(
        #     [titem],
        #     bands=["ensemble_flood_extent", "reference_water_mask"],
        #     chunks={},
        # )
        # print("Printing layers in native resolution for debugging:\nensemble_flood_extent:")
        # print(yy["ensemble_flood_extent"].values)
        # print("reference_water_mask:")
        # print(yy["reference_water_mask"].values)

        # # Save native-resolution bands for visual comparison
        # native_crs = yy.odc.crs.to_wkt()
        # yy["ensemble_flood_extent"].isel(time=0).rio.write_crs(native_crs).rio.to_raster(
        #     debug_dir / f"item_{idx:03d}_native_flood.tif", compress="LZW"
        # )
        # yy["reference_water_mask"].isel(time=0).rio.write_crs(native_crs).rio.to_raster(
        #     debug_dir / f"item_{idx:03d}_native_perm.tif", compress="LZW"
        # )
        # print(f"  Saved native TIFs to {debug_dir}/item_{idx:03d}_native_*.tif")
        resampling_method = "bilinear"  # or "nearest" depending on the desired resampling

        xx = odc.stac.load(
            [titem],
            geobox=gbox,
            bands=["ensemble_flood_extent", "reference_water_mask"],
            resampling=resampling_method,
            stac_cfg=gfm_stac_cfg,
            use_overviews=False,  # 🔴 IMPORTANT: disable overviews to ensure nodata handling works correctly
            chunks={},
            fail_on_error=True,
        )

        flood_arr = xx["ensemble_flood_extent"].values
        perm_arr = xx["reference_water_mask"].values
        if np.all(flood_arr == 255):
            print(
                f"Item {idx + 1}/{len(items)} WARNING: ensemble_flood_extent is all nodata (255) after loading. Check the item and STAC configuration."
            )
        else:
            print(
                f"Item {idx + 1}/{len(items)} loaded successfully with valid data. \n"
                f"Flood extent has {np.sum(flood_arr != 255)} valid pixels."
            )
        if np.all(perm_arr == 255):
            print(
                f"Item {idx + 1}/{len(items)} WARNING: reference_water_mask is all nodata (255) after loading. Check the item and STAC configuration."
            )
        else:
            print(
                f"Item {idx + 1}/{len(items)} loaded successfully with valid data. \n"
                f"Reference water mask has {np.sum(perm_arr != 255)} valid pixels."
            )
        print(flood_arr)
        print(perm_arr)

        # Save resampled (EPSG:4326) bands for visual comparison
        xx["ensemble_flood_extent"].isel(time=0).rio.write_crs("EPSG:4326").rio.to_raster(
            debug_dir / f"item_{idx:03d}_resampled_{resampling_method}_flood.tif", compress="LZW"
        )
        xx["reference_water_mask"].isel(time=0).rio.write_crs("EPSG:4326").rio.to_raster(
            debug_dir / f"item_{idx:03d}_resampled_{resampling_method}_perm.tif", compress="LZW"
        )
        print(f"  Saved resampled TIFs to {debug_dir}/item_{idx:03d}_resampled_{resampling_method}_*.tif")

        ### ------- ###

        # nodata from odc.stac is 0 for uint8 by default when pixel is outside footprint
        # GFM encoding: 0=nodata/outside, 1=no-water, 2=flood, 3=permanent-water (for flood extent)
        # Check actual nodata value
        flood_nodata = xx["ensemble_flood_extent"].attrs.get("nodata", 255)
        perm_nodata = xx["reference_water_mask"].attrs.get("nodata", 255)

        if sum_flood is None:
            shape = flood_arr.shape
            sum_flood = np.zeros(shape, dtype="float32")
            sum_missing = np.zeros(shape, dtype="uint16")
            coords = xx["ensemble_flood_extent"].coords
            dims = xx["ensemble_flood_extent"].dims

        # Count flood pixels (not nodata, not zero/dry)
        flood = np.where((flood_arr != flood_nodata) & (flood_arr != 0), flood_arr.astype("float32"), 0.0)

        # Mark pixels that had any valid observation
        missing = np.where(
            ((flood_arr != flood_nodata) | (perm_arr != perm_nodata)) & ((flood_arr != 0) | (perm_arr != 0)),
            1,
            0,
        ).astype("uint16")

        sum_flood += flood
        sum_missing += missing

        del xx, flood_arr, perm_arr, flood, missing
        print(f"Item {idx + 1}/{len(items)} OK")

    obs_da = xr.DataArray(sum_flood, coords=coords, dims=dims).rio.write_crs("EPSG:4326")

    obs_da = obs_da.fillna(0)
    obs_da.values[np.isnan(obs_da.values)] = 0
    obs_da.rio.to_raster(obs, compress="LZW")

    msk_da = xr.DataArray((sum_missing >= 1).astype("uint8"), coords=coords, dims=dims).rio.write_crs("EPSG:4326")

    msk_da.values[np.isnan(msk_da.values)] = 0
    msk_da.rio.to_raster(msk, compress="LZW")

    print("\n=== DONE ===\n")
    return gbox


# Define the area of interest (AOI), the date and days of the search

flood_case = "SriLanka"
date = 20251126
days = 1
area = [5, 80, 10, 82]  # SriLanka

flood_case = "Thessaly"
date = 20230907
days = 1
area = [36, 20, 41, 25]  # Thessaly

flood_case = "Germany"
date = 20210715
days = 3
area = [44, 0.0, 54.0, 10.0]  # Germany

flood_case = "Valencia"
date = 20241030
days = 2
area = [37, -2.5, 41, 1.5]  # Valencia

flood_case = "Oregon"
date = 20251209
days = 4
area = [42, -128, 52, -118]

flood_case = "EmiliaRomagna"
date = 20230503
days = 2
area = [46, 9, 43, 14]  # EmiliaRomagna

flood_case = "Pakistan"
date = 20220830
days = 1
area = [22, 66, 31, 72]  # Pakistan
area = [24, 67.5, 29, 70]  # Pakistan small area

flood_case = "Australia"
date = 20250325
days = 5
date = 20251231
days = 1
date = 20260103
days = 1
area = [-35, 135, -10, 155]  # Australia
area = [-20, 142, -15, 150]  # Australia
area = [-25, 135, -10, 155]  # Australia

flood_case = "SouthAfrica"
date = 20260124
area = [-15, 20, -35, 40]
area = [-20, 30, -28, 38]

flood_case = "Spain"
date = 20260206
area = [31.5, -10, 40, 0]  # Spain

flood_case = "France"
date = 20260212
days = 2
area = [42, -2, 46, 2]  # France

flood_case = "Persia"
area = [35, 40, 30, 45]
area = [35, 43, 29, 50]
date = 20260330
days = 2

flood_case = "SouthItaly"
area = [43, 12, 36, 19]  # SouthItaly
date = 20260402
days = 2

flood_case = "Yangtze"
area = [28, 105, 38, 125]  # Yangtze
date = 20200722
days = 1

obs = "/perm/pad/flood_cases/" + flood_case + "_flood_obs_GFM.tif"
msk = "/perm/pad/flood_cases/" + flood_case + "_flood_mask_GFM.tif"

# %%
# Search and extract. NOTE: Below an alternative version seful for large domains
items = search_subdomain(area, date, days)
print(f"On EODC we found {len(items)} items for the given search query")
if len(items) > 0:
    trs = extract_subdomain(area, items, obs, msk)
    print(f"Coordinates {trs} ")


path_obs = "/perm/pad/flood_cases/" + flood_case + "_flood_obs_GFM.tif"
path_mask = "/perm/pad/flood_cases/" + flood_case + "_flood_mask_GFM.tif"


def read_downsampled(path, factor=10):
    """Read a raster file and downsample it by the given factor using nearest neighbor resampling.
    Returns the downsampled data."""
    with rasterio.open(path) as ds:
        arr = ds.read(1, out_shape=(1, ds.height // factor, ds.width // factor), resampling=Resampling.nearest)
    return arr


obs = read_downsampled(path_obs, factor=10)
mask = read_downsampled(path_mask, factor=10)

# Plot
plt.figure(figsize=(14, 5))

plt.subplot(1, 2, 1)
plt.imshow(obs, cmap="Blues", vmin=0, vmax=1)
plt.title(flood_case + " — Flood Observations")
plt.colorbar(fraction=0.046, pad=0.04)

plt.subplot(1, 2, 2)
plt.imshow(mask, cmap="Reds", vmin=0, vmax=1)
plt.title(flood_case + " — Flood Mask")
plt.colorbar(fraction=0.046, pad=0.04)

plt.tight_layout()
plt.show()


def read_downsampled(path, factor=10):
    """Read a raster file and downsample it by the given factor using nearest neighbor resampling.
    Returns the downsampled data and the new transform."""
    with rasterio.open(path) as ds:
        data = ds.read(1, out_shape=(ds.height // factor, ds.width // factor), resampling=Resampling.nearest)

        # Scale transform accordingly
        transform = ds.transform * ds.transform.scale(ds.width / data.shape[1], ds.height / data.shape[0])

    return data, transform


def transform_to_extent(transform, shape):
    """Convert a rasterio transform and shape to a geographic extent [lon_min, lon_max, lat_min, lat_max]."""
    height, width = shape
    lon_min = transform.c
    lon_max = transform.c + transform.a * width
    lat_max = transform.f
    lat_min = transform.f + transform.e * height  # e < 0
    return [lon_min, lon_max, lat_min, lat_max]


flood_case = "Pakistan"
date = 20220830
days = 1
area = [24, 67.5, 29, 70]  # Pakistan
path_obs = "/perm/pad/flood_cases/" + flood_case + "_flood_obs.tif"
path_mask = "/perm/pad/flood_cases/" + flood_case + "_flood_mask.tif"

obs, obs_tr = read_downsampled(path_obs, factor=10)
mask, mask_tr = read_downsampled(path_mask, factor=10)
extent = transform_to_extent(obs_tr, obs.shape)

# --------------------------------------------------
# Plot geographic extent
# --------------------------------------------------
plt.figure(figsize=(14, 5))

plt.subplot(1, 2, 1)
plt.imshow(
    obs,
    cmap="Blues",
    vmin=0,
    vmax=1,
    extent=extent,
    origin="upper",  # 🔴 REQUIRED
)
plt.title(flood_case + " — Flood Observations")
plt.xlabel("Longitude")
plt.ylabel("Latitude")
plt.colorbar(fraction=0.046, pad=0.04)

plt.subplot(1, 2, 2)
plt.imshow(mask, cmap="Reds", vmin=0, vmax=1, extent=extent, origin="upper")
plt.title(flood_case + " — Flood Mask")
plt.xlabel("Longitude")
plt.ylabel("Latitude")
plt.colorbar(fraction=0.046, pad=0.04)

plt.tight_layout()
plt.show()


def make_subdomains(area, delta=1):
    """Split a geographic area into tiles of size delta × delta degrees.
    area = [south, west, north, east] ; delta = tile size in degrees
    """
    border = 0.0
    south, west, north, east = area
    subdomains = []
    lat = south - border
    while lat < north + border:
        next_lat = min(lat + delta, north)
        lon = west - border
        while lon < east + border:
            next_lon = min(lon + delta, east)
            subdomains.append([lat, lon, next_lat, next_lon])
            lon += delta
        lat += delta
    return subdomains


# %%
# area = [22, 66, 31, 72] # Pakistan Large Area
tiles_deg = make_subdomains(area, delta=4.0)
print("Splitting into " + str(len(tiles_deg)) + " domains ")
print(tiles_deg)

# %%
# Removing previous tiles if present
# !ls /perm/pad/flood_cases/{flood_case}_flood_obs_*.tif
# !ls /perm/pad/flood_cases/{flood_case}_flood_mask_*.tif
# !rm -rf /perm/pad/flood_cases/{flood_case}_flood_obs_*.tif
# !rm -rf /perm/pad/flood_cases/{flood_case}_flood_mask_*.tif

# %%
n = 0
for tile in tiles_deg:
    items = []
    items = search_subdomain(tile, date, days)
    print(f"{str(n + 1)} / {str(len(tiles_deg))} On EODC found {len(items)} EO items for area={tile} ")
    obs = "/perm/pad/flood_cases/" + flood_case + "_flood_obs_" + str(n) + ".tif"
    msk = "/perm/pad/flood_cases/" + flood_case + "_flood_mask_" + str(n) + ".tif"
    if len(items) > 0:
        xx = extract_subdomain(tile, items, obs, msk)
    n += 1

# %%
# BBOX check for debugging difficult
debug = 0
if debug == 1:
    import glob

    import rasterio

    for f in sorted(glob.glob("/perm/pad/flood_cases/" + flood_case + "_flood_obs_*.tif")):
        with rasterio.open(f) as src:
            print(f)
            print(" CRS:", src.crs)
            print(" Transform:", src.transform)
            print(" Shape:", src.height, src.width)
            print(" Bounds:", src.bounds)
            print()

    for f in sorted(glob.glob("/perm/pad/flood_cases/" + flood_case + "_flood_obs.tif")):
        with rasterio.open(f) as src:
            print(f)
            print(" CRS:", src.crs)
            print(" Transform:", src.transform)
            print(" Shape:", src.height, src.width)
            print(" Bounds:", src.bounds)
            print()


tiles = sorted(glob.glob("/perm/pad/flood_cases/" + flood_case + "_flood_obs_*.tif"))

# --- compute full geographic extent ---
minx = +1e9
miny = +1e9
maxx = -1e9
maxy = -1e9

for t in tiles:
    with rasterio.open(t) as src:
        b = src.bounds
        minx = min(minx, b.left)
        miny = min(miny, b.bottom)
        maxx = max(maxx, b.right)
        maxy = max(maxy, b.top)
        res = src.res  # assuming same for all tiles
        crs = src.crs

resx, resy = res
width = int(np.round((maxx - minx) / resx))
height = int(np.round((maxy - miny) / abs(resy)))

transform = from_bounds(minx, miny, maxx, maxy, width, height)
out_nodata = 0.0


# %%
out_file = "/perm/pad/flood_cases/" + flood_case + "_flood_obs.tif"

profile = {
    "driver": "GTiff",
    "height": height,
    "width": width,
    "count": 1,
    "dtype": "float32",
    "crs": crs,
    "transform": transform,
    "nodata": out_nodata,
    "compress": "LZW",
}

with rasterio.open(out_file, "w", **profile) as dst:
    dst.write(np.full((1, height, width), out_nodata, dtype="float32"))


# %%
def intersection(dst, src):
    """Compute intersection window in both dst and src pixel grids."""
    # Bounds in geographic coords
    L = max(dst.bounds.left, src.bounds.left)
    R = min(dst.bounds.right, src.bounds.right)
    B = max(dst.bounds.bottom, src.bounds.bottom)
    T = min(dst.bounds.top, src.bounds.top)

    if L >= R or B >= T:
        return None  # no overlap

    # Convert to pixel coords in dst
    dst_win = rasterio.windows.from_bounds(L, B, R, T, dst.transform)
    dst_win = dst_win.round_offsets().round_lengths()

    # Convert to pixel coords in src
    src_win = rasterio.windows.from_bounds(L, B, R, T, src.transform)
    src_win = src_win.round_offsets().round_lengths()

    # Ensure sizes match exactly
    h = min(int(dst_win.height), int(src_win.height))
    w = min(int(dst_win.width), int(src_win.width))

    dst_win = Window(dst_win.col_off, dst_win.row_off, w, h)
    src_win = Window(src_win.col_off, src_win.row_off, w, h)

    return dst_win, src_win


with rasterio.open(out_file, "r+") as dst:
    for t in tiles:
        print("Processing tile: ", t)

        with rasterio.open(t) as src:
            res = intersection(dst, src)
            if res is None:
                continue
            dst_win, src_win = res

            # read blocks
            tile = src.read(1, window=src_win).astype("float32")
            existing = dst.read(1, window=dst_win).astype("float32")

            # handle nodata on tile
            nod = src.nodata
            if nod is not None:
                tile = np.where(tile == nod, np.nan, tile)
            else:
                tile = np.where(tile == 255.0, np.nan, tile)

            existing = np.where(existing == out_nodata, np.nan, existing)

            # merge using max (nan-safe)
            merged = np.fmax(existing, tile)
            merged = np.where(np.isnan(merged), out_nodata, merged)

            # write the block
            dst.write(merged, 1, window=dst_win)

print("Done!", out_file)


# %%
tiles = sorted(glob.glob("/perm/pad/flood_cases/" + flood_case + "_flood_mask_*.tif"))

# --- compute full geographic extent ---
minx = +1e9
miny = +1e9
maxx = -1e9
maxy = -1e9

for t in tiles:
    with rasterio.open(t) as src:
        b = src.bounds
        minx = min(minx, b.left)
        miny = min(miny, b.bottom)
        maxx = max(maxx, b.right)
        maxy = max(maxy, b.top)
        res = src.res  # assuming same for all tiles
        crs = src.crs

resx, resy = res
width = int(np.round((maxx - minx) / resx))
height = int(np.round((maxy - miny) / abs(resy)))

transform = from_bounds(minx, miny, maxx, maxy, width, height)
out_nodata = 0.0

out_file = "/perm/pad/flood_cases/" + flood_case + "_flood_mask.tif"

profile = {
    "driver": "GTiff",
    "height": height,
    "width": width,
    "count": 1,
    "dtype": "float32",
    "crs": crs,
    "transform": transform,
    "nodata": out_nodata,
    "compress": "LZW",
}

with rasterio.open(out_file, "w", **profile) as dst:
    dst.write(np.full((1, height, width), out_nodata, dtype="float32"))

with rasterio.open(out_file, "r+") as dst:
    for t in tiles:
        print("Processing tile: ", t)

        with rasterio.open(t) as src:
            res = intersection(dst, src)
            if res is None:
                continue
            dst_win, src_win = res

            # read blocks
            tile = src.read(1, window=src_win).astype("float32")
            existing = dst.read(1, window=dst_win).astype("float32")

            # merge using max (nan-safe)
            merged = np.fmax(existing, tile)
            merged = np.where(np.isnan(merged), out_nodata, merged)

            # write the block
            dst.write(merged, 1, window=dst_win)

print("Done!", out_file)


path_obs = "/perm/pad/flood_cases/" + flood_case + "_flood_obs.tif"
path_mask = "/perm/pad/flood_cases/" + flood_case + "_flood_mask.tif"


def read_downsampled(path, factor=10):
    with rasterio.open(path) as ds:
        arr = ds.read(1, out_shape=(1, ds.height // factor, ds.width // factor), resampling=Resampling.nearest)
    return arr


obs = read_downsampled(path_obs, factor=10)
mask = read_downsampled(path_mask, factor=10)

# Plot
plt.figure(figsize=(14, 5))

plt.subplot(1, 2, 1)
plt.imshow(obs, cmap="Blues")
plt.title(flood_case + " — Flood Observations")
plt.colorbar(fraction=0.046, pad=0.04)

plt.subplot(1, 2, 2)
plt.imshow(mask, cmap="Reds")
plt.title(flood_case + " — Flood Mask")
plt.colorbar(fraction=0.046, pad=0.04)

plt.tight_layout()
plt.show()
