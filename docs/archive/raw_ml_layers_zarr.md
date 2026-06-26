> [!IMPORTANT]
> **Superseded (Issue #63).** This note argues for *two* physical stores
> (`raw.zarr` + `ml-ready.zarr`). That split has since been **collapsed into a
> single sharded `datacube.zarr`** — once Issue #63 standardised the three core
> channels across all sources, the layers converged (the differentiators were
> mask synthesis, a no-op normalise, and an unrealised lock-free-parallel claim).
> Zarr v3 sharding (256² inner chunk / 2048² shard) now serves both random ML
> tiles and large-window analysis from one store. See
> [`zarr-spec.md`](zarr-spec.md) for the current schema. The reasoning below is
> retained for historical context.

Good question — let me read the `Normaliser` source so I explain "normalised" and "masked" precisely rather than from memory. 

Read [](file:///home/slagaras/atlantis/src/atlantis/harmoniser/normaliser.py)

Great questions — and the answer has a useful nuance the ML team will want to know during review. Here's the reasoning, grounded in `writer.py` and `normaliser.py`.

## Why two layers at all

The two layers exist because **analysis and training have opposite access and write patterns**, and you can't tune one storage scheme to serve both well:

| Concern | `raw.zarr` (analysis-ready) | `ml-ready.zarr` (ML-ready) |
| --- | --- | --- |
| Primary reader | scientists, validation/benchmarking (e.g. the `Bench_CMF_*` notebooks) | training/inference DataLoaders |
| Typical read | large contiguous AOI windows | many small **random** tiles (shuffled minibatches) |
| Write model | **massively parallel** backfill — `1024²` chunks, unsharded, so disjoint workers never touch the same object (lock-free) | **single coordinator** — `2048²` shards bundle 64 inner tiles into one object, so concurrent writers would collide |
| Content | only the variables that actually arrived; physical values preserved | a *guaranteed, consistent* channel set, pre-transformed for models |

The deeper reasons:

1. **Source of truth vs. derived view.** `raw.zarr` is the canonical, faithful archive. `ml-ready.zarr` is an *opinionated, regenerable projection* of it. If the ML team wants a different tile size, normalisation, or channel set, you re-derive the ML layer from raw — no re-fetch, no re-harmonise.
2. **Don't pay preprocessing per epoch.** ML transforms (range contract + mask channels) are applied **once, ahead of time**, so the loader doesn't repeat them every epoch × every worker.
3. **Optimal storage layout per job.** Random `256²` tile reads bundled into few large S3 objects is good for training throughput; large unsharded chunks with parallel writes is good for backfilling a global archive. Separating them lets each use its best layout.
4. **Keep ML assumptions out of the analysis archive.** Forcing generated masks / model encodings into the raw cube would pollute it for analysts who want the unaltered harmonised signal.

## What "normalised" means (and an important nuance)

`write_ml_ready()` calls `_prepare_ml()`, which runs `Normaliser.normalise(ds, variable="flood_fraction")`. The *intent* is to guarantee the model-facing variable sits in a fixed, known range `[0, 1]`.

**The nuance:** in the default `NormaliserConfig`, `flood_fraction` is in `skip_normalise_vars`, so the normalise call is effectively a **no-op / range contract, not a per-image rescale**. From `normaliser.py`:

```python
if variable in self.config.skip_normalise_vars:  # flood_fraction is here
    ds.attrs["normalisation_skipped"] = variable
    return ds
```

This is **deliberate**: `flood_fraction` is already a *physical* fraction in `[0, 1]` from every fetcher. A per-tile min–max stretch would distort its physical meaning and make values incomparable across tiles/events (a 30%-flood pixel would encode differently depending on its tile's max). So:

- For `flood_fraction` → normalisation is a **guarantee that the range is `[0, 1]`**, not a transformation.
- For any variable *not* already in range → `normalise()` would min–max scale it to `[0, 1]` (with optional clip).

So the stored `flood_fraction` bytes are actually **identical** between `raw` and `ml-ready` (both encode `[0,1] → uint8 [0,100]`, decode via `scale_factor=0.01`). The substantive ML-specific change is the masks, below.

## What "masked" means

`_prepare_ml()` **guarantees two companion mask channels exist** on every ML tile, generating them if the source didn't provide them (`writer.py`):

```python
if "quality_mask" not in ds.data_vars:
    ds = ds.assign(quality_mask=normaliser.generate_quality_mask(ds, variable=base))
if "permanent_water" not in ds.data_vars:
    ds = ds.assign(permanent_water=normaliser.generate_permanent_water_mask(ds))
```

- **`quality_mask`** — `uint8` bitflags telling the model/loss which pixels are trustworthy: `0=valid, 1=nodata, 2=cloud, 4=snow, 8=outside AOI`. This is what a training loop uses as an **ignore/weight mask** so cloud- or nodata-corrupted pixels don't contribute to the loss.
- **`permanent_water`** — `uint8` binary (`1=water, 0=land`) marking permanent water bodies (rivers, lakes, sea). It lets the model **distinguish permanent water from actual flood**, so it doesn't learn to label a river as flooding.

"Masked" therefore means: the ML layer ships a **consistent, always-present set of mask channels** alongside `flood_fraction`, regardless of which masks the original source happened to include. The raw layer, by contrast, only stores whatever masks actually arrived.

## Net difference, honestly stated

For the current sources (`flood_fraction` already in `[0,1]`), the real differences the ML layer adds over raw are:

1. **Guaranteed mask channels** (`quality_mask`, `permanent_water`) — generated when missing.
2. **Tile/shard layout** (`256²` tiles in `2048²` shards) tuned for random minibatch reads.
3. A **range contract** on `flood_fraction` (a no-op today, but the hook is there if a non-`[0,1]` variable is ever added).

That last point is worth flagging to the ML team: if they expect per-channel standardisation (zero-mean/unit-variance) rather than a fixed `[0,1]` range, that is *not* what "normalised" currently does — it's intentionally a physical-range guarantee.

Want me to tighten the wording in zarr-spec.md to spell out this "range-contract vs. rescale" nuance, so reviewers aren't misled by the word "normalised"?