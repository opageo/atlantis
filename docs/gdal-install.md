# Installing GDAL 3.13 with HDF4 support (Rocky 9 / RHEL 9)

The MODIS historical backend (`laads_hdf4`) requires GDAL compiled with HDF4
driver support. This guide covers building GDAL 3.13 from source on Rocky
Linux 9 (or any RHEL 9 derivative).

## 1. System dependencies

```bash
sudo dnf update -y
sudo dnf install -y dnf-plugins-core
sudo dnf config-manager --set-enabled crb
sudo dnf groupinstall -y "Development Tools"
sudo dnf install -y \
  gcc gcc-c++ make cmake ninja-build \
  git wget tar pkgconf-pkg-config \
  glibc-devel libstdc++-devel \
  hdf-devel proj-devel
```

## 2. Clone GDAL and checkout release

```bash
cd ~
git clone https://github.com/OSGeo/gdal.git
cd gdal
git checkout v3.13.0
```

## 3. Configure (enable HDF4)

```bash
mkdir build && cd build
cmake .. \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX=/usr/local \
  -DGDAL_USE_HDF4=ON \
  -GNinja
```

After running cmake, check the output for:

```
-- Found HDF4: /usr/lib64/libdf.so (found version "4.2.15")
```

If HDF4 is not found, verify `hdf-devel` is installed and visible via
`pkg-config --libs hdf`.

Note: PROJ (`proj-devel`) must also be installed or cmake will error out
before reaching the HDF4 check.

## 4. Build and install

```bash
ninja
sudo ninja install
```

## 5. Library path

The shared library (`libgdal.so*`) is typically installed under
`/usr/local/lib64`. Make sure the linker can find it:

```bash
export LD_LIBRARY_PATH=/usr/local/lib64:$LD_LIBRARY_PATH
echo 'export LD_LIBRARY_PATH=/usr/local/lib64:$LD_LIBRARY_PATH' >> ~/.bashrc
```

## 6. Python bindings

Reinstall the Python GDAL package in your project venv so it links against
the newly built `libgdal`:

```bash
cd ~/repos/atlantis
uv pip install --no-binary gdal "GDAL==$(gdal-config --version)"
```

## 7. Verify

```bash
gdal-config --version
# Should print: 3.13.0

python -c "from osgeo import gdal; d = gdal.GetDriverByName('HDF4'); print('HDF4 driver:', 'OK' if d else 'MISSING')"
# Should print: HDF4 driver: OK
```

## 8. Test with the MODIS NRT backend

The simplest end-to-end check that requires no credentials uses the
`lance_geotiff` backend (LANCE NRT GeoTIFFs). If you have an
`EARTHDATA_TOKEN` you can also exercise `laads_hdf4` directly.

```bash
cd ~/repos/atlantis

# Smoke-test: verify LaadsHdf4Backend instantiates without error
# (confirms GDAL sees the HDF4 driver at runtime)
python -c "
from atlantis.fetchers.modis.backend import get_backend
b = get_backend('laads_hdf4')
print('laads_hdf4 backend OK:', b)
"

# Full NRT fetch (requires EARTHDATA_TOKEN, uses lance_geotiff — no HDF4)
uv run atlantis --verbose fetch \
  --event MODIS_recent --source modis \
  --bbox "-98 25 -93 31" \
  --start-date $(date -u -d '5 days ago' +%Y-%m-%d) \
  --end-date $(date -u +%Y-%m-%d) \
  --modis-backend lance_geotiff --modis-composite F2 \
  --strategy peak --classify \
  --output ./data/MODIS_recent
```

## 9. Pre-authorize the LAADS Web app (one-time, required for `laads_hdf4`)

A valid `EARTHDATA_TOKEN` alone is **not enough** to download HDF4 files
from `ladsweb.modaps.eosdis.nasa.gov`. The LAADS DAAC archive only serves
files to clients whose Earthdata user has explicitly authorized the LAADS
Web OAuth application (`client_id=A6th7HB-3EBoO7iOCiCLlA`). Without this
one-time approval, every HTTP `GET` against `/archive/.../*.hdf` 303-redirects
to `/profiles/licenses/...` → `/oauth/login` → URS authorize, and an
unattended script just sees an HTML login page.

Do it once per Earthdata account:

1. Visit the direct pre-authorize link while logged in to Earthdata:
   <https://urs.earthdata.nasa.gov/approve_app?client_id=A6th7HB-3EBoO7iOCiCLlA>
   and click **Authorize**.
2. Confirm the app shows up under
   <https://urs.earthdata.nasa.gov/profile> → _Applications_ → _Authorized Apps_
   as "LAADS Web".
3. Optionally accept any per-product EULA prompts by opening one HDF4 URL
   in the browser (e.g. the URL Atlantis tells you about when it errors).

Once pre-authorized, the bearer token alone is sufficient and
`make demo-modis` runs end-to-end. If you ever revoke the app or generate a
new token, the existing pre-authorization stays valid (it is bound to the
user, not the token).

## References

- [Building GDAL from source](https://gdal.org/en/stable/development/building_from_source.html)
- [GDAL HDF4 driver](https://gdal.org/en/stable/drivers/raster/hdf4.html)
- [NASA Earthdata: How to pre-authorize an application](https://urs.earthdata.nasa.gov/documentation/for_users/how_to_preauth_app)
