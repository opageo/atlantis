"""Render the layer registries to Markdown for human-readable documentation.

The registries are the single source of truth: docs are generated from them so
the published per-source layer tables never drift from the code. Used by both
``scripts/generate_layer_docs.py`` and tests.
"""

from __future__ import annotations

from atlantis.layers.registry import LayerRegistry, all_registries
from atlantis.layers.spec import DerivedLayer, NativeLayer


def _format_codes(layer: NativeLayer) -> str:
    """Render a native layer's code table as a compact inline string."""
    if not layer.codes:
        return ""
    return "; ".join(f"`{code}` = {meaning}" for code, meaning in layer.codes.items())


def _native_table(layers: list[NativeLayer]) -> list[str]:
    """Render the native-layer Markdown table rows."""
    lines = [
        "| Layer | dtype | nodata | Resampling | Aggregation | Codes | Description |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for layer in layers:
        lines.append(
            f"| `{layer.name}` | {layer.dtype} | {layer.nodata} | {layer.resampling} | "
            f"{layer.aggregation} | {_format_codes(layer)} | {layer.description} |"
        )
    return lines


def _derived_table(layers: list[DerivedLayer]) -> list[str]:
    """Render the derived-layer Markdown table rows."""
    lines = [
        "| Layer | dtype | nodata | Inputs | Resampling | Aggregation | Description |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for layer in layers:
        inputs = ", ".join(f"`{name}`" for name in layer.inputs)
        lines.append(
            f"| `{layer.name}` | {layer.dtype} | {layer.nodata} | {inputs} | {layer.resampling} | "
            f"{layer.aggregation} | {layer.description} |"
        )
    return lines


def _anchor(anchor_id: str) -> str:
    """Return an explicit HTML anchor for stable cross-doc links."""
    return f'<a id="{anchor_id}"></a>'


#: Optional native→derived recipe blurbs rendered under each source heading.
SOURCE_RECIPES: dict[str, str] = {
    "gfm": (
        "GFM native extent bands are converted to derived fractions through the "
        "following recipe: build 0/1 masks at native resolution from the extent "
        "bands, mean-pool by the coarsen factor, average-reproject to the ~80 m "
        "processed grid, then accumulate per-class counts across the date group. "
        "`water_fraction` / `flood_fraction` are the class count divided by "
        "`valid_count` (NaN where unobserved); `reference_water` is the "
        "masked-max of native reference-water codes."
    ),
}


def _cross_source_section(registries: dict[str, LayerRegistry]) -> list[str]:
    """Render the cross-source gotchas section.

    The per-source tables above describe each source in isolation. The hazards
    below only appear when GFM, MODIS, and VIIRS outputs are stitched together
    in an aggregate or comparison pipeline. They are not repeated per source
    because none of them is a property of a single source.

    The aggregation-policy table is derived from the registries (not hardcoded)
    so it cannot drift from the per-source tables above.
    """
    sources = sorted(registries)

    def _aggregation(source: str, name: str) -> str:
        """Return the declared aggregation operator for a layer, or '—'."""
        reg = registries.get(source)
        if reg is None or name not in reg:
            return "—"
        return reg.get(name).aggregation

    agg_rows = [
        ("`exclusion_mask`", ("exclusion_mask",)),
        ("`reference_water`", ("reference_water",)),
        ("`flood_fraction` / `water_fraction`", ("water_fraction", "flood_fraction")),
    ]
    agg_table = [
        "| Layer | " + " | ".join(sources) + " |",
        "| --- | " + " | ".join("---" for _ in sources) + " |",
    ]
    for label, names in agg_rows:
        cells = []
        for s in sources:
            vals = sorted({_aggregation(s, n) for n in names if _aggregation(s, n) != "—"})
            cells.append(" / ".join(vals) if vals else "—")
        agg_table.append(f"| {label} | " + " | ".join(cells) + " |")

    return [
        '<a id="layers-cross-source"></a>',
        "## Cross-source gotchas",
        "",
        "The per-source sections above describe each source in isolation. The "
        "hazards below only matter when GFM, MODIS, and VIIRS outputs are "
        "stitched together (unioned, compared, or averaged in one pipeline).",
        "",
        "### `reference_water` differs in both schema and nodata",
        "",
        "- **GFM** `reference_water` is a carried-through **3-class** native band "
        "(`0` = no water, `1` = permanent, `2` = seasonal), `nodata=255`.",
        "- **MODIS** `reference_water` is a **binary** `0/1` mask (classes `1` and `2`), `nodata=0`.",
        "- **VIIRS** `reference_water` is a **binary** `0/1` mask (code `99`), `nodata=0`.",
        "",
        "Code that unions or compares `reference_water` masks across sources "
        "must handle `255` vs `0` explicitly **and** must not collapse GFM's "
        "permanent/seasonal split into a single bit.",
        "",
        "### `0` in `reference_water` means different things across sources",
        "",
        "The nodata/`0` encoding is not a shared data-availability convention:",
        "",
        '- **GFM** — `255` genuinely means "unobserved"; `0` means "observed, no '
        'water". The two are distinguishable from the raster alone.',
        "- **MODIS / VIIRS** — `nodata=0` is a shared **rendering convention** for "
        "all binary derived masks (background renders transparent), **not** a "
        "data-availability flag. A pixel that could not be observed (MODIS "
        "insufficient-data `255`; VIIRS fill/cloud codes `0`/`1`/`30`) is also "
        "written as `0` in `reference_water` — indistinguishable from "
        "genuinely-observed non-water. On a single date (or `peak` strategy) "
        "you must pair `reference_water` with `exclusion_mask` (`1` = fill/cloud/"
        'insufficient) to tell "observed non-water" from "couldn\'t observe". '
        "In `aggregate` mode this is partly mitigated because the masks are "
        "reduced over non-excluded dates only (VIIRS `majority`, MODIS `mode`).",
        "",
        "### `exclusion_mask` is a binary mask for MODIS/VIIRS but native codes for GFM",
        "",
        "- **MODIS / VIIRS** `exclusion_mask` is a clean **binary `0/1`** mask "
        "(`0` = usable, `1` = excluded), `nodata=0`.",
        "- **GFM** `exclusion_mask` is **native multi-valued GFM codes** "
        "(`nodata=255`), passed through untouched — not a binary `0/1` mask.",
        "",
        "Averaging or OR-ing `exclusion_mask` across sources yields garbage on "
        "the GFM side; convert GFM codes to a binary mask before combining.",
        "",
        "### Aggregation policies differ by source",
        "",
        *agg_table,
        "",
        "An aggregate pipeline that mixes sources applies different per-source "
        "logic to the masks. In particular VIIRS is conservative — a pixel is "
        "excluded only if **every** observation was fill/cloud, and "
        "`reference_water` requires a strict majority of usable observations — "
        "while MODIS reduces over all dates. See the per-source operator values "
        "in the table above.",
        "",
        "### `reference_water` semantics differ",
        "",
        "GFM and MODIS both carry a permanent-vs-seasonal split, but VIIRS does not:",
        "",
        "- **GFM** — permanent (`1`) and seasonal (`2`); the seasonal class is "
        "the GFM analog of MODIS `recurring_flood`.",
        "- **MODIS** — `recurring_flood` is a separate derived layer (class `2`); "
        "`reference_water` itself folds classes `1` and `2` together.",
        '- **VIIRS** — only a single "normal water" class (`99`); no permanent/seasonal split exists.',
        "",
        "Recurring/seasonal water is therefore only available from MODIS and GFM, not from VIIRS.",
        "",
    ]


def render_source_markdown(registry: LayerRegistry) -> str:
    """Render one source's native and derived layer tables as Markdown."""
    lines = [_anchor(f"layers-{registry.source_id}"), f"## {registry.source_id}", ""]
    recipe = SOURCE_RECIPES.get(registry.source_id)
    if recipe:
        lines.extend([f"> {recipe}", ""])
    lines.append(_anchor(f"layers-{registry.source_id}-native"))
    lines.append(f"### Native layers ({registry.source_id})")
    lines.append("")
    lines.append("Layers the source physically provides (fetched untouched).")
    lines.append("")
    lines.extend(_native_table(registry.list_native()))
    lines.append("")
    lines.append(_anchor(f"layers-{registry.source_id}-derived"))
    lines.append(f"### Derived layers ({registry.source_id})")
    lines.append("")
    lines.append("Layers Atlantis computes from native inputs (not downloaded).")
    lines.append("")
    lines.extend(_derived_table(registry.list_derived()))
    lines.append("")
    return "\n".join(lines)


def render_all_markdown() -> str:
    """Render every registered source into a single Markdown document.

    Returns:
        Markdown with a title, an intro, and one section per source.
    """
    lines = [
        "# Atlantis layers",
        "",
        "This is the **canonical human-readable layer inventory** for Atlantis.",
        "Other docs should link here instead of repeating native/derived layer tables.",
        "",
        "Auto-generated from the per-source layer registries "
        "(`atlantis.fetchers.<source>.layers`). Do not edit by hand — regenerate "
        "with `python scripts/generate_layer_docs.py` (or `atlantis list-layers`).",
        "",
        "A **native** layer is fetched untouched from the source. A **derived** "
        "layer is computed by Atlantis from native inputs (for example "
        "`flood_fraction`).",
        "",
        "## Quick links",
        "",
    ]
    registries = all_registries()
    for registry in registries.values():
        lines.append(
            f"- `{registry.source_id}`: "
            f"[native](#layers-{registry.source_id}-native) / "
            f"[derived](#layers-{registry.source_id}-derived)"
        )
    lines.append("")
    for registry in registries.values():
        lines.append(render_source_markdown(registry))
    lines.extend(_cross_source_section(registries))
    return "\n".join(lines).rstrip() + "\n"
