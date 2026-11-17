# Nif Vertex Processor

Tools for reading and writing **vertex-colour alpha** and **mask shader flags** in Skyrim SE `.nif` meshes.

Built to paint coverage onto landscape meshes — the edge mask that makes a ground texture fade into its neighbour — and to
weld and re-UV meshes that need it.

Windows. Python 3.10 or newer. **Standard library only** — nothing to install.

---

## What is in here

- **Core reader/writer** — one implementation of the NIF layout, shared by every front end
- **A studio window** — shapes, operations and a preview in one place
- **Two focused GUIs** — the alpha mask, and the weld + UV tool
- **Six command-line tools** — mask, invert, weld + UV, shape operations, block inspector, shape renamer
- **A shell integration script** — right-click a `.nif` and open the tool
- **A launcher menu** — `NifTools.cmd`, so nothing has to be typed

The windows carry their own titles — **0cto shape studio**, **0cto vertex-alpha mask**, **0cto weld + UV**. One tool,
three windows.

---

## Install

- Download the release zip
- Unpack it anywhere — it is portable, no installer, no registry writes
- Run `NifTools.cmd` for a numbered menu, or double-click any `*_gui.py`
- The windows all need a `.nif`: the menu asks for one after you pick a tool, and a path can be passed on the command line
- Optional: install **NifSkope** — the tools open their output in it, and it stays the authority for block names and order

```
NifTools.cmd                          # the menu
NifTools.cmd 2 "D:\meshes\mountaincliff01.nif"
```

Nothing is added to `PATH`. Every tool writes its output next to the file it read, or where you point it.

One exception, now handled by the menu: `nif_vertex.py audit` without `--tsv` resolves `_recon\nif_vertex_audit.tsv`
relative to the folder **above** the unpack folder. The menu passes `--tsv` so the report lands beside the launcher;
if you run `audit` by hand, pass `--tsv <path>`.

---

## The tools

### `nif_studio.py` — the studio window

Three tabs, everything in memory, one Save.

- **Shapes** — a compact table: name, op, vertices, triangles, ring, colour, property, block, texture
- **Operations** — merge + weld, merge without welding, reUV, invert alpha, blend edges, blend + buffer N, blend gradient N, all white, reset
- **Preview** — shades triangles by vertex alpha, so a mask can be checked before it is saved

```
nif_studio.py <mesh.nif>                     # Save writes the SAME file
nif_studio.py <mesh.nif> --save-copy         # Save writes <stem>_edit.nif
nif_studio.py <mesh.nif> --out <folder>      # Save writes <stem>_edit.nif there
nif_studio.py <mesh.nif> --config plan.tsv   # start from a plan file
```

**Save writes the file you opened**, after backing it up to `<stem>.bak.nif` on load — so a NifSkope refresh shows the
edit. Use `--save-copy` or `--out` when you want a new file instead.

Details worth knowing:

- Save only touches **selected** rows. A plain click collapses the selection to one row
- `Select all rows` restores the whole file
- The status line says how many of the listed shapes will be written, and warns when some are not selected
- `Reset` takes alpha values from a pristine re-read of the file, not from the working copy
- Undo is a stack of 20 snapshots
- Save never launches NifSkope; the `Open in NifSkope` button opens the file on request
- The preview is a coverage check, not a material check — open NifSkope for the real look

### `nif_vertex_gui.py` — the alpha mask

Lists every shape with its colour and alpha state, and previews the mask with the merge applied.

```
nif_vertex_gui.py <mesh.nif> [--out <folder>] [--config plan.tsv]
```

- Per-shape settings, or one global setting
- A plan file can drive it, so a recipe survives a re-export
- Writes through the same `nif_vertex.apply_file()` the command line uses
- Writes a **new** file, never the source

### `nif_weld_uv_gui.py` — weld and re-project UVs

```
nif_weld_uv_gui.py <mesh.nif> [--out <folder>] [--config plan.tsv]
```

- One vertex per distinct position, then the triangles are unfolded and relaxed
- Colours merge by mean, **alpha by median** — one vertex left at alpha 0 among 255s means "covered", and a mean would undo it
- Judge it by the 5–95 % length/texel band, not by max/min

### `vertex_alpha_mask.py` — mask, from the command line

```
python vertex_alpha_mask.py --nif <mesh.nif> --mode edges --apply
python vertex_alpha_mask.py --nif <mesh.nif> --mode buffer --buffer 3 --apply
python vertex_alpha_mask.py --nif <mesh.nif> --mode gradient --buffer 4 --apply
python vertex_alpha_mask.py --nif <mesh.nif> --mode white --apply
python vertex_alpha_mask.py --nif <mesh.nif> --mode none --apply
python vertex_alpha_mask.py --nif <folder> --recurse --mode edges --apply
```

- `--mode edges` paints the open edges — the rim where a surface ends
- `--mode buffer` paints `--buffer N` rings inward from every open edge
- `--mode gradient` fades over `--buffer N` rings instead of a hard edge
- `--mode white` makes every vertex opaque, which exempts the shape from the merge
- `--mode none` changes no alpha at all — a property-only pass

Options:

- `--name` narrows to one shape, repeatable, wildcards allowed; every shape is processed otherwise
- `--config <plan.tsv>` gives each shape its own settings; a shape with no line is left alone
- `--write-config <path>` writes a starter plan listing every shape
- `--join-coincident` computes the edge set with coincident vertices joined — needed after a shape merge, or `edges`
  paints a black line along every old seam
- `--all-black ask|flip|skip` answers the "this mask is entirely black" question in advance
- `--replace` overwrites existing alpha instead of keeping the lower of the two
- `--alpha-flags <n>` sets a different `NiAlphaProperty` value (default `4333`)
- `--no-alpha-prop`, `--no-shader-flags`, `--no-verify` skip the property pass or the read-back check
- `--obj <mesh.obj> --out <alphas.txt>` computes alphas from an OBJ instead and writes one alpha per line — an import
  path for a mesh that is not a NIF yet, not an edit

### `invert_vertex_alpha.py` — `a -> 255 - a`

The one operation the studio cannot express.

```
python invert_vertex_alpha.py <mesh.nif> --apply
python invert_vertex_alpha.py <folder> --recurse --name 'tri*' --apply
```

- `--add-missing` gives shapes that have no vertex colours an opaque one first (off by default, like SNIFF)
- `--all-black ask|flip|keep|skip` decides what an all-zero result means
- Writes `<stem>_inv.nif` beside the source

### `nif_weld_uv.py` — weld duplicate vertices, rebuild the UVs

For landscapes, not for every mesh. Welding destroys UV seams by design, so re-projection is part of the job.

```
python nif_weld_uv.py <mesh.nif> --apply
python nif_weld_uv.py <mesh.nif> --apply --uv keep
python nif_weld_uv.py <mesh.nif> --apply --uv planar --axis z
python nif_weld_uv.py <folder> --recurse --apply
```

- `--uv unwrap` (default) unfolds and relaxes; `--uv planar` projects on `--axis`; `--uv keep` welds without touching UVs
- `--scale auto` keeps the original texel density; a number sets UV per world unit
- `--epsilon` — positions closer than this are the same point
- `--color-merge` / `--alpha-merge` choose how welded vertices combine; alpha defaults to **median**
- Writes `<stem>_weld.nif` beside the source

### `nif_shape_ops.py` — merge shapes into one copied shape

The "duplicate and merge into a base" move.

```
python nif_shape_ops.py <mesh.nif>                              # list shapes and their block indices
python nif_shape_ops.py <mesh.nif> --merge 7,8,9 --weld --apply
```

- The originals are not touched and not removed
- A new shape is appended to the same parent node
- `--weld` reduces to one vertex per position
- `--texture-from` picks which source texture set the merged copy takes
- Refuses layouts that cannot be concatenated, and refuses u16 vertex/triangle overflow loudly

### `nif_block_dump.py` — what is in this mesh

Answers "what is this file doing" from the block list, not from the filename.

```
python nif_block_dump.py <mesh.nif> [--strings] [--out report.txt]
```

- Header, block count, type histogram
- Every block in order, its name, and for shapes the vertex/triangle counts
- Shader and alpha flags per shape
- Every `.dds` path found anywhere in the file

### `nif_shapes.py` — read and rewrite trishape names

For renames that touch both the file and the shape inside it.

```
python nif_shapes.py <root> [--token <text>] [--out <folder>] [--report <file>]
```

- Read-only by default — it reports the shapes whose name contains the token
- `--out <folder>` copies the files there and rewrites only the string table, so the shape no longer matches the old name

### `Register-NifToolsMenu.ps1` — right-click a `.nif`

Per-user keys only. No desktop shortcut, no change to the `.nif` default handler.

```
powershell -NoProfile -ExecutionPolicy Bypass -File Register-NifToolsMenu.ps1 -Install
powershell -NoProfile -ExecutionPolicy Bypass -File Register-NifToolsMenu.ps1 -Status
powershell -NoProfile -ExecutionPolicy Bypass -File Register-NifToolsMenu.ps1 -Remove
```

Adds three verbs to the `.nif` right-click menu:

- `Vertex alpha mask...`
- `Weld + reproject UV...`
- `Shape studio...`

---

## What "done" looks like

A shape that uses vertex alpha correctly carries all of this:

| item | value |
|---|---|
| `NiAlphaProperty` flags | `4333` |
| `SLSF1` | Decal + Dynamic_Decal + Vertex_Alpha |
| `SLSF2` | Assume_Shadowmask + Vertex_Colors |
| `ZBuffer_Write` | clear |

Other flag values seen in the wild, decoded rather than guessed:

- `0x12EC` — alpha **test** on, blending off, test function 4. A hard cutout: `alpha == 0` is discarded, everything else is solid
- `0x10CF` — blending on with source 7 / destination 6 = One/One, i.e. **additive**
- A cutout shape with a painted mask reads as `test-painted` — a cutout, not a fade. That is reported separately, never counted as work outstanding
- A shape with **no** vertex colours is not a defect. With no vertex colour to read, blending comes from the texture's alpha channel

---

## Final alpha is texture alpha × vertex alpha

- A missing factor counts as **1**, not 0
- **BC1/DXT1 has no alpha channel**, so its factor is 1 — painted vertex alpha alone decides the fade
- A mesh with **no vertex colours** has a vertex factor of 1, so the texture's alpha alone decides
- Both present means they **multiply** — a painted 128 over a texture alpha of 0.5 lands at 0.25

---

## Self-tests

```
python nif_vertex.py selftest
python nif_vertex.py audit <tree> --recurse --tsv report.tsv
python nif_vertex.py roundtrip <file.nif>
```

- `selftest` — 72 checks over the reader and writer
- `audit` — per-shape colour, alpha flags and shader flags, to a TSV; default `_recon\nif_vertex_audit.tsv`
- `roundtrip` — a no-op save must be byte-identical; this is what caught two header conventions

A mask **only ever darkens**: the written alpha is `min(mask, original)`, so an area the artist already faded stays faded
and the border cannot become a seam. `--replace` overrides that.

---

## Which tool writes what

| tool | writes |
|---|---|
| `vertex_alpha_mask.py --apply` | `<stem>_mask.nif` beside the source, or under `--outdir` |
| `invert_vertex_alpha.py --apply` | `<stem>_inv.nif` |
| `nif_weld_uv.py --apply` | `<stem>_weld.nif` |
| `nif_vertex_gui.py` | `<stem>_mask.nif` |
| `nif_weld_uv_gui.py` | `<stem>_weld.nif` |
| `nif_shape_ops.py --apply --out` | the path you name |
| `nif_shapes.py --out` | a copy of each file, in that folder |
| `nif_studio.py`, Save | **the file you opened**, after `<stem>.bak.nif` was written on load |
| `nif_studio.py --save-copy` / `--out` | `<stem>_edit.nif` |

- Every destination is yours to choose: these are authoring tools for your own files, so `--outdir` / `--out` accept anywhere
- The four command-line writers never edit the source
- Only the studio's default Save does, and it writes the backup first

## Notes on paths

- Sources are never edited by the command-line tools. `--apply` writes a **new** file next to the source
- `--outdir` sends it anywhere else, including into a live modlist folder
- One development test, `nif_studio_math_test.py`, writes a log into a `_recon\` folder used by the author's own workspace. It is a headless self-test, not part of the normal workflow

---

## Versioning

SemVer with a `0P` prefix — `0P1.0.0` — the same scheme as the author's other tools.

- `MAJOR` — an existing plan file, or a NIF written by an earlier version, stops working
- `MINOR` — a new operation, or a batch of fixes
- `PATCH` — one small fix

Pre-releases carry a `-pre.N` suffix and are marked as pre-releases on GitHub.

---

## Status

**Pre-release.** The tools are in daily use on the author's own meshes, and the command-line surface is stable.

Expect rough edges in the windows, not in the files they write.

---

## Licence

No licence file yet — that is a decision still to be made, not an oversight to be read as permission.
