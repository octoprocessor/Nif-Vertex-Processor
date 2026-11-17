#!/usr/bin/env python3
"""nif_block_dump.py -- what is actually in a NIF: every block, its name, its shape data, its textures.

READS : one .nif (read-only)
WRITES: stdout, and optionally --out <file>

Why this exists: "what is this mesh doing" is answered by the block list, not by the filename. This prints,
for one file:
  * the header (version / userVersion / block count) and a type histogram
  * every block in order, with the name it owns (when its first field is a string index) and, for
    BSTriShape, its vertex/triangle counts, shader and alpha references
  * the string table, which is where node names, materials and texture paths actually live
  * every .dds path found anywhere in the file, whether or not a parsed block points at it
  * for each shape: NiAlphaProperty flags + threshold and shader flags 1/2, via nif_vertex's own readers
    (so the numbers agree with the vertex tools instead of being re-derived here)

Not a validator: it reports what is present, and says "unparsed" rather than guessing when a layout does
not match. For per-shape vertex-alpha state use audit_aio_vertex_state.py.

Usage:
    python tools\\nif_block_dump.py <mesh.nif> [--strings] [--out _recon\\dump.txt]
"""
import argparse
import collections
import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nif_vertex                                                     # noqa: E402

NAME_FIRST_TYPES = ("NiNode", "BSFadeNode", "BSLeafAnimNode", "BSValueNode", "NiBillboardNode",
                    "RootCollisionNode", "BSTriShape", "BSSubIndexTriShape", "NiTriShape",
                    "BSLightingShaderProperty", "BSEffectShaderProperty", "BSShaderProperty",
                    "NiAlphaProperty", "NiStringExtraData", "BSXFlags", "NiTextureTransformController",
                    "NiVisController", "BSShaderTextureSet", "NiFloatInterpolator", "NiBoolInterpolator")


def describe(blob, nif, idx):
    """(name, extra fields) for one block. Best effort, never fatal."""
    t = nif.type_of(idx)
    blk = nif.blocks[idx]
    name = ""
    extra = ""
    if t in NAME_FIRST_TYPES and len(blk) >= 4:
        ref, = struct.unpack_from("<I", blk, 0)
        if ref < len(nif.strings):
            name = nif.strings[ref]
        if t in ("NiNode", "BSFadeNode", "BSLeafAnimNode", "NiBillboardNode", "RootCollisionNode"):
            try:
                n_extra, = struct.unpack_from("<I", blk, 4)
                p = 8 + 4 * n_extra + 4                     # + controller
                p += 4                                      # rotation (not in SSE nodes) - probed below
                n_child, = struct.unpack_from("<H", blk, p)
                kids = struct.unpack_from("<%dI" % n_child, blk, p + 2)
                extra = "children %d: %s" % (n_child, list(kids))
            except Exception:
                extra = "children unparsed"
    return name, extra


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mesh")
    ap.add_argument("--strings", action="store_true", help="print the whole string table")
    ap.add_argument("--out", default="", help="also write the report to this file")
    a = ap.parse_args(argv)

    raw = open(a.mesh, "rb").read()
    nif = nif_vertex.Nif(raw)
    lines = []
    add = lines.append

    add("file      : %s" % os.path.abspath(a.mesh))
    add("size      : %d bytes" % len(raw))
    add("version   : 0x%08X  userVersion %d  userVersion2 %d  SSE=%s"
        % (nif.version, nif.user_version, nif.user_version_2, nif.is_sse))
    add("blocks    : %d" % len(nif.blocks))
    hist = collections.Counter(nif.type_of(i) for i in range(len(nif.blocks)))
    add("types     : " + ", ".join("%s x%d" % (k, v) for k, v in sorted(hist.items())))
    add("")

    add("== blocks ==")
    for i in range(len(nif.blocks)):
        name, extra = describe(raw, nif, i)
        add("  [%2d] %-32s %-34s %s" % (i, nif.type_of(i), ("'%s'" % name) if name else "", extra))
    add("")

    add("== shapes ==")
    for sh in nif.shapes():
        try:
            alpha = nif_vertex.read_alpha_property(nif, sh)
        except Exception as exc:
            alpha = "unreadable: %s" % exc
        try:
            sf1, sf2 = nif_vertex.read_shader_flags(nif, sh)
        except Exception as exc:
            sf1, sf2 = ("unreadable", exc)
        add("  %-28s type=%s  verts=%d tris=%d  shader_ref=%s alpha_ref=%s"
            % (sh.name, sh.type, sh.vert, sh.tri, sh.shader_ref, sh.alpha_ref))
        add("       layout %s  stride %d  colors=%s" % (sh.d, sh.stride, sh.has_colors))
        if isinstance(alpha, tuple):
            add("       alpha property: flags=%s threshold=%s" % alpha)
        else:
            add("       alpha property: %s" % (alpha,))
        if isinstance(sf1, int):
            add("       shader flags1=0x%08X flags2=0x%08X" % (sf1, sf2))
        else:
            add("       shader flags: %s" % (sf1,))
    add("")

    dds = []
    for m in re.finditer(rb"[A-Za-z0-9_\\/\-\. ]{3,240}?\.dds", raw):
        s = m.group(0).decode("latin-1").strip()
        if s not in dds:
            dds.append(s)
    add("== texture paths in file (%d) ==" % len(dds))
    for s in dds:
        add("  " + s)
    add("")

    if a.strings:
        add("== string table (%d) ==" % len(nif.strings))
        for i, s in enumerate(nif.strings):
            add("  [%3d] %s" % (i, s))
        add("")

    text = "\n".join(lines)
    print(text)
    if a.out:
        with open(a.out, "w", encoding="utf-8", newline="") as fh:
            fh.write(text + "\n")
        print("\nwrote " + a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
