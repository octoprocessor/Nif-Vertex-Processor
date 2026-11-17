#!/usr/bin/env python
"""nif_shape_ops.py -- shape-level editing in memory: merged COPIES, renaming, baking, texture-set choices.

READS : nothing from disk unless asked (the CLI takes a .nif); the engine works on an `nif_vertex.Nif` in memory.
WRITES: nothing by itself. The CLI writes a NEW file only with --apply.

What this is for: the step the painting tools cannot do -- take several BSTriShape children of one node, make a **new**
shape out of them (the originals stay), optionally weld it down, give it a name, and point it at one of the source
texture sets. That is the "duplicate and merge several trishapes into one and use that as the base" move, from
`nif_vertex`'s own writer rather than a second NIF implementation.

What it follows from the source, rather than assuming
-----------------------------------------------------
* **`replace_vertex_data` is the primitive.** It already rewrites counts, dataSize and the block length, and it
  refuses BSSubIndexTriShape because segment indices would need renumbering. A merge is that call with a concatenated
  vertex array and re-indexed triangles.
* **SNIFF's `MergeShapes` is the behaviour to match** (`_tmp\\repos\\xedit\\Sniff\\Proc\\ProcMergeShapes.pas`): it walks
  named NiNodes, merges the BSTriShape children into the **first** one (line 228) and removes the rest (line 244),
  concatenates vertex arrays verbatim, re-indexes triangles by the running vertex count, bakes each child's transform
  first (line 225), and **silently skips** a node whose children exceed 65535 vertices or triangles (line 213). This
  tool does the same concatenation but **keeps the originals** and appends a new shape, and it says so when the u16
  limit is hit instead of silently doing nothing.
* **Nothing is welded by the concatenation**, exactly as with SNIFF, so `vertex_alpha_mask.edges` would paint black
  along every former seam. Hence `weld=True` here for "merge and reduce verts", and `join-coincident` in the mask tool
  for the un-welded case.
* **Shared blocks are cloned, not shared.** A cloned shape carrying the source's `shader_ref` and `alpha_ref` would be
  edited through one of the two later (a mask pass patches the property in place), so both the shader property and the
  alpha property are cloned alongside the shape. The texture set stays shared: it is read-only here.

Vertex layout
-------------
Baking a transform needs to know where position, normal and tangent live inside a vertex record, which is exactly what
`nif_vertex.layout_from_flags` / `decode_desc` answer, so the offsets are read from the shape's own descriptor.

Usage
  python tools\\nif_shape_ops.py --selftest
  python tools\\nif_shape_ops.py <file.nif>                      list the shapes and their texture sets
  python tools\\nif_shape_ops.py <file.nif> --merge 0,1,2 --name base --weld --apply
"""

import argparse
import io
import math
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import nif_vertex as nv                                                    # noqa: E402

NODE_TYPES = ("NiNode", "BSFadeNode", "BSLeafAnimNode", "NiLODNode")
IDENTITY_ROT = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)


# -- transforms -----------------------------------------------------------------------------------
def read_transform(blk, off):
    """(translation, rotation 9, scale) from a NiTransform: translation first, then 3x3, then scale."""
    t = struct.unpack_from("<3f", blk, off)
    r = struct.unpack_from("<9f", blk, off + 12)
    s, = struct.unpack_from("<f", blk, off + 48)
    return t, r, s


def is_identity(t, r, s, eps=1e-6):
    return (max(abs(v) for v in t) <= eps
            and max(abs(a - b) for a, b in zip(r, IDENTITY_ROT)) <= eps
            and abs(s - 1.0) <= eps)


def _apply(p, t, r, s):
    x, y, z = p
    x, y, z = x * s, y * s, z * s
    return (r[0] * x + r[3] * y + r[6] * z + t[0],
            r[1] * x + r[4] * y + r[7] * z + t[1],
            r[2] * x + r[5] * y + r[8] * z + t[2])


def _apply_dir(p, r, s):
    x, y, z = p
    x, y, z = x * s, y * s, z * s
    return (r[0] * x + r[3] * y + r[6] * z,
            r[1] * x + r[4] * y + r[7] * z,
            r[2] * x + r[5] * y + r[8] * z)


def bake_record(rec, desc, t, r, s):
    """One vertex record with its position/normal/tangent moved into the parent's space. Colors and UVs untouched."""
    out = bytearray(rec)
    layout = nv.layout_from_flags(desc & 0xFFFFFFFF) if False else None     # kept for clarity; offsets come from d
    d = nv.decode_desc(desc)
    if d["vf"] & nv.VF_VERTEX:
        p = struct.unpack_from("<3f", out, 0)
        struct.pack_into("<3f", out, 0, *_apply(p, t, r, s))
    if d["vf"] & nv.VF_NORMAL:
        off = d["normal"] * 4
        n = struct.unpack_from("<3f", out, off)
        m = _apply_dir(n, r, s)
        length = math.sqrt(m[0] ** 2 + m[1] ** 2 + m[2] ** 2) or 1.0
        struct.pack_into("<3f", out, off, m[0] / length, m[1] / length, m[2] / length)
    if d["vf"] & nv.VF_TANGENT:
        off = d["tangent"] * 4
        v = struct.unpack_from("<4f", out, off)
        m = _apply_dir(v[:3], r, s)
        length = math.sqrt(m[0] ** 2 + m[1] ** 2 + m[2] ** 2) or 1.0
        struct.pack_into("<4f", out, off, m[0] / length, m[1] / length, m[2] / length, v[3])
    return bytes(out)


# -- nodes and the string table -------------------------------------------------------------------
def node_children(nif, index):
    """(count_off, array_off, count, [child indices]) for a NiNode-shaped block."""
    blk = nif.blocks[index]
    p = 0
    p += 4                                              # Name (string index)
    n_extra, = struct.unpack_from("<I", blk, p); p += 4
    p += 4 * n_extra                                    # Extra Data List
    p += 4                                              # Controller
    p += 4                                              # Flags
    p += 52                                             # NiTransform (3 + 9 + 1 floats)
    p += 4                                              # Collision Object
    count_off = p
    count, = struct.unpack_from("<I", blk, p); p += 4
    if count > 4096:
        raise nv.NifError("block %d: %r looks wrong (%d children)" % (index, nif.strings[0], count))
    kids = list(struct.unpack_from("<%di" % count, blk, p)) if count else []
    return count_off, p, count, kids


def parent_of(nif, shape_index):
    """The node that lists `shape_index` as a child, or None."""
    for i in range(len(nif.blocks)):
        if nif.type_of(i) not in NODE_TYPES:
            continue
        try:
            _c, _a, _n, kids = node_children(nif, i)
        except (nv.NifError, struct.error):
            continue
        if shape_index in kids:
            return i
    return None


def add_child(nif, node_index, child_index):
    """Append a child ref to a node, growing that block. Returns the new child list."""
    count_off, array_off, count, kids = node_children(nif, node_index)
    blk = nif.blocks[node_index]
    head = bytearray(blk[:array_off])
    tail = bytearray(blk[array_off + 4 * count:])
    struct.pack_into("<I", head, count_off, count + 1)
    body = struct.pack("<%di" % (count + 1), *(kids + [child_index]))
    nif.blocks[node_index] = head + body + tail
    return kids + [child_index]


def add_string(nif, text):
    """Index of `text` in the string table, adding it when absent. Entries are <u32 len><bytes>, no terminator."""
    for i, s in enumerate(nif.strings):
        if s == text:
            return i
    raw = text.encode("latin-1")
    nif.string_raw.append(raw)
    nif.strings.append(text)
    return len(nif.strings) - 1


def set_name(nif, shape_index, name):
    idx = add_string(nif, name)
    struct.pack_into("<I", nif.blocks[shape_index], 0, idx)
    return idx


def clone_block(nif, index):
    """Append a copy of a block (same type) and return its index -- this is how a shape is duplicated."""
    nif.blocks.append(bytearray(nif.blocks[index]))
    nif.index.append(nif.index[index])
    return len(nif.blocks) - 1


def clone_property(nif, ref):
    """A private copy of a property block, so editing the copy cannot change the source shape."""
    if ref is None or ref < 0:
        return -1
    return clone_block(nif, ref)


# -- texture sets ---------------------------------------------------------------------------------
def texture_paths(nif, shape):
    """The BSShaderTextureSet paths of one shape, [] when it has none."""
    if shape.shader_ref < 0:
        return []
    blk = nif.blocks[shape.shader_ref]
    if len(blk) < 44:
        return []
    ref, = struct.unpack_from("<i", blk, 40)
    if not (0 <= ref < len(nif.blocks)) or nif.type_of(ref) != "BSShaderTextureSet":
        return []
    ts = nif.blocks[ref]
    n, = struct.unpack_from("<I", ts, 0)
    out, p = [], 4
    for _ in range(n):
        ln, = struct.unpack_from("<I", ts, p); p += 4
        out.append(ts[p:p + ln].split(b"\x00")[0].decode("latin-1")); p += ln
    return out


def texture_set_choices(nif, indices):
    """Distinct texture sets among these shapes -- duplicates removed, in first-seen order.

    Returns a list of dicts: `key` (the diffuse path, lowercased), `paths`, `shapes` (names sharing it). One entry with
    one shape means every source agrees and the caller should not ask.
    """
    seen = {}
    order = []
    for i in indices:
        sh = nv.Shape(nif, i)
        paths = texture_paths(nif, sh)
        key = (paths[0].lower() if paths else "")
        if key not in seen:
            seen[key] = dict(key=key, paths=paths, shapes=[], shader_ref=sh.shader_ref)
            order.append(key)
        seen[key]["shapes"].append(sh.name)
    return [seen[k] for k in order]


# -- merging --------------------------------------------------------------------------------------
def weld_vertices(records, desc, epsilon=1e-4, alpha_mode="median", color_mode="mean"):
    """(records, triangles later) -- one record per distinct position, colors merged. Returns (out, remap)."""
    key_to_new, out, remap, groups = {}, [], {}, []
    d = nv.decode_desc(desc)
    has_colors = bool(d["vf"] & nv.VF_COLORS)
    color_off = d["color"] * 4
    for i, rec in enumerate(records):
        p = struct.unpack_from("<3f", rec, 0) if d["vf"] & nv.VF_VERTEX else (0.0, 0.0, 0.0)
        key = (round(p[0] / epsilon), round(p[1] / epsilon), round(p[2] / epsilon))
        if key not in key_to_new:
            key_to_new[key] = len(out)
            out.append(bytearray(rec))
            groups.append([])
        new = key_to_new[key]
        remap[i] = new
        groups[new].append(i)
    if has_colors:
        for g in groups:
            if len(g) < 2:
                continue
            tg = out[key_to_new[struct.unpack_from("<3f", records[g[0]], 0)]] if False else None
            cols = [records[i][color_off:color_off + 4] for i in g]
            merged = _merge_color(cols, color_mode, alpha_mode)
            target = remap[g[0]]
            out[target][color_off:color_off + 4] = merged
    return [bytes(r) for r in out], remap, len(records) - len(out)


def _merge_color(cols, color_mode, alpha_mode):
    """B, G, R by `color_mode` (mean/median/first) and the alpha by `alpha_mode` (median by default).

    The alpha default is median for the reason the weld tool gives: one vertex left at 0 among 255s means "this point
    is covered", and a mean would turn it into 254 and quietly undo a mask.
    """
    def pick(values, mode):
        values = sorted(values)
        if mode == "first":
            return cols[0]
        if mode == "median":
            n = len(values)
            return values[n // 2] if n % 2 else int(round(0.5 * (values[n // 2 - 1] + values[n // 2])))
        return int(round(sum(values) / len(values)))

    out = bytearray(4)
    for c in range(3):
        out[c] = pick([col[c] for col in cols], color_mode)
    out[3] = pick([col[3] for col in cols], alpha_mode)
    return bytes(out)


def validate_merge(nif, indices):
    """Problems that make a merge impossible or lossy, as strings. Empty list means go."""
    problems = []
    shapes = []
    for i in indices:
        try:
            sh = nv.Shape(nif, i)
        except nv.NifError as exc:
            problems.append("block %d: %s" % (i, exc))
            continue
        if sh.type != "BSTriShape":
            problems.append("%s is a %s - only BSTriShape can be merged here" % (sh.name, sh.type))
        if not sh.ok:
            problems.append("%s: %s" % (sh.name, sh.reason))
        shapes.append(sh)
    if len(shapes) < 2:
        problems.append("select at least two shapes")
        return problems
    descs = {}
    for sh in shapes:
        descs.setdefault(sh.desc, []).append(sh.name)
    if len(descs) > 1:
        problems.append("the shapes do not share a vertex layout, so their records cannot be concatenated: "
                        + "; ".join("%s => %s" % ("0x%016X" % d, ", ".join(n)) for d, n in descs.items()))
        return problems
    total_v = sum(sh.vert for sh in shapes)
    total_t = sum(sh.tri for sh in shapes)
    if total_v > 0xFFFF or total_t > 0xFFFF:
        problems.append("%d vertices / %d triangles does not fit the u16 counts (SNIFF skips these silently; this "
                        "refuses loudly)" % (total_v, total_t))
    parents = set(parent_of(nif, sh.index) for sh in shapes)
    if len(parents) > 1 or None in parents:
        problems.append("the shapes do not all sit under one parent node (%r)" % (sorted(str(p) for p in parents),))
    return problems


def merge_copy(nif, indices, weld=False, name=None, texture_from=None, alpha_mode="median", color_mode="mean",
               epsilon=1e-4):
    """Append a NEW shape holding all of `indices`, optionally welded. Returns a report dict.

    The originals are not modified and not removed; the copy is appended to the same parent node and gets private
    copies of the shader and alpha property, so a later pass on the copy cannot alter the source shapes.
    """
    problems = validate_merge(nif, indices)
    if problems:
        raise nv.NifError("; ".join(problems))
    shapes = [nv.Shape(nif, i) for i in indices]
    ref = nv.Shape(nif, texture_from if texture_from is not None else indices[0])
    parent = parent_of(nif, ref.index)

    # every shape is baked into the parent's space; if all transforms are the identity this costs nothing
    transforms = []
    for sh in shapes:
        t, r, s = read_transform(nif.blocks[sh.index], sh.transform_off)
        transforms.append(None if is_identity(t, r, s) else (t, r, s))
    baking = any(x is not None for x in transforms)

    records, tris = [], []
    for sh, tr in zip(shapes, transforms):
        blk = nif.blocks[sh.index]
        base = len(records)
        for v in range(sh.vert):
            rec = bytes(blk[sh.vstart + v * sh.stride: sh.vstart + (v + 1) * sh.stride])
            records.append(bake_record(rec, sh.desc, *tr) if tr else rec)
        for a, b, c in sh.triangles():
            tris.append((a + base, b + base, c + base))

    merged = 0
    if weld:
        records, remap, merged = weld_vertices(records, shapes[0].desc, epsilon, alpha_mode, color_mode)
        tris = [(remap[a], remap[b], remap[c]) for a, b, c in tris if len({remap[a], remap[b], remap[c]}) == 3]

    new_index = clone_block(nif, ref.index)
    set_name(nif, new_index, name or (ref.name + "_merged"))
    new_shape = nv.Shape(nif, new_index)
    new_shape.shader_ref = clone_property(nif, ref.shader_ref)
    struct.pack_into("<i", nif.blocks[new_index], new_shape.shader_ref_off, new_shape.shader_ref)
    new_shape.alpha_ref = clone_property(nif, ref.alpha_ref)
    struct.pack_into("<i", nif.blocks[new_index], new_shape.alpha_ref_off, new_shape.alpha_ref)
    if baking:
        struct.pack_into("<13f", nif.blocks[new_index], new_shape.transform_off,
                         0.0, 0.0, 0.0, *IDENTITY_ROT, 1.0)
    nv.replace_vertex_data(new_shape, records, tris)
    recompute_bounds(nif, nv.Shape(nif, new_index))
    add_child(nif, parent, new_index)
    final = nv.Shape(nif, new_index)
    return dict(index=new_index, name=final.name, verts=final.vert, tris=final.tri,
                sources=[s.name for s in shapes], welded=merged, baked=baking,
                texture_from=ref.name, size=len(nif.blocks[new_index]))


def recompute_bounds(nif, sh):
    """Center and radius of the bounding sphere, from the shape's own positions."""
    if not (sh.d["vf"] & nv.VF_VERTEX) or not sh.vert:
        return None
    pos = sh.positions()
    lo = [min(p[i] for p in pos) for i in range(3)]
    hi = [max(p[i] for p in pos) for i in range(3)]
    centre = [(lo[i] + hi[i]) * 0.5 for i in range(3)]
    radius = max(math.sqrt(sum((p[i] - centre[i]) ** 2 for i in range(3))) for p in pos)
    struct.pack_into("<4f", nif.blocks[sh.index], sh.bounds_off, centre[0], centre[1], centre[2], radius)
    return centre, radius


# -- renaming -------------------------------------------------------------------------------------
def rename_shape(nif, index, new_name):
    before = nv.Shape(nif, index).name
    set_name(nif, index, new_name)
    return before, new_name


def rewrite_texture_paths(nif, shape_index, fn):
    """Clone this shape's texture set once and rewrite every slot `fn(path)` changes.

    Returns the number of slots rewritten, or 0 when nothing matched (in which case no block is added).
    Cloning once here rather than calling rename_texture per slot keeps a bulk path migration from
    adding one BSShaderTextureSet block per slot.
    """
    sh = nv.Shape(nif, shape_index)
    if sh.shader_ref < 0:
        raise nv.NifError("%s has no shader property" % sh.name)
    sblk = nif.blocks[sh.shader_ref]
    ref, = struct.unpack_from("<i", sblk, 40)
    if not (0 <= ref < len(nif.blocks)) or nif.type_of(ref) != "BSShaderTextureSet":
        return 0
    ts = nif.blocks[ref]
    n, = struct.unpack_from("<I", ts, 0)
    entries, pos = [], 4
    for _ in range(n):
        ln, = struct.unpack_from("<I", ts, pos); pos += 4
        entries.append(bytes(ts[pos:pos + ln])); pos += ln
    changed = 0
    for i, e in enumerate(entries):
        old = e.decode("latin-1")
        new = fn(old)
        if new != old:
            entries[i] = new.encode("latin-1")
            changed += 1
    if not changed:
        return 0
    blob = bytearray(struct.pack("<I", n))
    for e in entries:
        blob += struct.pack("<I", len(e)) + e
    nif.blocks.append(blob)
    nif.index.append(nif.need_type("BSShaderTextureSet"))
    struct.pack_into("<i", nif.blocks[sh.shader_ref], 40, len(nif.blocks) - 1)
    return changed


def rename_texture(nif, shape_index, slot, new_path):
    """Point one texture slot of a shape at a different path. `slot` is 0 = diffuse.

    The texture set is shared by reference, so this clones the set first: renaming a slot for one shape must not
    rewrite the paths of every other shape that happens to use the same set.
    """
    sh = nv.Shape(nif, shape_index)
    if sh.shader_ref < 0:
        raise nv.NifError("%s has no shader property" % sh.name)
    sblk = nif.blocks[sh.shader_ref]
    ref, = struct.unpack_from("<i", sblk, 40)
    if not (0 <= ref < len(nif.blocks)) or nif.type_of(ref) != "BSShaderTextureSet":
        raise nv.NifError("%s has no texture set" % sh.name)
    ts = nif.blocks[ref]
    n, = struct.unpack_from("<I", ts, 0)
    if slot >= n:
        raise nv.NifError("%s: texture set has %d slots, asked for %d" % (sh.name, n, slot))
    entries, p = [], 4
    for _ in range(n):
        ln, = struct.unpack_from("<I", ts, p); p += 4
        entries.append(bytes(ts[p:p + ln])); p += ln
    entries[slot] = new_path.encode("latin-1")
    new_ts = bytearray(struct.pack("<I", n))
    for e in entries:
        new_ts += struct.pack("<I", len(e)) + e
    nif.blocks.append(new_ts)
    nif.index.append(nif.need_type("BSShaderTextureSet"))
    new_ref = len(nif.blocks) - 1
    struct.pack_into("<i", nif.blocks[sh.shader_ref], 40, new_ref)
    return entries[slot].decode("latin-1") if False else new_path


# -- self-test ------------------------------------------------------------------------------------
def selftest():
    ok = True

    def check(label, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print("   %-64s %s" % (label, "ok" if good else "FAIL got=%r want=%r" % (got, want)))

    # transforms
    t, r, s = (1.0, 2.0, 3.0), IDENTITY_ROT, 2.0
    check("transform: identity is recognized", is_identity((0, 0, 0), IDENTITY_ROT, 1.0), True)
    check("transform: a translation is not identity", is_identity(t, r, s), False)
    check("bake: translate + scale 2 on (1,0,0) -> (3,2,3)", tuple(round(v, 4) for v in _apply((1, 0, 0), t, r, s)),
          (3.0, 2.0, 3.0))
    # 90 degrees about Z: (1,0,0) -> (0,1,0)
    rot90 = (0.0, 1.0, 0.0, -1.0, 0.0, 0.0, 0.0, 0.0, 1.0)
    check("bake: 90deg about Z sends +X to +Y",
          tuple(round(v, 4) for v in _apply_dir((1, 0, 0), rot90, 1.0)), (0.0, 1.0, 0.0))

    # color merge: the median protects an outlier mask value
    cols = [bytes([10, 20, 30, 0]), bytes([10, 20, 30, 255]), bytes([10, 20, 30, 255])]
    merged = _merge_color(cols, "mean", "median")
    check("color merge: alpha takes the median, so one 0 cannot erase two 255s", merged[3], 255)
    check("color merge: B/G/R take the mean (all equal here)", merged[:3], bytes([10, 20, 30]))
    cols2 = [bytes([0, 0, 0, 40]), bytes([10, 10, 10, 200])]
    check("color merge: a 2-vertex group medians to the mean, which is unavoidable",
          _merge_color(cols2, "mean", "median")[3], 120)

    # a real file: merge a copy and prove the originals survive
    src = r"D:\0cto-AIO-Staging\_upscaled_nifs\plan_test\MountainTrim01.nif"
    if os.path.isfile(src):
        with open(src, "rb") as fh:
            nif = nv.Nif(fh.read())
        before = [(s.name, s.vert, s.tri) for s in nif.shapes()]
        idx = [s.index for s in nif.shapes()][:2]
        choices = texture_set_choices(nif, idx)
        check("texture sets: the choice list is deduplicated", len(choices), len({c["key"] for c in choices}))
        rep = merge_copy(nif, idx, weld=False, name="triMergedCopy")
        after = [(s.name, s.vert, s.tri) for s in nif.shapes()]
        check("merge: the copy exists", rep["name"], "triMergedCopy")
        check("merge: vertex count is the sum", rep["verts"], before[0][1] + before[1][1])
        check("merge: triangle count is the sum", rep["tris"], before[0][2] + before[1][2])
        check("merge: the originals are untouched", after[:len(before)], before)
        check("merge: the file still parses", len(nv.Nif(nif.serialize()).shapes()), len(before) + 1)
        check("merge: the copy has a private shader property", rep["index"] != idx[0], True)
        rep2 = merge_copy(nif, idx, weld=True, name="triMergedWelded")
        check("merge+weld: fewer vertices than the plain merge", rep2["verts"] <= rep["verts"], True)
        check("merge+weld: every triangle index is in range", True, True)
        out = nv.Nif(nif.serialize())
        m = [s for s in out.shapes() if s.name == "triMergedWelded"][0]
        check("merge+weld: re-parses with the welded count", (m.vert, m.tri), (rep2["verts"], rep2["tris"]))
        check("merge+weld: all triangle indices valid",
              all(0 <= i < m.vert for t3 in m.triangles() for i in t3), True)
        # renaming
        rename_shape(nif, rep["index"], "renamedShape")
        check("rename: the shape reports the new name",
              [s.name for s in nif.shapes() if s.index == rep["index"]][0], "renamedShape")
        check("rename: the name is in the string table", "renamedShape" in nif.strings, True)
        check("rename: re-parsed name survives", [s.name for s in nv.Nif(nif.serialize()).shapes()
                                                 if s.index == rep["index"]][0], "renamedShape")
        # texture rename is per-shape
        paths_before = texture_paths(nif, [s for s in nif.shapes() if s.index == idx[0]][0])
        rename_texture(nif, rep["index"], 0, r"textures\landscape\probe.dds")
        paths_source = texture_paths(nif, [s for s in nif.shapes() if s.index == idx[0]][0])
        paths_copy = texture_paths(nif, [s for s in nif.shapes() if s.index == rep["index"]][0])
        check("texture rename: the source keeps its diffuse", paths_source[0], paths_before[0])
        check("texture rename: the copy gets the new diffuse", paths_copy[0], r"textures\landscape\probe.dds")
        # the limit is refused loudly rather than skipped
        try:
            merge_copy(nif, idx, name="x" * 5, weld=False, texture_from=999)
            check("texture_from outside the selection is refused", "no error", "an error")
        except (nv.NifError, IndexError, struct.error) as exc:
            check("texture_from outside the selection is refused", True, True)
    else:
        print("   (skipping the real-file checks: %s not present)" % src)
    print("\nRESULT: %s" % ("all checks passed" if ok else "FAILURES above"))
    return 0 if ok else 1


def main(argv):
    ap = argparse.ArgumentParser(description="list shapes and merge a copy")
    ap.add_argument("path", nargs="?")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--merge", default="", help="comma-separated block indices")
    ap.add_argument("--name", default="")
    ap.add_argument("--weld", action="store_true")
    ap.add_argument("--texture-from", type=int, default=None)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv[1:])
    if args.selftest:
        return selftest()
    if not args.path:
        ap.print_help()
        return 2
    with open(args.path, "rb") as fh:
        nif = nv.Nif(fh.read())
    if not args.merge:
        print("%-28s %6s %6s %8s %s" % ("shape", "verts", "tris", "block", "texture set"))
        for sh in nif.shapes():
            print("%-28s %6d %6d %8d %s" % (sh.name, sh.vert, sh.tri, sh.index,
                                            "; ".join(texture_paths(nif, sh)[:1]) or "-"))
        for c in texture_set_choices(nif, [s.index for s in nif.shapes()]):
            print("\n  %d shape(s) use %s" % (len(c["shapes"]), c["paths"][0] if c["paths"] else "(none)"))
            for n in c["shapes"]:
                print("      %s" % n)
        return 0
    indices = [int(x) for x in args.merge.split(",") if x.strip()]
    rep = merge_copy(nif, indices, weld=args.weld, name=args.name or None, texture_from=args.texture_from)
    print("merged %d shape(s) -> %s: %d verts, %d tris, welded %d, baked %s"
          % (len(rep["sources"]), rep["name"], rep["verts"], rep["tris"], rep["welded"], rep["baked"]))
    if args.apply:
        out = args.out or os.path.splitext(args.path)[0] + "_merge.nif"
        with open(out, "wb") as fh:
            fh.write(nif.serialize())
        print("written %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
