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


def render_source_markdown(registry: LayerRegistry) -> str:
    """Render one source's native and derived layer tables as Markdown."""
    lines = [f"## {registry.source_id}", ""]
    lines.append("### Native layers")
    lines.append("")
    lines.append("Layers the source physically provides (fetched untouched).")
    lines.append("")
    lines.extend(_native_table(registry.list_native()))
    lines.append("")
    lines.append("### Derived layers")
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
        "Auto-generated from the per-source layer registries "
        "(`atlantis.fetchers.<source>.layers`). Do not edit by hand — regenerate "
        "with `python scripts/generate_layer_docs.py` (or `atlantis list-layers`).",
        "",
        "A **native** layer is fetched untouched from the source. A **derived** "
        "layer is computed by Atlantis from native inputs (for example "
        "`flood_fraction`).",
        "",
    ]
    for registry in all_registries().values():
        lines.append(render_source_markdown(registry))
    return "\n".join(lines).rstrip() + "\n"
