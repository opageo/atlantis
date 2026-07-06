"""Reusable NiceGUI form controls for the fetch page."""

from __future__ import annotations

from typing import Callable

from nicegui import ui

from atlantis.fetchers.registry import list_fetchers

# Event pre-sets from CLI examples (pixi.toml).
EVENT_PRESETS: list[dict] = [
    {
        "name": "Valencia 2024",
        "event_id": "Valencia_2024",
        "bbox": (-1.5, 38.8, 0.5, 40.0),
        "date_from": "2024-10-29",
        "date_to": "2024-11-04",
    },
    {
        "name": "Harvey 2017",
        "event_id": "Harvey_2017",
        "bbox": (-97.27, 28.24, -95.54, 29.80),
        "date_from": "2017-08-28",
        "date_to": "2017-08-31",
    },
    {
        "name": "Bihar 2019",
        "event_id": "Bihar_2019",
        "bbox": (84.84, 24.92, 86.49, 26.16),
        "date_from": "2019-09-16",
        "date_to": "2019-09-20",
    },
    {
        "name": "Vamco 2020",
        "event_id": "Vamco_2020",
        "bbox": (121.14, 16.72, 122.25, 18.45),
        "date_from": "2020-11-12",
        "date_to": "2020-11-14",
    },
    {
        "name": "West Africa 2020",
        "event_id": "WestAfrica_2020",
        "bbox": (-0.86, 8.26, 1.99, 11.73),
        "date_from": "2020-10-13",
        "date_to": "2020-10-15",
    },
]


def source_selector(on_change: Callable[[str], None] | None = None) -> ui.select:
    """Dropdown for selecting a data source.

    Args:
        on_change: Called with the new source id when selection changes.
    """
    sources = list_fetchers()
    return ui.select(
        options=sources,
        label="Data Source",
        value=sources[0] if sources else None,
        on_change=lambda e: on_change(e.value) if on_change else None,
    ).props("outlined dense").classes("w-full")


def bbox_input() -> tuple[ui.number, ui.number, ui.number, ui.number]:
    """Four number inputs for bounding box (west, south, east, north).

    Returns:
        Tuple of (west, south, east, north) ui.number controls.
    """
    with ui.element("div").classes("w-full"):
        with ui.grid(columns=4).classes("w-full gap-1"):
            west = (
                ui.number(label="W", value=-1.5, min=-180, max=180, format="%.3f")
                .props("outlined dense")
                .classes("w-full")
            )
            east = (
                ui.number(label="E", value=0.5, min=-180, max=180, format="%.3f")
                .props("outlined dense")
                .classes("w-full")
            )
            south = (
                ui.number(label="S", value=38.8, min=-90, max=90, format="%.3f")
                .props("outlined dense")
                .classes("w-full")
            )
            north = (
                ui.number(label="N", value=40.0, min=-90, max=90, format="%.3f")
                .props("outlined dense")
                .classes("w-full")
            )
    return west, south, east, north


def date_range_picker(
    default_from: str | None = None,
    default_to: str | None = None,
) -> tuple[ui.date, ui.label]:
    """Date range picker using Quasar's range mode.

    Args:
        default_from: Default start date (YYYY-MM-DD). Defaults to today-30.
        default_to: Default end date (YYYY-MM-DD). Defaults to today.

    Returns:
        Tuple of (date_picker, date_display_label).
    """
    from datetime import date, timedelta

    if default_to is None:
        default_to = str(date.today())
    if default_from is None:
        default_from = str(date.today() - timedelta(days=30))

    date_picker = ui.date(value={"from": default_from, "to": default_to}).props("range")
    date_picker.classes("w-full")
    date_display = ui.label("").classes("text-sm text-blue-600 font-medium")

    return date_picker, date_display


def option_toggle(
    label: str,
    tooltip: str = "",
    value: bool = True,
    on_change: Callable[[bool], None] | None = None,
) -> ui.switch:
    """Toggle switch with label and optional tooltip."""
    switch = ui.switch(
        label,
        value=value,
        on_change=lambda e: on_change(e.value) if on_change else None,
    )
    if tooltip:
        switch.tooltip(tooltip)
    return switch


def strategy_selector(value: str = "peak") -> ui.select:
    """Strategy dropdown (peak, aggregate, all)."""
    return ui.select(
        options=["peak", "aggregate", "all"],
        label="Strategy",
        value=value,
    ).props("outlined dense").classes("w-full")


def viirs_options(default_backend: str = "noaa_s3") -> ui.select:
    """VIIRS backend dropdown."""
    return ui.select(
        options=["noaa_s3", "gmu_legacy"],
        label="VIIRS Backend",
        value=default_backend,
    ).props("outlined dense").classes("w-full")


def modis_options(
    default_backend: str = "lance_geotiff", default_composite: str = "F2"
) -> tuple[ui.select, ui.select]:
    """MODIS backend + composite dropdowns."""
    backend = ui.select(
        options=["lance_geotiff", "laads_hdf4"],
        label="MODIS Backend",
        value=default_backend,
    ).props("outlined dense").classes("w-full")

    composite = ui.select(
        options=["F1", "F1C", "F2", "F3"],
        label="MODIS Composite",
        value=default_composite,
    ).props("outlined dense").classes("w-full")

    return backend, composite


def gfm_options(default_coarsen: int = 4) -> tuple[ui.number, ui.select]:
    """GFM coarsen factor + resampling controls."""
    coarsen = (
        ui.number(label="Coarsen Factor", value=default_coarsen, min=1, max=16)
        .props("outlined dense")
        .classes("w-full")
    )

    resampling = ui.select(
        options=["average", "bilinear", "nearest", "cubic"],
        label="Resampling",
        value="average",
    ).props("outlined dense").classes("w-full")

    return coarsen, resampling
