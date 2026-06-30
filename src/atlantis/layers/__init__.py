"""Atlantis layer framework: discover native layers and define derived ones.

This package makes *layers* a first-class, source-agnostic Atlantis concept.

* Declare what a source physically offers with :class:`NativeLayer`.
* Define what Atlantis computes (``flood_fraction``, ``quality_mask``, ...) with
  :class:`DerivedLayer` — each a declarative spec plus a pure ``derive`` function.
* Publish both through a per-source :class:`LayerRegistry`, the single source of
  truth used by processors, the CLI (``atlantis layers list``), and the docs.

Typical use in a source's ``layers.py``::

    from atlantis.layers import LayerRegistry, NativeLayer, register_source_registry

    registry = register_source_registry(LayerRegistry("modis"))
    registry.add_native(NativeLayer(name="Flood_2Day_250m", dtype="uint8", nodata=255,
                                    description="2-day flood composite", codes={3: "unusual flood"}))

    @registry.derived(name="flood_fraction", inputs=("flood_composite",), dtype="float32",
                      nodata=None, resampling="average", aggregation="nanmean",
                      description="Binary unusual-flood flag; fractional after averaging.")
    def flood_fraction(ctx):
        ...
"""

from __future__ import annotations

from atlantis.layers.registry import (
    LayerRegistry,
    all_registries,
    available_sources,
    find_layer,
    get_source_registry,
    is_known_layer,
    list_layers,
    load_source_registries,
    register_source_registry,
    resampling_for,
)
from atlantis.layers.spec import (
    AggregationMethod,
    DerivationContext,
    DerivedLayer,
    DeriveFn,
    Layer,
    LayerKind,
    NativeLayer,
    ResamplingMethod,
)

__all__ = [
    "AggregationMethod",
    "DeriveFn",
    "DerivationContext",
    "DerivedLayer",
    "Layer",
    "LayerKind",
    "LayerRegistry",
    "NativeLayer",
    "ResamplingMethod",
    "all_registries",
    "available_sources",
    "find_layer",
    "get_source_registry",
    "is_known_layer",
    "list_layers",
    "load_source_registries",
    "register_source_registry",
    "resampling_for",
]
