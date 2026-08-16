# Cheatsheet of common pipeline commands

If you are using pixi with the gdal binary baked there, you can replace `uv run` with `pixi run`

## Plot native layers for a dataset

uv run atlantis --verbose fetch \
--event Valencia_2024 --source viirs \
--bbox "-1.5 38.8 0.5 40.0" \
--start-date 2024-10-29 --end-date 2024-11-04 \
--strategy all --peak-window-days 2 --max-observations 3 --peak-priority balanced \
--plot --harmonise --no-keep-processed --no-classify \
--output ./data/Valencia_2024

## Run the batch zarr pipeline for

```bash
atlantis batch viirs cube run  --inventory s3://atlantis/assets/viirs/viirs_archive_catalog_2018.parquet  -a s3://atlantis/zarr/2018/ --db-path archive_tracker_viirs_2018.db --dashboard-port 8001
```

## Visualize in localhost a timerange per dataset

E.g. for Valencia event:

```bash
uv run viz atlantis viz serve gfm -a s3://atlantis/zarr/2024/ --bbox "-1.5 38.8 0.5 40.0" --start 2024-10-31 --end 2024-11-04 --var water_fraction
```

## Yearly size of VIIRS and MODIS Atlantis archive on S3

```bash
aws s3 ls --recursive --summarize --human-readable s3://atlantis/zarr/2024/datacube.zarr/

Total Objects: 171272
   Total Size: 11.5 GiB
```
