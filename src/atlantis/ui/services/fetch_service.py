"""Async adapter for running fetch/search/harmonise/plot in background threads."""

from __future__ import annotations

import asyncio
import queue
from datetime import date
from pathlib import Path
from typing import Callable

import requests
from loguru import logger as loguru_logger
from rasterio.enums import Resampling

from atlantis.config import get_config
from atlantis.fetchers.registry import get_fetcher
from atlantis.models.event import FloodEvent
from atlantis.ui.models import FetchProgress, FetchRequest, FetchResponse
from atlantis.utils.plot import (
    GFM_ENSEMBLE_FLOOD_EXTENT_CODES,
    MODIS_RAW_CODES,
    VIIRS_RAW_CODES,
    plot_classified,
    plot_raw,
)


def _parse_bbox(value: str) -> tuple[float, float, float, float]:
    """Parse a bbox from a four-number string."""
    parts = value.replace(",", " ").split()
    if len(parts) != 4:
        raise ValueError("BBox must contain exactly four numbers: west south east north")
    return tuple(float(p) for p in parts)  # type: ignore[return-value]


def _ds_is_classified(ds, source_id: str) -> bool:
    """Return True if the dataset contains a classified flood_fraction layer."""
    return "flood_fraction" in ds


def _date_label(result) -> str:
    """Extract a date label from a fetch result."""
    timestamp = getattr(result, "timestamp", None)
    if timestamp is not None:
        return timestamp.strftime("%Y%m%d")
    token = getattr(result, "date_token", None)
    if token:
        return token
    return "unknown"


async def run_fetch(
    request: FetchRequest,
    progress_callback: Callable[[FetchProgress], None],
    log_callback: Callable[[str, str], None] | None = None,
) -> FetchResponse:
    """Run the full fetch pipeline asynchronously.

    All blocking I/O (search, fetch, harmonise, plot) runs inside
    ``asyncio.to_thread`` so the NiceGUI event loop stays responsive.
    Progress is reported via *progress_callback*. Live fetcher log messages
    are streamed via *log_callback* (``(level, text)``) when provided.

    Args:
        request: The validated fetch request from the web form.
        progress_callback: Called with updated FetchProgress after each stage.
        log_callback: Optional callback receiving ``(level, message_text)``
            from fetcher-internal loguru messages captured during the fetch.

    Returns:
        FetchResponse with paths to generated files and any diagnostics.
    """
    config = get_config()
    output_dir = config.fetcher.cache_dir / "raw" / request.event_id
    output_dir.mkdir(parents=True, exist_ok=True)

    progress = FetchProgress()

    def update(stage: str, **kwargs: object) -> None:
        progress.stage = stage  # type: ignore[assignment]
        for k, v in kwargs.items():
            setattr(progress, k, v)
        progress_callback(progress)

    # ── Validate and build FloodEvent ──────────────────────────────────────
    try:
        bbox_tuple = _parse_bbox(request.bbox)
        flood_event = FloodEvent(
            event_id=request.event_id,
            bbox=bbox_tuple,
            start_date=date.fromisoformat(request.start_date),
            end_date=date.fromisoformat(request.end_date),
            sources=[request.source],
        )
    except ValueError as exc:
        return FetchResponse(
            event_id=request.event_id,
            source_id=request.source,
            output_dir=output_dir,
            error=str(exc),
        )

    # ── Get fetcher class and instantiate ─────────────────────────────────
    try:
        fetcher_cls = get_fetcher(request.source)
    except KeyError:
        return FetchResponse(
            event_id=request.event_id,
            source_id=request.source,
            output_dir=output_dir,
            error=f"Unknown source '{request.source}'",
        )

    fetcher_kwargs: dict = _build_fetcher_kwargs(request)
    fetcher = await asyncio.to_thread(lambda: fetcher_cls(**fetcher_kwargs))

    # ── Fetch (with live log capture) ─────────────────────────────────────
    _log_queue: queue.Queue = queue.Queue()

    def _log_sink(message) -> None:
        record = message.record
        _log_queue.put((record["level"].name.lower(), record["message"]))

    _handler_id: int | None = None
    _drain_task: asyncio.Task | None = None

    async def _drain_log_messages() -> None:
        loop = asyncio.get_event_loop()
        while True:
            try:
                level, text = await loop.run_in_executor(None, _log_queue.get, True, 0.3)
                if log_callback:
                    log_callback(level, text)
            except queue.Empty:
                pass

    if log_callback:
        _handler_id = loguru_logger.add(
            _log_sink,
            level="DEBUG",
            filter="atlantis",
            format="{message}",
        )
        _drain_task = asyncio.create_task(_drain_log_messages())

    update(
        "searching",
        message=f"Searching {request.source.upper()} for {request.event_id} "
        f"({request.start_date} to {request.end_date})...",
    )
    try:
        fetch_results = await asyncio.to_thread(fetcher.fetch, flood_event, output_dir / request.source)
    except requests.RequestException as exc:
        update("error", error=f"Network error: {exc}")
        return FetchResponse(
            event_id=request.event_id,
            source_id=request.source,
            output_dir=output_dir,
            error=f"Network error: {exc}",
        )
    finally:
        if _drain_task is not None:
            _drain_task.cancel()
            try:
                await _drain_task
            except asyncio.CancelledError:
                pass
        if _handler_id is not None:
            loguru_logger.remove(_handler_id)

    if not fetch_results:
        diagnostics = getattr(fetcher, "last_diagnostics", None)
        update("done", message="No results found", diagnostics=diagnostics)
        return FetchResponse(
            event_id=request.event_id,
            source_id=request.source,
            output_dir=output_dir,
            diagnostics=diagnostics,
        )

    all_files = [path for result in fetch_results for path in result.files]
    written_count = len(all_files)
    update(
        "fetched",
        files=written_count,
        message=f"Fetched {written_count} file(s) into {output_dir / request.source}",
    )

    # ── Harmonise ────────────────────────────────────────────────────────
    harmonised_path: Path | None = None
    if request.harmonise:
        update("harmonising", message="Harmonising to 1 arcmin EPSG:4326 grid...")
        best_result, best_label = await asyncio.to_thread(_select_best_result, fetcher, fetch_results)
        if best_result is not None:
            ds = await asyncio.to_thread(fetcher.to_dataset, best_result)
            if _ds_is_classified(ds, request.source):
                flood_var = "flood_fraction"
            else:
                flood_var = "ensemble_flood_extent" if request.source == "gfm" else "raw"
            harm_dir = output_dir / request.source / "harmonised"
            harm_dir.mkdir(parents=True, exist_ok=True)
            harm_out = harm_dir / f"{request.event_id}_{best_label}_{request.source}_harm.tif"
            try:
                from atlantis.harmoniser import Harmoniser

                harmoniser = Harmoniser()
                harmonised_path = await asyncio.to_thread(
                    harmoniser.harmonise_file,
                    _first_geotiff(all_files, best_result),
                    harm_out,
                    request.source,
                    flood_var,
                )
            except Exception:
                try:
                    ds_source = await asyncio.to_thread(fetcher.to_dataset, best_result)
                    harmoniser = Harmoniser()
                    ds_squeezed = ds_source.get(flood_var) or ds_source.get("raw")
                    if ds_squeezed is not None and hasattr(ds_squeezed, "rio"):
                        harm_out_str = str(harm_out)
                        await asyncio.to_thread(lambda: ds_source.rio.to_raster(harm_out_str))
                except Exception:
                    pass
        update("harmonised", message="Harmonisation complete")

    # ── Plot ─────────────────────────────────────────────────────────────
    plot_path: Path | None = None
    if request.plot:
        update("plotting", message="Generating plot...")
        best_result, best_label = await asyncio.to_thread(_select_best_result, fetcher, fetch_results)
        if best_result is not None:
            ds = await asyncio.to_thread(fetcher.to_dataset, best_result)
            plot_dir = output_dir / request.source / "plots"
            plot_dir.mkdir(parents=True, exist_ok=True)
            png_path = plot_dir / f"{request.event_id}_{best_label}_{request.source}.png"
            try:
                await asyncio.to_thread(
                    _plot_source,
                    ds,
                    request.event_id,
                    best_label,
                    source_id=request.source,
                    output_png_path=png_path,
                )
                plot_path = png_path
            except Exception:
                pass
        update("plotted", message=f"Plot saved to {png_path}")

    final_dir = output_dir / request.source
    update(
        "done",
        message=f"Done — {len(all_files)} file(s) in {final_dir}",
        files=len(all_files),
    )
    return FetchResponse(
        event_id=request.event_id,
        source_id=request.source,
        output_dir=output_dir / request.source,
        files=all_files,
        harmonised_path=harmonised_path,
        plot_path=plot_path,
        diagnostics=getattr(fetcher, "last_diagnostics", None),
    )


def _build_fetcher_kwargs(request: FetchRequest) -> dict:
    """Build source-specific fetcher constructor kwargs from a FetchRequest."""
    kwargs: dict = {
        "classify": request.classify,
        "strategy": request.strategy,
        "keep_processed": True,
    }
    if request.source == "viirs":
        kwargs.update(
            backend=request.viirs_backend,
            data_format="tif",
            stream=request.stream,
        )
    elif request.source == "modis":
        effective_stream = request.stream and request.modis_backend == "lance_geotiff"
        kwargs.update(
            backend=request.modis_backend,
            composite=request.modis_composite,
            stream=effective_stream,
        )
    elif request.source == "gfm":
        gfm_resampling = (
            Resampling[request.gfm_resampling]
            if request.gfm_resampling in Resampling.__members__
            else Resampling.average
        )
        kwargs.update(
            coarsen_factor=request.gfm_coarsen_factor,
            resampling=gfm_resampling,
        )
    return kwargs


def _select_best_result(fetcher, fetch_results):
    """Select the fetch result with the highest flood pixel count."""
    if len(fetch_results) == 1:
        return fetch_results[0], _date_label(fetch_results[0])

    best_result = None
    best_date_label = ""
    best_flood_count = 0

    from atlantis.utils.plot import pixel_stats_raw

    for result in fetch_results:
        ds = fetcher.to_dataset(result)
        date_label = _date_label(result)
        if _ds_is_classified(ds, fetcher.source_id):
            flooded = int((ds["flood_fraction"].values > 0).sum())
            if flooded > best_flood_count:
                best_flood_count = flooded
                best_result = result
                best_date_label = date_label
        else:
            pixel_stats_raw(ds["raw"].values if "raw" in ds else list(ds.data_vars.values())[0].values, name=date_label)

    if best_result is None:
        best_result = fetch_results[0]
        best_date_label = _date_label(fetch_results[0])

    return best_result, best_date_label


def _first_geotiff(files: list[Path], result) -> Path | None:
    """Return the first GeoTIFF from a list of fetched files."""
    for f in files:
        if f.suffix in (".tif", ".tiff"):
            return f
    if files:
        return files[0]
    return None


def _plot_source(
    ds,
    event_id: str,
    date_label: str,
    *,
    source_id: str,
    output_png_path: Path,
) -> None:
    """Save a PNG visualisation of the peak-flood date."""
    res = {"viirs": "375 m", "modis": "250 m", "gfm": "20 m"}.get(source_id, "")
    pretty = {"viirs": "VIIRS", "modis": "MODIS", "gfm": "GFM"}.get(source_id, source_id)

    if _ds_is_classified(ds, source_id):
        plot_classified(
            ds["flood_fraction"],
            title=f"{event_id}: {pretty} flood fraction {date_label} ({res})",
            output_path=output_png_path,
            announce=False,
        )
    elif "ensemble_flood_extent" in ds:
        plot_raw(
            ds["ensemble_flood_extent"],
            title=f"{event_id}: {pretty} ensemble_flood_extent {date_label} ({res})",
            output_path=output_png_path,
            codes=GFM_ENSEMBLE_FLOOD_EXTENT_CODES,
            legend_title="GFM ensemble_flood_extent codes",
            announce=False,
        )
    else:
        codes = MODIS_RAW_CODES if source_id == "modis" else VIIRS_RAW_CODES
        legend_title = "MODIS MCDWD codes" if source_id == "modis" else "VIIRS pixel codes"
        raw = ds["raw"]
        if source_id == "viirs":
            import numpy as np

            raw = raw.where((np.isnan(raw)) | (raw < 101) | (raw > 200), 100)
        plot_raw(
            raw,
            title=f"{event_id}: {pretty} raw {date_label} ({res})",
            output_path=output_png_path,
            codes=codes,
            legend_title=legend_title,
            announce=False,
        )
