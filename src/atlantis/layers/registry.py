"""Per-source layer registries and cross-source discovery.

Each source declares one :class:`LayerRegistry` holding its native and derived
layers. The registry is the single source of truth consulted by the processors
(to build outputs), the CLI (``atlantis layers list``), and the documentation
generator. Registries register themselves globally on import so discovery can
enumerate every source without importing heavy raster dependencies.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

from atlantis.layers.spec import (
    AggregationMethod,
    DerivedLayer,
    DeriveFn,
    Layer,
    NativeLayer,
    ResamplingMethod,
)


class LayerRegistry:
    """Ordered catalogue of the native and derived layers for one source.

    The registry preserves insertion order so generated docs and CLI listings
    are stable and human-meaningful (native bands first, then derivations).
    """

    def __init__(self, source_id: str) -> None:
        """Create an empty registry for *source_id* (e.g. ``"modis"``)."""
        self.source_id = source_id
        self._native: dict[str, NativeLayer] = {}
        self._derived: dict[str, DerivedLayer] = {}

    # ── Registration ─────────────────────────────────────────────────────

    def add_native(self, layer: NativeLayer) -> NativeLayer:
        """Register a :class:`NativeLayer`, returning it for convenience.

        Raises:
            ValueError: if a layer with the same name is already registered.
        """
        self._guard_unique(layer.name)
        self._native[layer.name] = layer
        return layer

    def add_derived(self, layer: DerivedLayer) -> DerivedLayer:
        """Register a pre-built :class:`DerivedLayer`, returning it.

        Raises:
            ValueError: if a layer with the same name is already registered.
        """
        self._guard_unique(layer.name)
        self._derived[layer.name] = layer
        return layer

    def derived(
        self,
        *,
        name: str,
        inputs: tuple[str, ...],
        dtype: str,
        nodata: int | float | None,
        description: str,
        resampling: ResamplingMethod,
        aggregation: AggregationMethod,
    ) -> Callable[[DeriveFn], DeriveFn]:
        """Decorator registering the wrapped function as a derived layer.

        Example:
            >>> @registry.derived(
            ...     name="flood_fraction", inputs=("flood_composite",),
            ...     dtype="float32", nodata=None,
            ...     resampling="average", aggregation="nanmean",
            ...     description="Binary unusual-flood flag; fractional after averaging.",
            ... )
            ... def flood_fraction(ctx):
            ...     return (ctx["flood_composite"] == 3).astype("float32")

        Returns:
            A decorator that registers and returns the original function so it
            stays directly callable for unit tests.
        """

        def decorator(fn: DeriveFn) -> DeriveFn:
            self.add_derived(
                DerivedLayer(
                    name=name,
                    inputs=inputs,
                    derive=fn,
                    dtype=dtype,
                    nodata=nodata,
                    description=description,
                    resampling=resampling,
                    aggregation=aggregation,
                )
            )
            return fn

        return decorator

    def _guard_unique(self, name: str) -> None:
        if name in self._native or name in self._derived:
            raise ValueError(f"Layer '{name}' already registered for source '{self.source_id}'")

    # ── Lookup ───────────────────────────────────────────────────────────

    def get(self, name: str) -> Layer:
        """Return the layer named *name* (native or derived).

        Raises:
            KeyError: if no layer with that name is registered.
        """
        if name in self._native:
            return self._native[name]
        if name in self._derived:
            return self._derived[name]
        raise KeyError(f"Source '{self.source_id}' has no layer '{name}'. Available: {self.names()}")

    def get_native(self, name: str) -> NativeLayer:
        """Return the native layer named *name*.

        Raises:
            KeyError: if no native layer with that name is registered.
        """
        return self._native[name]

    def get_derived(self, name: str) -> DerivedLayer:
        """Return the derived layer named *name*.

        Raises:
            KeyError: if no derived layer with that name is registered.
        """
        return self._derived[name]

    def __contains__(self, name: object) -> bool:
        """Return ``True`` when *name* is a registered native or derived layer."""
        return name in self._native or name in self._derived

    # ── Listing ──────────────────────────────────────────────────────────

    def list_native(self) -> list[NativeLayer]:
        """Return the native layers in registration order."""
        return list(self._native.values())

    def list_derived(self) -> list[DerivedLayer]:
        """Return the derived layers in registration order."""
        return list(self._derived.values())

    def list_all(self) -> list[Layer]:
        """Return native layers followed by derived layers."""
        return [*self._native.values(), *self._derived.values()]

    def names(self) -> list[str]:
        """Return all registered layer names (native then derived)."""
        return [*self._native, *self._derived]

    def __iter__(self) -> Iterator[Layer]:
        """Iterate over all layers, native first."""
        return iter(self.list_all())


# ── Cross-source discovery ───────────────────────────────────────────────

#: Source id -> registry, populated as each source's ``layers`` module imports.
_SOURCE_REGISTRIES: dict[str, LayerRegistry] = {}


def register_source_registry(registry: LayerRegistry) -> LayerRegistry:
    """Register *registry* globally so discovery can find it.

    Raises:
        ValueError: if a registry for the same source id already exists.
    """
    if registry.source_id in _SOURCE_REGISTRIES:
        raise ValueError(f"Layer registry for source '{registry.source_id}' already registered")
    _SOURCE_REGISTRIES[registry.source_id] = registry
    return registry


def get_source_registry(source_id: str) -> LayerRegistry:
    """Return the registry for *source_id*, importing source modules if needed.

    Raises:
        KeyError: if the source has no registered layer registry.
    """
    if source_id not in _SOURCE_REGISTRIES:
        load_source_registries()
    if source_id not in _SOURCE_REGISTRIES:
        raise KeyError(f"No layer registry for source '{source_id}'. Available: {available_sources()}")
    return _SOURCE_REGISTRIES[source_id]


def available_sources() -> list[str]:
    """Return the ids of sources that have registered a layer registry."""
    load_source_registries()
    return sorted(_SOURCE_REGISTRIES)


def all_registries() -> dict[str, LayerRegistry]:
    """Return a copy of the source-id -> registry mapping after loading all."""
    load_source_registries()
    return dict(_SOURCE_REGISTRIES)


def list_layers(source_id: str) -> list[Layer]:
    """Return all layers (native first, then derived) for *source_id*.

    Convenience programmatic-discovery entry point mirroring the CLI
    ``atlantis list-layers`` command.

    Raises:
        KeyError: if the source has no registered layer registry.
    """
    return get_source_registry(source_id).list_all()


def find_layer(name: str, source_id: str | None = None) -> Layer | None:
    """Return the layer named *name*, preferring *source_id* when given.

    Searches the given source's registry first (if provided), then every other
    source. Returns ``None`` when no source declares a layer with that name.
    """
    if source_id is not None and source_id in _SOURCE_REGISTRIES:
        registry = _SOURCE_REGISTRIES[source_id]
        if name in registry:
            return registry.get(name)
    for registry in all_registries().values():
        if name in registry:
            return registry.get(name)
    return None


def resampling_for(name: str, source_id: str | None = None) -> str | None:
    """Return the declared resampling method for a layer, or ``None`` if unknown."""
    layer = find_layer(name, source_id)
    return layer.resampling if layer is not None else None


def is_known_layer(name: str, source_id: str | None = None) -> bool:
    """Return ``True`` when any source declares a layer named *name*."""
    return find_layer(name, source_id) is not None


def load_source_registries() -> None:
    """Import every source's ``layers`` module so its registry self-registers.

    Imports are best-effort and lightweight: layer modules depend only on
    NumPy and this package, not on raster I/O, so discovery stays fast.
    """
    import importlib

    for module in (
        "atlantis.fetchers.modis.layers",
        "atlantis.fetchers.viirs.layers",
        "atlantis.fetchers.gfm.layers",
    ):
        try:
            importlib.import_module(module)
        except ImportError:
            continue
