# Atlantis Setup Guide

This page covers one-time credential and account setup required to use the
various data backends. Run `uv run python scripts/setup.py` (or
`atlantis setup` via the CLI) to automate most of this — the steps below
explain what that script does and how to complete the parts that require a
browser.

## NASA Earthdata token (`EARTHDATA_TOKEN`)

Several backends (VIIRS, MODIS LANCE NRT, MODIS LAADS HDF4) authenticate
against NASA Earthdata using a bearer token.

1. Create a free account at <https://urs.earthdata.nasa.gov/> if you do not
   have one.
2. Generate a token under _Profile_ → _Generate Token_.
3. Export it in your shell (or add to `~/.bashrc`):

   ```bash
   export EARTHDATA_TOKEN=<your-token>
   ```

   `atlantis setup` will prompt you for this value and persist it for the
   current session.

## Pre-authorize the LAADS Web app (one-time, required for `laads_hdf4`)

A valid `EARTHDATA_TOKEN` alone is **not enough** to download HDF4 files
from `ladsweb.modaps.eosdis.nasa.gov`. The LAADS DAAC archive only serves
files to clients whose Earthdata user has explicitly authorized the LAADS
Web OAuth application (`client_id=A6th7HB-3EBoO7iOCiCLlA`). Without this
one-time approval, every HTTP `GET` against `/archive/.../*.hdf` redirects
to the URS login flow and an unattended script just sees an HTML login page.

Do it once per Earthdata account:

1. Visit the direct pre-authorize link while logged in to Earthdata:
   <https://urs.earthdata.nasa.gov/approve_app?client_id=A6th7HB-3EBoO7iOCiCLlA>
   and click **Authorize**.
2. Confirm the app shows up under
   <https://urs.earthdata.nasa.gov/profile> → _Applications_ → _Authorized Apps_
   as "LAADS Web".
3. Optionally accept any per-product EULA prompts by opening one HDF4 URL
   in the browser (e.g. the URL Atlantis prints when it errors).

Once pre-authorized, the bearer token alone is sufficient and
`make demo-modis` runs end-to-end. If you ever revoke the app or generate a
new token, the existing pre-authorization stays valid (it is bound to the
user, not the token).

## AWS profiles (GFM / ECMWF object store)

The GFM backend and ECMWF benchmarking notebooks read from S3-compatible
storage. `atlantis setup` writes two AWS profiles to `~/.aws/{config,credentials}`:

| Profile   | Purpose                                          |
| --------- | ------------------------------------------------ |
| `default` | ECMWF object store — requires access/secret keys |
| `noa`     | Anonymous AWS for public NOAA/NOA buckets        |

Existing profiles are never overwritten; missing sections are added in-place.
If you have ECMWF S3 credentials, have the access key and secret ready before
running `atlantis setup`.

## KuroSiwo catalogue

The KuroSiwo catalogue (`assets/ks_catalogue.gpkg`, ~500 MB) is not tracked
in the repository. Download it on demand:

```bash
uv run python scripts/download_kurosiwo.py
```

## Verifying the setup

```bash
# Runs all setup checks and reports missing assets/credentials
uv run python scripts/setup.py --check-only
```

For MODIS with HDF4 support, see [docs/gdal-install.md](gdal-install.md) for
instructions on building GDAL from source.
