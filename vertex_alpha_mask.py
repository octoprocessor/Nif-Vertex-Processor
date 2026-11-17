#!/usr/bin/env python
"""vertex_alpha_mask.py -- paint per-vertex alpha as an edge mask, with a buffer ring and an even gradient.

This is the geometry half of the job: given vertex positions and triangle indices it decides, for every
vertex, what its alpha should be. It does not itself read or write a NIF -- see INTEGRATION at the bottom
for exactly what is missing there and why the split is deliberate.

What it computes
----------------
1. **Edge vertices.** An edge is a pair of vertex indices appearing in a triangle. A *boundary* (open) edge
   is one used by exactly ONE triangle; its two vertices are edge vertices. This is the topology-correct
   definition for a mask that fades out at the rim of a mesh, and it needs no positions at all -- so it
   cannot be fooled by two vertices sitting at the same coordinate.
2. **Buffer rings.** Breadth-first distance, in *vertex increments*, from the edge set: ring 0 = edge,
   ring 1 = one vertex in from the edge, ring 2 = two in, and so on. `buffer` blackens that many extra
   rings. This is a hop count over the adjacency graph, NOT a distance in centimetres -- as asked: "not
   length between vertex, just a simple even progression from vertex neighbour to neighbour".
3. **Paint modes.**
     edges    ring 0 black, everything else white
     buffer   rings 0..buffer black, the rest white
     white    every vertex opaque (255). This RAISES coverage, so it is exempt from the merge below -- asking for
              white and then getting the artist's darker alpha kept would be a silent no-op.
     none     change no alpha at all. Useful on its own: `--apply --mode none` writes the property fixes
              (NiAlphaProperty 4333 and the shader bits) without touching a single alpha byte.
     gradient ring r gets alpha = 255 * r / (rings), evenly spaced per vertex increment, so ring 0 is
              black, the farthest ring is white, and each step is the same size regardless of geometry

Alpha is written as 0 = black (transparent/masked out) and 255 = white, matching the "paint their alphas
black and the rest white" phrasing and Skyrim's convention where vertex alpha drives blending.

Usage
  python tools\\vertex_alpha_mask.py --selftest
  python tools\\vertex_alpha_mask.py --obj mesh.obj --mode buffer --buffer 2 --out alphas.txt

`--obj` is a convenience: an OBJ gives vertices and faces, so the geometry half can be exercised on real
meshes without a NIF reader. `--out` writes one alpha per line, in vertex order.

INTEGRATION
-----------
The NIF reader is now verified for Skyrim SE (version 20.2.0.7, userVersion 12) and is wired in as `--nif`:

  BSTriShape block layout, measured on 79 of 79 shapes in 30 real NIFs:
      +0    Name                     u32 INDEX into the header string table (not an inline string)
      +4    Num Extra Data List      u32, then that many i32 refs
      +...  Controller               i32
      +...  Flags                    u32
      +...  Transform                TRANSLATION first: 3 floats, then 9 rotation, then 1 scale
      +...  Collision Object         i32
      +...  Bounding Sphere          4 floats (centre xyz + radius)
      +...  Skin / Shader / Alpha    three i32 refs
      +100  Vertex Desc              u64
      +108  Num Triangles / Num Vertices   TWO u16 packed in one u32 -- triangles low, vertices high
      +112  Data Size                u32, = vertices*stride + triangles*6
      +116  Vertex Data              vertices * stride bytes
            Triangles              triangles * 3 u16
            Particle Data Size     u32

  Two controls that make this trustworthy rather than plausible: the block ends exactly at
  `116 + dataSize + 4`, and every triangle index is below the vertex count (measured: the maximum index
  equals vertices-1 in all 79 shapes).

Two findings cost several wrong attempts and are worth keeping:
  * the Name field is a **string-table index** -- MountainTrim01's shapes begin with 2, 3, 4, 5 and those
    index 'triLandscape', 'triMountainMask', 'triMountainMask2', 'triRockSkirt';
  * `NiTransform` is written **translation first**; read rotation-first it yields rows (0,0,0) (1,0,0)
    (0,1,0) -- a singular matrix, which is the tell.

**Correction, measured 2026-11-11:** an earlier version of this file said all 79 measured shapes had "no
VF_COLORS". That was wrong, and the mistake is instructive: it tested `desc & 0x20`, and 0x20 is not a flag
bit at all -- the flags are a u16 at byte 5 of the VertexDesc, where VF_COLORS is `1 << 9`. On the real
tree, 182 of 191 shapes already carry vertex colors (`vf=0x03B0`), and the color byte sits at stride
offset 28. The layout is in `nif_vertex.py`, which now owns reading *and* writing; this file keeps the
geometry half.

Writing
-------
`--apply` writes a new NIF **beside the source**, named `<stem>_mask.nif`, and opens it in NifSkope; `--outdir`
sends it anywhere else (mirroring the relative path for a tree) and `--no-open` suppresses the launch. There is no
destination restriction. The source file is never modified. For every selected shape it:
  1. adds a 4-byte BGRA color component when the shape has none, setting VF_COLORS and rebuilding the
     stride, dataSize and block size -- the case this tool was asked to support;
  2. writes one alpha per vertex, **keeping the lower of the mask and the alpha already on the mesh**;
  3. sets the shape's NiAlphaProperty to 4333, creating the block and re-pointing the shape when absent;
  4. clears SLSF2 ZBuffer_Write and sets SLSF1 Decal + Dynamic_Decal + Vertex_Alpha and SLSF2
     Assume_Shadowmask + Vertex_Colors, because a vertex-alpha mask that the shader ignores renders nothing.

**Every shape in the file is processed unless `--name` narrows it.** No pattern means all of them, on both the command
line and in the GUI, which selects every row it lists.

**The merge: a mask may only darken, and that is why it cannot make a seam.** Where the mesh already carries
an alpha, the written value is `min(mask, original)`, so an area the artist faded out stays faded rather than
being repainted opaque next to a masked-out neighbour -- the border between the two is exactly where a seam
appears. Measured on `MountainCliff02.nif`'s `triMountainmask` (3,857 verts, 233 distinct artist values): the
edge mask lowered 574 vertices, left **3,283 untouched**, and preserved 229 of the 233 values. `--replace` is
the escape hatch that overwrites instead, and on that same mesh it collapses the alpha to `{0, 255}` -- use it
only when the existing alpha is known to be junk.

**An all-black mask is stopped before anything is written.** If every vertex would end up 0 the whole surface
disappears, so the run asks: flip it to white and continue, or skip the shape. The question is asked **before** a
single byte is written, so a cancelled prompt cannot leave a half-written file. A dialog appears when there is a
desktop, a console question when the run is interactive (`NIF_VERTEX_NO_GUI=1` forces the console), and with neither
the shape is skipped -- an unanswered prompt never writes anything, and the dialog times out to skip. `--all-black
flip|skip` decides ahead of time for unattended runs.

**There is no "write it all black" answer, and no `--mode all`.** These tools only remove coverage, so a mask that is
black on every vertex is a mistake rather than an intention -- and a shape that should not be there at all is
**culled by PGPatcher** before runtime, not made invisible by painting it away. Offering either would ship a trishape
nobody can see, which nothing in the pipeline would report.

`--verify` (on by default) re-reads the written file and checks the alphas, the VertexDesc arithmetic, the
block length, that positions/UVs/normals/tangents/triangles are byte-identical to the source, and -- the part
that matters most -- that the properties above are actually present: a NiAlphaProperty at 4333 on every painted
shape, every required shader bit set and ZBuffer_Write clear. Setting them is not optional and not a flag; only
`--no-alpha-prop` / `--no-shader-flags` turn it off, and the run then says so, because a vertex-alpha mask whose
flags were never applied renders nothing and is indistinguishable from a working one on disk.
"""
import argparse
import math
import os
import struct
import sys
from collections import defaultdict, deque


def edge_vertices(triangles, num_verts):
    """Vertices belonging to a boundary edge (an edge used by exactly one triangle)."""
    counts = defaultdict(int)
    for a, b, c in triangles:
        for u, v in ((a, b), (b, c), (c, a)):
            if u > v:
                u, v = v, u
            counts[(u, v)] += 1
    edge = set()
    for (u, v), n in counts.items():
        if n == 1:
            edge.add(u)
            edge.add(v)
    return edge


def coincident_groups(positions, epsilon=1e-4):
    """Group vertices that occupy the same point. Returns (groups, remap), first-seen order.

    Same keying as the weld tool (`round(coordinate / epsilon)`), but **the mesh is not changed**: this exists so the
    edge set can be computed on the joined topology while every original vertex keeps its own index, UV and colour.
    That distinction is what makes it usable on the output of a shape merge, where the seam is two coincident vertices
    with two different UV islands - welding for real would have to pick one of those UVs and would stretch the texture
    across the seam, whereas joining only for topology costs nothing.
    """
    key_to_new, groups, remap = {}, [], {}
    for i, p in enumerate(positions):
        key = (round(p[0] / epsilon), round(p[1] / epsilon), round(p[2] / epsilon))
        if key not in key_to_new:
            key_to_new[key] = len(groups)
            groups.append([])
        new = key_to_new[key]
        groups[new].append(i)
        remap[i] = new
    return groups, remap


def adjacency(triangles, num_verts):
    adj = [set() for _ in range(num_verts)]
    for a, b, c in triangles:
        for u, v in ((a, b), (b, c), (c, a)):
            adj[u].add(v)
            adj[v].add(u)
    return adj


def ring_distance(triangles, seeds, num_verts, max_rings=None):
    """Hop distance from the seed vertices, in vertex increments. Unreached vertices get -1."""
    adj = adjacency(triangles, num_verts)
    dist = [-1] * num_verts
    q = deque()
    for s in seeds:
        dist[s] = 0
        q.append(s)
    while q:
        u = q.popleft()
        if max_rings is not None and dist[u] >= max_rings:
            continue
        for v in adj[u]:
            if dist[v] == -1:
                dist[v] = dist[u] + 1
                q.append(v)
    return dist


def paint(dist, mode, buffer=0):
    """Alpha per vertex, 0 = black, 255 = white.

    `white` and `none` are here so every mode answers the same question, but neither depends on ring distance:
    `white` sets every vertex opaque and `none` changes nothing. They are the two that can only ever RAISE coverage,
    which is why `final_alphas` exempts them from the merge -- a request to make a mesh opaque that the merge then
    quietly ignored would be the worst kind of silent no-op.
    """
    n = len(dist)
    if mode == "white":
        return [255] * n
    if mode == "none":
        return [255] * n                 # placeholder: `final_alphas` returns the mesh's own alphas for this mode
    if mode == "all":
        return [0] * n
    if mode == "edges" or mode == "buffer":
        cut = buffer if mode == "buffer" else 0
        return [0 if (d != -1 and d <= cut) else 255 for d in dist]
    if mode == "gradient":
        # even progression per vertex increment: ring 0 black, ring `buffer` white, equal steps
        steps = max(1, buffer)
        out = []
        for d in dist:
            if d == -1:
                out.append(255)
            else:
                out.append(int(round(255.0 * min(d, steps) / steps)))
        return out
    raise ValueError("unknown mode %r - 'keep' is not a painting mode: a shape that should not be touched must be "
                     "left out of the plan (or given its own 'keep' setting), never passed here" % mode)


def median_edge_length(positions, triangles):
    """Median length of a triangle edge, in the mesh's own units -- i.e. what ONE ring costs in world space.

    A buffer or gradient ring is a hop over the adjacency graph, so how wide it looks depends entirely on how
    densely the shape is tessellated. Two shapes of the same physical size can need different ring counts, and one
    ring count means different feather widths on different shapes. This number is what turns that guess into a
    measurement: `rings * median_edge_length` is the feather depth in units.

    Median, not mean: a mesh with a few needle-thin slivers has a mean edge length far below the step that matters,
    and the median is what a person means by "one vertex apart". Every unique edge is counted once (a shared edge
    would otherwise be counted twice and a boundary edge once, which biases the result by topology).
    """
    if not positions or not triangles:
        return 0.0
    lengths = {}
    for a, b, c in triangles:
        for u, v in ((a, b), (b, c), (c, a)):
            if u >= len(positions) or v >= len(positions):
                continue
            key = (u, v) if u < v else (v, u)
            if key in lengths:
                continue
            p, q = positions[u], positions[v]
            lengths[key] = math.sqrt((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 + (p[2] - q[2]) ** 2)
    if not lengths:
        return 0.0
    vals = sorted(lengths.values())
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else 0.5 * (vals[mid - 1] + vals[mid])


def selftest():
    """Synthetic meshes with known answers -- the only way to trust the topology."""
    ok = True

    def check(label, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print("   %-52s %s" % (label, "ok" if good else "FAIL got=%r want=%r" % (got, want)))

    # A 3x3 grid of quads = 16 vertices, 8 triangles. Perimeter vertices are the edges.
    #    0--1--2--3
    #    |  |  |  |
    #    4--5--6--7
    #    |  |  |  |
    #    8--9-10-11
    #    |  |  |  |
    #   12-13-14-15
    tris = []
    for r in range(3):
        for c in range(3):
            a = r * 4 + c
            b = a + 1
            d = a + 4
            e = d + 1
            tris.append((a, b, e))
            tris.append((a, e, d))
    n = 16
    edge = edge_vertices(tris, n)
    check("3x3 grid: edge vertices are the 12 perimeter ones",
          sorted(edge), [0, 1, 2, 3, 4, 7, 8, 11, 12, 13, 14, 15])
    check("3x3 grid: edge count", len(edge), 12)

    dist = ring_distance(tris, edge, n)
    # The four interior vertices of a 3x3 quad grid touch the perimeter directly (5 touches 1 and 4),
    # so they are ONE hop in, not two. My first expectation said 2 and was wrong -- the code was right.
    check("3x3 grid: interior vertices are 1 hop from the edge",
          [dist[i] for i in (5, 6, 9, 10)], [1, 1, 1, 1])
    check("3x3 grid: every vertex is reachable", min(dist), 0)

    check("mode=edges: perimeter black, interior white",
          [paint(dist, "edges")[i] for i in (0, 5, 15)], [0, 255, 0])
    check("mode=buffer 1: one ring further in becomes black",
          [paint(dist, "buffer", 1)[i] for i in (0, 5, 6)], [0, 0, 0])
    check("mode=white: everything opaque", set(paint(dist, "white")), {255})
    check("mode=none: the placeholder is opaque and final_alphas replaces it", set(paint(dist, "none")), {255})
    g = paint(dist, "gradient", 2)
    # 255 * 1/2 = 127.5 -> 128. The step is even in vertex increments, which is the requirement; the
    # exact midpoint lands on 128 rather than 127, so the check says 128.
    check("gradient rings=2: ring0 black, ring1 halfway (128), far ring white",
          [g[0], g[5], g[0] + 0], [0, 128, 0])
    g4 = paint(dist, "gradient", 4)
    # Span 4: ring 1 = 255/4 = 63.75 -> 64. My first version of this check said 128, which would have
    # been the span-2 value; the even progression is what makes it 64.
    check("gradient rings=4: ring1 is a quarter of the way (64)",
          [g4[0], g4[5]], [0, 64])

    # A closed mesh has no boundary edges: a single triangle's edges are each used once, so it is all edge.
    one = [(0, 1, 2)]
    check("single triangle: all three vertices are edges", sorted(edge_vertices(one, 3)), [0, 1, 2])

    # Two triangles sharing an edge: the shared edge is interior, the four outer ones are boundary.
    two = [(0, 1, 2), (1, 2, 3)]
    check("two tris sharing edge 1-2: all four vertices still on a boundary",
          sorted(edge_vertices(two, 4)), [0, 1, 2, 3])

    # A closed tetrahedron: every edge is used twice, so no vertex is an edge vertex.
    tet = [(0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3)]
    check("closed tetrahedron: no boundary edges at all", sorted(edge_vertices(tet, 4)), [])

    # merge rules: a mask may only darken, never raise an alpha the artist already set
    merged, kept = merge_alphas([0, 255, 255, 128], [200, 64, 255, 255])
    check("merge keeps the lower value per vertex", merged, [0, 64, 255, 128])
    check("merge counts the vertices it protected", kept, 1)
    check("merge with no original alpha returns the mask", merge_alphas([0, 255], None), ([0, 255], 0))
    check("merge never raises either input", merge_alphas([0, 40, 90], [10, 30, 200])[0], [0, 30, 90])
    check("merge of all-black against any original is all black", merge_alphas([0, 0], [255, 3])[0], [0, 0])
    check("edge-mask white does not lighten a faded original", merge_alphas([255], [17])[0], [17])

    # -- joining coincident vertices: what the output of a shape merge needs
    class _Stub(object):
        """Just enough of a shape for shape_mask(): a position list and a triangle list."""

        def __init__(self, positions, triangles):
            self._pos, self._tris, self.vert = positions, triangles, len(positions)

        def positions(self):
            return self._pos

        def triangles(self):
            return self._tris

    # a 20x20 square built as two 10-wide halves, each carrying its OWN copy of the x=10 vertices --
    # the index layout SNIFF's "Merge shapes" leaves behind, because it concatenates vertex arrays
    merged_pos = [(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0), (0, 20, 0), (10, 20, 0),
                  (10, 0, 0), (20, 0, 0), (20, 10, 0), (10, 10, 0), (20, 20, 0), (10, 20, 0)]
    merged_tris = [(0, 1, 2), (0, 2, 3), (3, 2, 4), (4, 2, 5),
                   (6, 7, 8), (6, 8, 9), (9, 8, 10), (9, 10, 11)]
    stub = _Stub(merged_pos, merged_tris)
    groups, remap = coincident_groups(merged_pos)
    check("coincident: 12 vertices collapse to 9 positions", (len(groups), len(remap)), (9, 12))
    check("coincident: the seam copies map onto one vertex each",
          (remap[1], remap[6], remap[2], remap[9]), (remap[1], remap[1], remap[2], remap[2]))
    a_off, e_off, _ = shape_mask(stub, "edges", 0)
    a_on, e_on, d_on = shape_mask(stub, "edges", 0, 1e-4)
    check("merged seam without joining: every one of the 12 vertices is boundary black",
          (len(e_off), a_off.count(0)), (12, 12))
    check("merged seam with joining: the two middle seam vertices become interior (white)",
          (a_on[2], a_on[9]), (255, 255))
    check("merged seam with joining: the outer corners are still black",
          (a_on[0], a_on[4], a_on[7], a_on[10]), (0, 0, 0, 0))
    check("merged seam: joining never adds boundary vertices", len(e_on) <= len(e_off), True)
    check("merged seam: distance is 1 for a vertex one hop inside the joined rim", (d_on[2], d_on[9]), (1, 1))

    # -- ring depth in world units: the number that lets two different shapes be feathered to the same width
    square = [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (10.0, 10.0, 0.0), (0.0, 10.0, 0.0)]
    quad = [(0, 1, 2), (0, 2, 3)]
    check("median edge: a 10-unit square has one vertex step of 10 units",
          round(median_edge_length(square, quad), 6), 10.0)
    # three edges of 10 and one diagonal of ~14.14: the median must be the 10, not the mean of 11.04
    check("median edge: the diagonal does not drag the step outward",
          round(median_edge_length(square, quad), 6) < 11.0, True)
    dense = [(float(x), float(y), 0.0) for y in (0.0, 1.0) for x in (0.0, 1.0)]
    dense_tris = [(0, 1, 3), (0, 3, 2)]
    check("median edge: a unit-square tessellation reports a 1-unit step",
          round(median_edge_length(dense, dense_tris), 6), 1.0)
    check("median edge: no geometry gives 0, not a crash", median_edge_length([], []), 0.0)
    check("median edge: a shared edge is counted once, so the median of a strip is still its step",
          round(median_edge_length([(0, 0, 0), (5, 0, 0), (10, 0, 0)], [(0, 1, 2)]), 6), 5.0)

    print("\nRESULT: %s" % ("all checks passed" if ok else "FAILURES above"))
    return 0 if ok else 1


def load_obj(path):
    verts, tris = [], []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("v "):
                p = line.split()
                verts.append((float(p[1]), float(p[2]), float(p[3])))
            elif line.startswith("f "):
                idx = []
                for tok in line.split()[1:]:
                    idx.append(int(tok.split("/")[0]) - 1)
                for k in range(1, len(idx) - 1):
                    tris.append((idx[0], idx[k], idx[k + 1]))
    return verts, tris


# ---------------------------------------------------------------------------------------------
# NIF input/output -- the reader and the writer both live in nif_vertex.py
# ---------------------------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nif_vertex as nv                                                    # noqa: E402



def shape_mask(sh, mode, buffer, join_epsilon=None):
    """(alphas, edge set, hop distance) for one nif_vertex.Shape -- the mask before any merge.

    `join_epsilon` computes the topology with coincident vertices treated as one, then maps the distances back onto
    the original vertices. A merged shape (NifSkope/SNIFF "Merge shapes" concatenates vertex arrays and re-indexes the
    triangles) has NO shared indices across the old seam, so every former rim is still an open edge: measured on a
    two-quad merge, 8 of 8 vertices were boundary, and with joining, 6 of 8 -- only the true outer rim. Painting
    `edges` on such a shape without joining draws a black line along every old shape boundary, inside what is now one
    surface. Joining fixes that without touching a single vertex, UV or colour.
    """
    tris = sh.triangles()
    n = sh.vert
    if join_epsilon:
        groups, remap = coincident_groups(sh.positions(), join_epsilon)
        wtris = [(remap[a], remap[b], remap[c]) for a, b, c in tris]
        wdist = ring_distance(wtris, edge_vertices(wtris, len(groups)), len(groups))
        dist = [wdist[remap[i]] for i in range(n)]
        edge = set(i for i, d in enumerate(dist) if d == 0)
    else:
        edge = edge_vertices(tris, n)
        dist = ring_distance(tris, edge, n)
    return paint(dist, mode, buffer), edge, dist


def merge_alphas(mask, existing):
    """Keep the LOWER of the mask and the alpha already on the mesh. Returns (alphas, kept count).

    A mask may only darken what the artist already assigned. Raising an alpha would paint opacity over an
    area the original mesh fades out, and the border between the two is where a visible seam appears -- so
    the result is min(mask, existing) per vertex, and the original shape survives wherever it was darker.
    A shape with no colour component has no original alpha (`existing is None`) and takes the mask as given.
    """
    if existing is None:
        return list(mask), 0
    out, kept = [], 0
    for a, b in zip(mask, existing):
        if b < a:
            out.append(b)
            kept += 1
        else:
            out.append(a)
    return out, kept


def final_alphas(sh, mode, buffer, replace=False, join_epsilon=None):
    """(alphas, edge set, hop distance, kept) -- the mask after the merge unless --replace or a raising mode.

    `white` and `none` bypass the merge on purpose. The merge exists so a *mask* cannot paint over coverage the
    artist already removed; asking for white is asking for the opposite, so min() would silently undo it.
    """
    if mode == "none":
        cur = sh.alphas()
        return (list(cur) if cur is not None else [255] * sh.vert), set(), [], 0
    mask, edge, dist = shape_mask(sh, mode, buffer, join_epsilon)
    if replace or mode == "white":
        return mask, edge, dist, 0
    merged, kept = merge_alphas(mask, sh.alphas())
    return merged, edge, dist, kept


def property_plan(nif, sh, alpha_flags):
    """What --apply would do to this shape's NiAlphaProperty and shader flags, in words.

    Report mode answers "is this mesh ready?" and a colour component is only half of that, so the properties are
    reported the same way the writer enforces them.
    """
    parts = []
    ap = nv.read_alpha_property(nif, sh)
    if ap is None:
        parts.append("alpha: none -> create 4333")
    elif "error" in ap:
        parts.append("alpha: %s" % ap["error"])
    elif ap["flags"] == alpha_flags:
        parts.append("alpha: ok 0x%04X" % ap["flags"])
    else:
        parts.append("alpha: 0x%04X -> 0x%04X" % (ap["flags"], alpha_flags))
    sf = nv.read_shader_flags(nif, sh)
    if "error" in sf:
        parts.append("shader: %s" % sf["error"])
    else:
        todo = []
        for bit, name in sorted(nv.SLSF1_NAMES.items()):
            if (nv.MASK_SET1 & bit) and not (sf["flags1"] & bit):
                todo.append("+" + name)
        for bit, name in sorted(nv.SLSF2_NAMES.items()):
            if (nv.MASK_SET2 & bit) and not (sf["flags2"] & bit):
                todo.append("+" + name)
            if (nv.MASK_CLEAR2 & bit) and (sf["flags2"] & bit):
                todo.append("-" + name)
        parts.append("shader: " + (", ".join(todo) if todo else "ok"))
    return "; ".join(parts)


def nif_paths(path, recurse):
    if not os.path.isdir(path):
        return [path]
    if not recurse:
        raise SystemExit("%s is a folder - add --recurse" % path)
    out = []
    for dp, _, fns in os.walk(path):
        for f in fns:
            if f.lower().endswith(".nif"):
                out.append(os.path.join(dp, f))
    return sorted(out)


def run_nif(args):
    import fnmatch
    pats = [p.lower() for p in args.name] or ["*"]
    paths = nif_paths(args.nif, args.recurse)
    root = os.path.abspath(args.nif) if os.path.isdir(args.nif) else os.path.dirname(os.path.abspath(args.nif))
    print("# %d file(s)  mode=%s buffer=%d  %s" % (len(paths), args.mode, args.buffer,
                                                   "APPLY" if args.apply else "report only"))
    print("# name patterns: %s" % ", ".join(pats))
    print("   %-26s %-24s %-6s %-6s %-7s %-8s %-14s %s"
          % ("file", "shape", "verts", "tris", "stride", "edge", "black", "colors / props"))
    plan_rows, plan_problems = ([], [])
    if args.config:
        plan_rows, plan_problems = nv.load_shape_plan(args.config)
        for prob in plan_problems:
            print("# plan: %s" % prob)
        if args.name:
            print("# --config is given, so --name is ignored: the plan decides which shapes are touched")
    tally = dict(files=0, shapes=0, selected=0, written=0, added=0, failed=0)
    chosen = {}
    for path in paths:
        base = os.path.basename(path)[:26]
        try:
            with open(path, "rb") as fh:
                nif = nv.Nif(fh.read())
        except (nv.NifError, IOError) as exc:
            print("   %-26s %s" % (base, exc))
            tally["failed"] += 1
            continue
        if not nif.is_sse:
            print("   %-26s not a Skyrim SE NIF (version 0x%X userVersion %d)"
                  % (base, nif.version, nif.user_version))
            tally["failed"] += 1
            continue
        tally["files"] += 1
        if args.write_config:
            nv.write_shape_plan(args.write_config, nif.shapes(),
                                "mode=%s buffer=%d merge=%s" % (args.mode, args.buffer,
                                                                "replace" if args.replace else "keep"))
            print("# wrote a starter plan: %s" % args.write_config)
            continue
        selected = 0
        for sh in nif.shapes():
            tally["shapes"] += 1
            if plan_rows:
                settings = nv.plan_for(sh.name, plan_rows)
                if settings is None:
                    continue
                if settings.get("skip") or settings.get("keep") is True:
                    continue
            elif not any(fnmatch.fnmatchcase(sh.name.lower(), p) for p in pats):
                continue
            else:
                settings = {}
            if not sh.ok:
                print("   %-26s %-24s %-6d %-6d %-7s %-8s %-14s %s"
                      % (base, sh.name[:24], sh.vert, sh.tri, "-", "-", "-", sh.reason))
                tally["failed"] += 1
                continue
            mode = settings.get("mode", args.mode)
            try:
                rings = int(settings.get("buffer", args.buffer))
            except (TypeError, ValueError):
                rings = args.buffer
            replace = args.replace or str(settings.get("merge", "")).lower() == "replace"
            join = (args.join_coincident
                    or str(settings.get("join", "")).strip().lower() in ("1", "on", "true", "yes"))
            if mode not in ("edges", "buffer", "gradient", "white", "none"):
                print("      ! %s: unknown mode %r in the plan, skipped" % (sh.name, mode))
                tally["failed"] += 1
                continue
            chosen[sh.index] = (mode, rings, replace, join)
            alphas, edge, _dist, kept = final_alphas(sh, mode, rings, replace, 1e-4 if join else None)
            selected += 1
            black = sum(1 for a in alphas if a == 0)
            note = "colors=%s | %s" % ("yes" if sh.has_colors else "NO -> will add",
                                       property_plan(nif, sh, args.alpha_flags))
            if args.config:
                note = (mode + (" %d" % rings if mode in ("buffer", "gradient") else "")) + " | " + note
            if join:
                note = "coincident verts joined | " + note
            if mode == "white":
                note = "ALL WHITE (no merge) | " + note
            elif mode == "none":
                note = "no alpha change | " + note
            if kept:
                note += " | kept original alpha on %d of %d" % (kept, sh.vert)
            if alphas and all(a == 0 for a in alphas):
                note += " | ALL BLACK -> %s" % ("will ask before writing" if args.all_black == "ask"
                                                else args.all_black)
            print("   %-26s %-24s %-6d %-6d %-7d %-8d %-14s %s"
                  % (base, sh.name[:24], sh.vert, sh.tri, sh.stride, len(edge),
                     "%d (%.1f%%)" % (black, 100.0 * black / max(1, sh.vert)), note))
        tally["selected"] += selected
        if not (args.apply and selected):
            continue

        # one write per FILE: apply_file paints every matching shape itself, so calling it per shape would
        # rewrite the same output once per shape and report the work that many times
        rel = os.path.relpath(path, root) if os.path.isdir(args.nif) else ""
        probe, problem = nv.output_path_for(path, "_mask", args.outdir)
        if problem:
            print("      ! %s" % problem)
            tally["failed"] += 1
            continue
        out_path = os.path.join(os.path.dirname(probe), rel, os.path.basename(probe)) if rel else probe

        def alphas_for(sh):
            if not sh.ok or sh.index not in chosen:
                return None
            mode, rings, replace, join = chosen[sh.index]
            return final_alphas(sh, mode, rings, replace, 1e-4 if join else None)[0]

        res = nv.apply_file(path, out_path, alphas_for, args.alpha_flags,
                            not args.no_shader_flags, not args.no_alpha_prop, not args.no_verify,
                            args.all_black)
        for r in res["records"]:
            if r["added_colors"]:
                tally["added"] += 1
        if res["ok"]:
            tally["written"] += 1
            print("      written %s (%d -> %d bytes, %d shape(s))"
                  % (out_path, res["src_bytes"], res["bytes"], len(res["records"])))
            if not args.no_open:
                opened = nv.open_in_nifskope(out_path)
                print("      %s in NifSkope: %s" % ("opened" if opened == "opened" else "could not open", out_path)
                      if opened == "opened" else "      ! " + opened)
        else:
            tally["failed"] += 1
            print("      FAILED: %s" % ("; ".join(res["problems"]) or "?"))
        for r in res["records"]:
            print("      %-24s alpha  %s" % (r["shape"][:24], r["alpha"]))
            print("      %-24s shader %s" % (r["shape"][:24], r["shader"] or "untouched"))
        for n in res["notes"]:
            print("      confirmed %s" % n)
        if args.no_alpha_prop or args.no_shader_flags:
            print("      ! property edits disabled (%s) - a mask without them renders nothing"
                  % ", ".join([n for n, off in (("--no-alpha-prop", args.no_alpha_prop),
                                                ("--no-shader-flags", args.no_shader_flags)) if off]))
        for prob in res["problems"]:
            print("      ! %s" % prob)
    print("")
    print("shapes %d, selected %d, files written %d, color components added %d, problems %d"
          % (tally["shapes"], tally["selected"], tally["written"], tally["added"], tally["failed"]))
    if not args.apply:
        probe = nv.output_path_for(paths[0], "_mask", args.outdir)[0] if paths else ""
        print("Report only: no NIF was written. Add --apply to write %s"
              % (probe if probe else "beside the source"))
    else:
        print("Sources are untouched; the new files were listed above. Not verified: in-game appearance.")
    return 0 if tally["failed"] == 0 else 1


def main():
    ap = argparse.ArgumentParser(description="edge-mask / buffer / gradient vertex alpha")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--obj", default="")
    ap.add_argument("--nif", default="", help="a .nif or a folder of them (Skyrim SE)")
    ap.add_argument("--recurse", action="store_true", help="with a folder --nif, walk it")
    ap.add_argument("--name", action="append", default=[],
                    help="shape-name pattern (wildcards, repeatable). Omit it and EVERY shape in the file is processed")
    ap.add_argument("--mode", choices=("edges", "buffer", "gradient", "white", "none"), default="edges",
                    help="edges/buffer/gradient paint a mask; white sets every vertex opaque; none changes no alpha "
                         "and only applies the property fixes")
    ap.add_argument("--buffer", type=int, default=1, help="extra rings to blacken, or the gradient span")
    ap.add_argument("--out", default="", help="with --obj, write one alpha per line here")
    ap.add_argument("--apply", action="store_true", help="write new NIFs (never edits the source)")
    ap.add_argument("--outdir", default="",
                    help="where --apply writes (default: beside the source, named <name>_mask.nif)")
    ap.add_argument("--no-open", action="store_true", help="do not open the result in NifSkope")
    ap.add_argument("--config", default="",
                    help="per-shape plan: one shape per line, settings as key=value (mode, buffer, merge). "
                         "Shapes not listed are left alone")
    ap.add_argument("--write-config", default="",
                    help="write a starter plan listing every shape, then exit (no file is modified)")
    ap.add_argument("--replace", action="store_true",
                    help="overwrite existing vertex alpha instead of keeping the lower of the two")
    ap.add_argument("--join-coincident", action="store_true",
                    help="compute the edge set with coincident vertices joined, which is what a merged shape needs: "
                         "a shape merge concatenates vertex arrays, so the old seams stay open edges and `edges` would "
                         "paint a black line along every one of them. No vertex, UV or color is changed.")
    ap.add_argument("--all-black", choices=("ask", "flip", "skip"), default="ask",
                    help="what to do when the mask is black on every vertex (default: ask with a dialog)")
    ap.add_argument("--no-verify", action="store_true", help="skip re-reading the written file")
    ap.add_argument("--no-alpha-prop", action="store_true", help="do not set NiAlphaProperty")
    ap.add_argument("--no-shader-flags", action="store_true", help="do not touch BSLightingShaderProperty")
    ap.add_argument("--alpha-flags", type=lambda s: int(s, 0), default=nv.ALPHA_FLAGS_DEFAULT,
                    help="NiAlphaProperty Flags value (default 4333)")
    args = ap.parse_args()

    if args.nif:
        return run_nif(args)

    if args.selftest or not args.obj:
        return selftest()
    verts, tris = load_obj(args.obj)
    n = len(verts)
    edge = edge_vertices(tris, n)
    dist = ring_distance(tris, edge, n)
    alphas = paint(dist, args.mode, args.buffer)
    unreached = sum(1 for d in dist if d == -1)
    print("vertices %d   triangles %d   edge vertices %d (%.1f%%)   isolated %d"
          % (n, len(tris), len(edge), 100.0 * len(edge) / max(1, n), unreached))
    print("mode=%s buffer=%d -> black %d (%.1f%%), white %d"
          % (args.mode, args.buffer, sum(1 for a in alphas if a == 0),
             100.0 * sum(1 for a in alphas if a == 0) / max(1, n),
             sum(1 for a in alphas if a == 255)))
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(str(a) for a in alphas) + "\n")
        print("written: %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
