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
  hdf-devel
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
-- HDF4: Found
```

If HDF4 is not found, verify `hdf-devel` is installed and visible via
`pkg-config --libs hdf`.

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
uv pip install --no-binary gdal GDAL==3.13.0
```

## 7. Verify

```bash
gdal-config --version
# Should print: 3.13.0

python -c "from osgeo import gdal; d = gdal.GetDriverByName('HDF4'); print('HDF4 driver:', 'OK' if d else 'MISSING')"
# Should print: HDF4 driver: OK
```

## References

- [Building GDAL from source](https://gdal.org/en/stable/development/building_from_source.html)
- [GDAL HDF4 driver](https://gdal.org/en/stable/drivers/raster/hdf4.html)
