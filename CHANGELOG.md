# Changelog

All notable changes to **Nif Vertex Processor**.

Versioning is SemVer with a `0P` prefix — `0P0.0.0`, `0P0.1.0` — the same scheme I use on my other tools.

---

## 0P0.0.0 — the first published build

The version this tool has been at while it was being used, now packaged.

### Added

- **`nif_studio.py`** — the studio window
  - Shapes / Operations / Preview tabs
  - Nine operations: merge + weld, merge without welding, reUV, invert alpha, blend edges, blend + buffer N, blend gradient N, all white, reset
  - Per-row operation override
  - Undo stack of 20 snapshots
  - Painter's-algorithm preview shaded by vertex alpha

- **`nif_vertex_gui.py`** — the alpha mask window
  - Every shape listed with its color and alpha state
  - Per-shape settings, plan-file driven or typed in the table
  - `ring` column: the median triangle edge length per shape, so one global ring count can be judged

- **`nif_weld_uv_gui.py`** — the weld and UV window

- **`vertex_alpha_mask.py`** — mask, from the command line
  - `--edges`, `--buffer N`, `--gradient N`, `--white`, `--none`
  - `--config <plan.tsv>`, `--write-config <path>`
  - `--all-black flip|skip`

- **`invert_vertex_alpha.py`** — `a -> 255 - a`

- **`nif_weld_uv.py`** — weld duplicate vertices and rebuild the UV map
  - Texel density calibrated to the original by mean, not by eye
  - Color merged by mean, **alpha by median**

- **`nif_shape_ops.py`** — merge several shapes into one copied shape
  - Originals untouched
  - Transforms baked, bounding sphere recomputed
  - Shader and alpha property cloned, never shared

- **`nif_block_dump.py`** — what is actually inside a `.nif`

- **`nif_shapes.py`** — read and rewrite trishape names

- **`Register-NifToolsMenu.ps1`** — right-click verbs on a `.nif`
  - Per-user keys only
  - No desktop shortcut
  - `.nif` default handler untouched

- **`NifTools.cmd`** — a numbered launcher menu

### Properties and flags

- A painted shape is written with `NiAlphaProperty` **4333**, `SLSF1` Decal + Dynamic_Decal + Vertex_Alpha,
  `SLSF2` Assume_Shadowmask + Vertex_Colors, and `ZBuffer_Write` clear
- Those settings are confirmed by **re-reading the written file**, not assumed
- A mask **only ever darkens**: written alpha is `min(mask, original)`, so an artist's fade is preserved and the
  border cannot become a seam. `--replace` overrides it

### Safety

- The command-line tools never edit the source. Output is always a new file: `<stem>_mask.nif`, `_inv`, `_weld`
- The studio's Save writes **the file you opened**, after backing it up to `<stem>.bak.nif` on load.
  `--save-copy` or `--out DIR` writes `<stem>_edit.nif` instead
- A mask **only ever darkens**: written alpha is `min(mask, original)`, so an artist's fade is preserved and the
  border cannot become a seam. `--replace` overrides it
- An all-black mask is stopped **before anything is written**, and asks: flip it to white, or skip the shape
- There is no "write it all black" answer. A shape that should not exist is culled by the patcher before runtime,
  not painted invisible

### Self-tests

- `python nif_vertex.py selftest` — 72 checks
- `python nif_vertex.py roundtrip <file>` — a no-op save must be byte-identical
- `python nif_vertex.py audit <tree> --recurse` — per-shape state to a TSV

---

## 0P0.0.0 — known limits

- Windows only. The shell integration is PowerShell
- The studio preview is a coverage check. It does not shade materials — NifSkope remains the authority
- The weld tool is an unfold plus relaxation, not LSCM or ABF on closed geometry
- No undo outside the studio window

---

## 0P0.0.0 — license

- **GPL-3.0**, in `LICENSE` — the FSF text, unmodified
- The same license as Community Shaders and AutoSeasons, and the same GPL-3.0 text NifSkope ships as
  `LICENSE-GPL-3.0.txt`
- `Copyright (C) 2026 octoprocessor`
