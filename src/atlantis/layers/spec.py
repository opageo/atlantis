"""Declarative specifications for Atlantis data layers.

A *layer* is a single named 2D raster variable that Atlantis can expose for a
source (MODIS, VIIRS, GFM, ...). Layers come in two kinds:

* :class:`NativeLayer` — a layer that physically exists in the upstream
  product (an HDF subdataset, a STAC asset, or a code band). Native layers are
  *fetched* untouched; they describe what the source offers.
* :class:`DerivedLayer` — a layer that Atlantis *computes* from one or more
  native layers (for example ``flood_fraction`` is derived, not downloaded).
  Each derived layer carries a pure :func:`derive` function plus the metadata
  needed to write, resample, and aggregate it.

The split keeps derivation logic declarative and discoverable: every source
publishes its layers through a :class:`~atlantis.layers.registry.LayerRegistry`,
which is the single source of truth for code, the CLI, and the docs.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

#: Resampling methods understood by the harmoniser's reprojector. ``mode`` and
#: ``nearest`` preserve discrete codes; ``average`` produces sub-pixel fractions.
ResamplingMethod = str  # one of: "average", "bilinear", "nearest", "cubic", "mode"

#: Aggregation methods used when collapsing several observations (dates) of the
#: same layer into one. ``nanmean`` averages fractions ignoring NaNs; ``mode``
#: takes the per-pixel majority code; ``any`` ORs boolean masks; ``max`` keeps
#: the largest valid code.
AggregationMethod = str  # one of: "nanmean", "mean", "mode", "any", "max"


class LayerKind(str, Enum):
    """Whether a layer is fetched from the source or computed by Atlantis."""

    NATIVE = "native"
    DERIVED = "derived"


@dataclass(frozen=True)
class NativeLayer:
    """A raster layer that exists in the upstream product.

    Native layers describe what a source physically offers so it can be
    discovered and documented. They are passed through untouched (no Atlantis
    classification) when written as raw output.

    Attributes:
        name: Stable layer identifier (also the xarray variable / file token).
        dtype: NumPy dtype string for the on-disk representation (e.g. ``"uint8"``).
        nodata: Sentinel value marking missing pixels, or ``None`` when unset.
        description: Human-readable explanation of the layer's meaning.
        codes: Optional mapping of pixel code -> meaning for categorical bands.
        resampling: Default resampling method for the harmoniser.
        aggregation: Default cross-observation aggregation method.
    """

    name: str
    dtype: str
    nodata: int | float | None
    description: str
    codes: Mapping[int, str] | None = None
    resampling: ResamplingMethod = "nearest"
    aggregation: AggregationMethod = "mode"

    #: Discriminator so callers can branch on layer kind without isinstance.
    kind: LayerKind = field(default=LayerKind.NATIVE, init=False)


@dataclass(frozen=True)
class DerivationContext:
    """Inputs available to a :class:`DerivedLayer`'s ``derive`` function.

    Wraps the set of native arrays a source has loaded for the current tile /
    date group, keyed by native layer name. Keeping derivation behind this thin
    accessor lets each source decide *which* stage (raw codes, accumulated
    fraction counts, ...) it exposes without changing the derive signature.

    Attributes:
        arrays: Mapping of native layer name -> 2D NumPy array.
    """

    arrays: Mapping[str, "np.ndarray"]

    def __getitem__(self, name: str) -> "np.ndarray":
        """Return the native array registered under *name*.

        Raises:
            KeyError: with the available names when *name* is missing, so a
                misconfigured ``inputs`` declaration fails loudly.
        """
        try:
            return self.arrays[name]
        except KeyError as exc:
            available = sorted(self.arrays)
            raise KeyError(f"Native layer '{name}' not available for derivation; loaded layers: {available}") from exc

    def get(self, name: str, default: "np.ndarray | None" = None) -> "np.ndarray | None":
        """Return the native array for *name* or *default* when absent."""
        return self.arrays.get(name, default)

    def __contains__(self, name: object) -> bool:
        """Return ``True`` when a native array is loaded under *name*."""
        return name in self.arrays


#: Signature of a derived-layer derivation: pure function of a context.
DeriveFn = Callable[[DerivationContext], "np.ndarray"]


@dataclass(frozen=True)
class DerivedLayer:
    """A raster layer that Atlantis computes from native layers.

    The derivation is a *pure* function of a :class:`DerivationContext`, which
    makes each layer trivially unit-testable in isolation and keeps the logic
    in one declarative place per source.

    Attributes:
        name: Stable layer identifier (also the xarray variable / file token).
        inputs: Native layer names this derivation consumes (must be present in
            the :class:`DerivationContext` at derive time).
        derive: Pure function mapping a context to the output 2D array.
        dtype: NumPy dtype string for the in-memory derived array.
        nodata: Sentinel value marking missing pixels, or ``None`` when unset.
        description: Human-readable explanation of how the layer is built.
        resampling: Resampling method the harmoniser should use for this layer.
        aggregation: Cross-observation aggregation method for this layer.
    """

    name: str
    inputs: tuple[str, ...]
    derive: DeriveFn
    dtype: str
    nodata: int | float | None
    description: str
    resampling: ResamplingMethod
    aggregation: AggregationMethod

    #: Discriminator so callers can branch on layer kind without isinstance.
    kind: LayerKind = field(default=LayerKind.DERIVED, init=False)


#: Either layer kind, for APIs that treat the catalogue uniformly.
Layer = NativeLayer | DerivedLayer
