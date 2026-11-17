#!/usr/bin/env python
"""nif_weld_uv.py -- weld duplicate vertices inside one BSTriShape and recompute its UV map from geometry.

READS : a .nif (or a folder), the shape names you ask for, and the shape's own vertex data.
WRITES: nothing by default. With --apply, a NEW .nif under --outdir; the source is never modified.

What it is for
--------------
Landscape meshes carry vertices duplicated at the same point because a UV seam needs them, and their UVs are
often hand-placed, so the texture is stretched in some triangles and squashed in others. Welding gives one vertex
per point; re-projecting the UV map from the geometry then gives even texel density everywhere. The scale is chosen
from the ORIGINAL mapping so the texture keeps its apparent size -- that is the "close enough to not be obvious"
requirement, and it is measured, not guessed.

Measured on the shipped AIO (see _recon\\weld_probe.txt):

    MountainTrim01 triLandscape   3708 verts, 3204 distinct points (504 extra, 13.6%), L/T spread 6.7x
    dirtcliffs01 tri3DRoot02      7762 verts, 6350 distinct points (1412 extra, 18.2%), L/T spread 3431x
    dirtcliffs01 tri3DRoots01     1170 verts,  943 distinct points ( 227 extra, 19.4%), L/T spread 1780x

L/T is 3D edge length over UV edge length -- texels per unit. A perfectly even map has one value; the spread is
max/min over every triangle edge.

How the new UV map is built
---------------------------
  unwrap (default) unfold the triangles into flat space, preserving each one's edge lengths, then relax the result
         against those lengths so loop-closure error is spread out instead of concentrated. Islands are separated so
         two disconnected pieces never share UV coordinates.
  planar one plane for the whole shape (--axis x|y|z), for genuinely flat geometry.
  keep   weld only. Beware: welding destroys UV seams by design, so a seam vertex's neighbours keep one of two
         different UVs and the map tears across the atlas. Measured on triLandscape, `keep` took the L/T spread from
         6.75x to 321x. It exists for completeness, not for use.

Three approaches were measured and two rejected, which is why the default is what it is:
  * per-vertex dominant-axis projection -- adjacent vertices projected from different planes, collapsing UV edges:
    spread 6.75x -> 20454x on triLandscape;
  * per-vertex tangent-plane projection with a continuous frame -- isometric pointwise, but projecting absolute
    positions through a rotating frame multiplies large mesh coordinates by small frame rotations: spread -> 20454x
    and worse on the dirtcliffs;
  * unfolding + relaxation -- spread 6.75x -> 4.65x on triLandscape, and the 5-95% band, which is the fair summary,
    goes 199-333 -> 223-276 texels per unit. triRockSkirt: 205-240 -> 221-225.

The scale is then calibrated so the MEAN texel density equals the original's. That is the "keep the overall size
close enough not to be obvious" requirement, and it is exact rather than approximate: 247 -> 247 on triLandscape,
347 -> 347 on triMountainMask, 223 -> 223 on triRockSkirt. Triangles with no UV area are skipped when the original
density is derived, so one degenerate edge cannot drag the whole map.

Attributes other than UV, and what happens to them
--------------------------------------------------
  normal   averaged over the welded group and re-encoded (--normals average, the default) or taken from the first
           member (--normals first). Vertices welded together should end up sharing one normal.
  tangent  kept from the first member by default (--tangents keep). `--tangents derive` rebuilds it from the
           projection, which is exact for a planar map; it is not the default because the bitangent packing
           (bitangent X in the vertex's 4th float, Y in the normal's 4th byte, Z in the tangent's 4th byte) is a
           convention this tool has no independent way to verify here.
  colour   the first member wins, and the merge rule is not involved: this tool rewrites UVs, not alpha.
  skin     the first member wins; a welded group is expected to share its bone weights.
  triangles that collapse (two indices equal after the weld) are dropped and counted, never left in.

Usage
  python tools\\nif_weld_uv.py <file.nif>                              # report: what welding would do
  python tools\\nif_weld_uv.py <file.nif> --shape "triLandscape" --apply
  python tools\\nif_weld_uv.py <folder> --recurse --uv axis --apply    # a tree
  python tools\\nif_weld_uv.py --selftest
"""
import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nif_vertex as nv                                                     # noqa: E402

AXES = {"x": (1, 2), "y": (0, 2), "z": (0, 1)}          # projection axes -> which position components become U,V


# ---------------------------------------------------------------------------------------------
# geometry helpers -- pure, so the self-test can pin them down
# ---------------------------------------------------------------------------------------------
def read_vertices(sh):
    """Every vertex as a dict of decoded fields, plus its raw bytes."""
    blk = sh.nif.blocks[sh.index]
    d = sh.d
    vf = d["vf"]
    out = []
    for v in range(sh.vert):
        s = sh.vstart + v * sh.stride
        rec = dict(
            index=v,
            raw=bytes(blk[s:s + sh.stride]),
            pos=struct.unpack_from("<3f", blk, s) if vf & nv.VF_VERTEX else (0.0, 0.0, 0.0),
            uv=struct.unpack_from("<2e", blk, s + d["uv0"] * 4) if vf & nv.VF_UV else (0.0, 0.0),
            uv1=struct.unpack_from("<2e", blk, s + d["uv1"] * 4) if vf & nv.VF_UV_2 else None,
        )
        if vf & nv.VF_NORMAL:
            b = blk[s + d["normal"] * 4: s + d["normal"] * 4 + 3]
            rec["normal"] = tuple((x - 128) / 127.0 for x in b)
        else:
            rec["normal"] = (0.0, 0.0, 1.0)
        rec["color"] = (tuple(blk[s + sh.color_off: s + sh.color_off + 4]) if sh.has_colors else None)
        out.append(rec)
    return out


def weld(vertices, triangles, epsilon):
    """One vertex per distinct position. Returns (groups, remap, tris, dropped).

    `groups` is a list of lists of original indices, one per surviving vertex, in first-seen order; `remap` maps
    an original index to its new one; `dropped` counts triangles that collapsed onto fewer than three points.
    """
    key_to_new, groups, remap = {}, [], {}
    for v in vertices:
        p = v["pos"]
        key = (round(p[0] / epsilon), round(p[1] / epsilon), round(p[2] / epsilon))
        if key not in key_to_new:
            key_to_new[key] = len(groups)
            groups.append([])
        new = key_to_new[key]
        groups[new].append(v["index"])
        remap[v["index"]] = new
    tris, dropped = [], 0
    for tri in triangles:
        a, b, c = (remap[i] for i in tri)
        if a == b or b == c or a == c:
            dropped += 1
            continue
        tris.append((a, b, c))
    return groups, remap, tris, dropped


def dominant_axis(normal):
    """Which plane to project from: the axis the normal points along most strongly."""
    ax, ay, az = abs(normal[0]), abs(normal[1]), abs(normal[2])
    if ay >= ax and ay >= az:
        return "y"
    return "x" if ax >= az else "z"


def tri_area3d(p0, p1, p2):
    ux, uy, uz = p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2]
    vx, vy, vz = p2[0] - p0[0], p2[1] - p0[1], p2[2] - p0[2]
    cx, cy, cz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
    return 0.5 * (cx * cx + cy * cy + cz * cz) ** 0.5


def tri_area2d(a, b, c):
    return 0.5 * abs((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1]))


def original_uv_scale(positions, uvs, triangles):
    """UV units per world unit implied by the current mapping: sqrt(UV area / 3D area) over all triangles.

    This is the number that keeps the texture looking the same size after a re-projection. Triangles with no UV
    area are skipped rather than allowed to drag the average to zero.
    """
    uv_area = tri_area = 0.0
    for a, b, c in triangles:
        t = tri_area2d(uvs[a], uvs[b], uvs[c])
        if t <= 1e-12:
            continue
        uv_area += t
        tri_area += tri_area3d(positions[a], positions[b], positions[c])
    if tri_area <= 0 or uv_area <= 0:
        return None
    return (uv_area / tri_area) ** 0.5


def tangent_frame(n, ref=(0.0, 0.0, 1.0)):
    """Two orthonormal in-plane directions for a vertex normal -- the plane the surface lies in.

    Built from a FIXED reference vector, so the frame turns smoothly as the normal turns, which is what makes the
    UV field continuous between neighbours. A per-vertex *axis choice* does not (it jumps), and that is why an
    earlier mode of this tool produced degenerate UV edges and a worse spread than the map it replaced.
    """
    cx = ref[1] * n[2] - ref[2] * n[1]
    cy = ref[2] * n[0] - ref[0] * n[2]
    cz = ref[0] * n[1] - ref[1] * n[0]
    m = (cx * cx + cy * cy + cz * cz) ** 0.5
    if m < 1e-6:                                  # normal parallel to the reference: pick another
        ref = (1.0, 0.0, 0.0)
        cx = ref[1] * n[2] - ref[2] * n[1]
        cy = ref[2] * n[0] - ref[0] * n[2]
        cz = ref[0] * n[1] - ref[1] * n[0]
        m = (cx * cx + cy * cy + cz * cz) ** 0.5
        if m < 1e-6:
            return (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)
    u = (cx / m, cy / m, cz / m)
    v = (n[1] * u[2] - n[2] * u[1], n[2] * u[0] - n[0] * u[2], n[0] * u[1] - n[1] * u[0])
    return u, v


def project(positions, normals, mode, axis, scale, offset):
    """New UVs. mode is 'planar' (one axis) or 'tangent' (each vertex's own tangent plane)."""
    out = []
    for p, n in zip(positions, normals):
        if mode == "tangent":
            u_dir, v_dir = tangent_frame(n)
            u = p[0] * u_dir[0] + p[1] * u_dir[1] + p[2] * u_dir[2]
            v = p[0] * v_dir[0] + p[1] * v_dir[1] + p[2] * v_dir[2]
        else:
            i, j = AXES[axis]
            u, v = p[i], p[j]
        out.append((u * scale + offset[0], v * scale + offset[1]))
    return out


def auto_offset(new_uvs, old_uvs):
    """Shift the projection so its centroid sits where the original's did -- keeps alignment roughly intact."""
    if not new_uvs:
        return (0.0, 0.0)
    nu = sum(u for u, _ in new_uvs) / len(new_uvs)
    nv_ = sum(v for _, v in new_uvs) / len(new_uvs)
    ou = sum(u for u, _ in old_uvs) / len(old_uvs)
    ov = sum(v for _, v in old_uvs) / len(old_uvs)
    return (ou - nu, ov - nv_)


def _dist3(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


def _circle_intersect(p0, r0, p1, r1):
    """Both points at distance r0 from p0 and r1 from p1, in 2D. Returns [] or two points."""
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    d = (dx * dx + dy * dy) ** 0.5
    if d < 1e-12 or d > r0 + r1 or d < abs(r0 - r1):
        return []
    a = (r0 * r0 - r1 * r1 + d * d) / (2 * d)
    h2 = r0 * r0 - a * a
    h = h2 ** 0.5 if h2 > 0 else 0.0
    mx, my = p0[0] + a * dx / d, p0[1] + a * dy / d
    return [(mx + h * dy / d, my - h * dx / d), (mx - h * dy / d, my + h * dx / d)]


def unwrap(positions, triangles):
    """Unfold the triangles into UV space, preserving each triangle's edge lengths.

    This is what actually removes stretching. A single-axis projection smears every face that is not perpendicular
    to the axis, and projecting absolute positions through a per-vertex rotating frame multiplies large mesh
    coordinates by small frame rotations -- both were measured, and both made the spread worse than the map they
    replaced. Unfolding is exact per triangle: a neighbour is placed across the shared edge using its true side
    lengths, so L/T comes out near-constant.

    A curved surface cannot be flattened without error, so where the flood fill closes a loop the later triangles
    are fitted to vertices placed another way. The result tiles and overlaps, which is what landscape textures do
    anyway. Each disconnected island starts its own origin. Returns (uvs, island_count).

    The first version of this restarted the walk on every triangle it had not reached, and dropped each restart's
    first vertex at (0,0) -- 30 of 98 vertices ended up coincident and the measured spread was 800x. The structure
    below is one flood fill per island, which is why the count is reported rather than assumed.
    """
    from collections import deque
    n = len(positions)
    uv = [None] * n
    edge_tris = {}
    for ti, (a, b, c) in enumerate(triangles):
        for u, v in ((a, b), (b, c), (c, a)):
            edge_tris.setdefault((u < v and u or v, u < v and v or u), []).append(ti)
    done = [False] * len(triangles)

    def dist(u, v):
        return _dist3(positions[u], positions[v]) or 1e-6

    def third_corner_uv(ti, u, v):
        for nt in edge_tris.get((min(u, v), max(u, v)), []):
            if nt == ti:
                continue
            rest = [x for x in triangles[nt] if x not in (u, v)]
            if rest and uv[rest[0]] is not None:
                return uv[rest[0]]
        return None

    def place(ti):
        """Give every vertex of this triangle a UV if it does not have one. True if all three now have one."""
        a, b, c = triangles[ti]
        if uv[a] is None and uv[b] is None and uv[c] is None:
            uv[a] = (0.0, 0.0)
            uv[b] = (dist(a, b), 0.0)
            hits = _circle_intersect(uv[a], dist(a, c), uv[b], dist(b, c))
            if hits:
                uv[c] = hits[0] if hits[0][1] >= hits[1][1] else hits[1]
            else:
                uv[c] = (uv[a][0], uv[a][1] + dist(a, c))
            return True
        for u, v, w in ((a, b, c), (b, c, a), (c, a, b)):
            if uv[u] is None or uv[v] is None or uv[w] is not None:
                continue
            hits = _circle_intersect(uv[u], dist(u, w), uv[v], dist(v, w))
            if not hits:
                # inconsistent side lengths: the walk has closed a loop and the exact fit is impossible, so
                # place the vertex at the right distance from u along the edge normal rather than skipping it.
                # Skipping left vertices without a UV, which then all landed on (0,0) -- the degenerate edges
                # that made the first version's spread worse than the map it replaced.
                dx, dy = uv[v][0] - uv[u][0], uv[v][1] - uv[u][1]
                mag = (dx * dx + dy * dy) ** 0.5 or 1e-6
                uv[w] = (uv[u][0] - dy / mag * dist(u, w), uv[u][1] + dx / mag * dist(u, w))
                break
            other = third_corner_uv(ti, u, v)
            if other is None:
                uv[w] = hits[0]
            else:
                dx, dy = uv[v][0] - uv[u][0], uv[v][1] - uv[u][1]

                def side(pt):
                    return dx * (pt[1] - uv[u][1]) - dy * (pt[0] - uv[u][0])
                uv[w] = hits[0] if side(hits[0]) * side(other) < 0 else hits[1]
            break
        return all(uv[x] is not None for x in (a, b, c))

    islands = 0
    island_of = [None] * len(triangles)
    for seed in range(len(triangles)):
        if done[seed]:
            continue
        island_of[seed] = islands
        islands += 1
        place(seed)
        done[seed] = True
        queue = deque([seed])
        while queue:
            ti = queue.popleft()
            a, b, c = triangles[ti]
            for u, v in ((a, b), (b, c), (c, a)):
                for nt in edge_tris.get((min(u, v), max(u, v)), []):
                    if done[nt]:
                        continue
                    if place(nt):
                        done[nt] = True
                        island_of[nt] = islands - 1
                        queue.append(nt)
    for i in range(n):
        if uv[i] is None:
            uv[i] = (0.0, 0.0)

    # Push each island clear of the others. Without this every island starts at (0,0), so a vertex in island 2 and
    # a vertex in island 5 can share a UV exactly -- measured: 38 vertices on one point and an edge with 19 units of
    # 3D length and zero UV length. Separation keeps the tiles independent, which is what they are.
    member = [set() for _ in range(islands)]
    for ti, tri in enumerate(triangles):
        if island_of[ti] is not None:
            for v in tri:
                member[island_of[ti]].add(v)
    shift, y_max = 0.0, 0.0
    for isl in range(islands):
        if not member[isl]:
            continue
        us = [uv[v][0] for v in member[isl]]
        vs = [uv[v][1] for v in member[isl]]
        u0, u1, v0, v1 = min(us), max(us), min(vs), max(vs)
        for v in member[isl]:
            uv[v] = (uv[v][0] - u0 + shift, uv[v][1] - v0 + y_max)
        shift += (u1 - u0) + 0.05 * max(1.0, u1 - u0)
        y_max = max(y_max, (v1 - v0) * 1.0) if islands > 1 else y_max
    return uv, islands


def relax(uv, positions, triangles, iterations=1200, damping=0.9, tolerance=0.05, chunk=100):
    """Even out the flattening: pull every edge toward its true 3D length until the worst one is close.

    The flood fill is exact per triangle but cannot stay exact everywhere once it closes a loop, and the error shows
    up as a handful of edges far off their proper density. Edge-length relaxation is a cheap least-squares
    compromise: each pass moves an edge's endpoints toward the right separation, and the map is re-centred after each
    pass so it does not drift. Returns (uv, mean-and-worst error before, mean-and-worst error after).

    The pass count is a quality dial, measured on triDirtFace: 120 passes still left 6 edges over 10x, 400 left 4,
    and 1200 left none. So it runs in chunks and stops when the worst edge is within `tolerance`, or when a chunk
    buys less than 1% on the worst edge -- judging the stop on the MEAN error instead stopped after one chunk and
    left the same 612x spread that 120 passes gave, because the mean barely moves while the outliers converge.
    """
    edges = {}
    for a, b, c in triangles:
        for u, v in ((a, b), (b, c), (c, a)):
            key = (min(u, v), max(u, v))
            if key not in edges:
                edges[key] = _dist3(positions[u], positions[v]) or 1e-6
    items = list(edges.items())

    def mean_error():
        tot, worst = 0.0, 0.0
        for (u, v), L in items:
            d = ((uv[u][0] - uv[v][0]) ** 2 + (uv[u][1] - uv[v][1]) ** 2) ** 0.5
            e = abs(d - L) / L
            tot += e
            worst = max(worst, e)
        return tot / max(1, len(items)), worst

    before = mean_error()
    cu = sum(u for u, _ in uv) / max(1, len(uv))
    cv = sum(v for _, v in uv) / max(1, len(uv))
    done, prev_worst = 0, None
    while done < max(0, iterations):
        for _ in range(min(chunk, max(0, iterations) - done)):
            done += 1
            for (u, v), L in items:
                du, dv = uv[v][0] - uv[u][0], uv[v][1] - uv[u][1]
                d = (du * du + dv * dv) ** 0.5
                if d < 1e-12:
                    continue
                corr = 0.5 * damping * (d - L) / d
                uv[u] = (uv[u][0] + du * corr, uv[u][1] + dv * corr)
                uv[v] = (uv[v][0] - du * corr, uv[v][1] - dv * corr)
            nu = sum(u for u, _ in uv) / max(1, len(uv))
            nvv = sum(v for _, v in uv) / max(1, len(uv))
            du, dv = cu - nu, cv - nvv
            uv = [(u + du, v + dv) for u, v in uv]
        _err, worst = mean_error()
        if worst <= tolerance:
            break
        if prev_worst is not None and worst > prev_worst * 0.99:
            break
        prev_worst = worst
    return uv, before, mean_error()


def stretch_stats(positions, uvs, triangles):
    """L/T over every triangle edge: mean, 5th/95th percentile, worst, and max/min spread."""
    ratios = []
    for a, b, c in triangles:
        for u, v in ((a, b), (b, c), (c, a)):
            L = ((positions[u][0] - positions[v][0]) ** 2 + (positions[u][1] - positions[v][1]) ** 2
                 + (positions[u][2] - positions[v][2]) ** 2) ** 0.5
            T = ((uvs[u][0] - uvs[v][0]) ** 2 + (uvs[u][1] - uvs[v][1]) ** 2) ** 0.5
            if L > 1e-6 and T > 1e-9:
                ratios.append(L / T)
    if not ratios:
        return None
    ratios.sort()
    n = len(ratios)
    return dict(edges=n, mean=sum(ratios) / n, p05=ratios[int(0.05 * (n - 1))],
                p95=ratios[int(0.95 * (n - 1))], worst=ratios[-1],
                best=ratios[0], spread=ratios[-1] / max(ratios[0], 1e-12))


def encode_byte3(v):
    """A unit vector as the three bytes Skyrim stores, with the convention this module decodes."""
    return bytes(max(0, min(255, int(round(x * 127.0 + 128.0)))) for x in v)


def _median(values):
    """Middle value; for an even count, the mean of the two middle ones, rounded. Bytes stay bytes."""
    v = sorted(values)
    n = len(v)
    if not n:
        return 0
    if n % 2:
        return v[n // 2]
    return int(round((v[n // 2 - 1] + v[n // 2]) / 2.0))


def merge_colors(colors, color_mode="mean", alpha_mode="median"):
    """BGRA for one welded group: the colour channels averaged, the ALPHA by median unless told otherwise.

    Alpha is the channel that matters and the channel that gets outliers -- one vertex left at 0 in the middle of a
    group of 255s means "this point is covered", and a mean would turn it into 254 and quietly undo a mask. The
    median survives that, which is why it is the default. `mean`, `min`, `max` and `first` are all available, and
    `color_mode` offers the same choices for B/G/R (default `mean`).
    """
    colors = [c for c in colors if c is not None]
    if not colors:
        return None
    if len(colors) == 1:
        return tuple(colors[0])

    def pick(channel, mode):
        vals = [c[channel] for c in colors]
        if mode == "first":
            return vals[0]
        if mode == "median":
            return _median(vals)
        if mode == "min":
            return min(vals)
        if mode == "max":
            return max(vals)
        return int(round(sum(vals) / float(len(vals))))

    return (pick(0, color_mode), pick(1, color_mode), pick(2, color_mode), pick(3, alpha_mode))


def normalise(v):
    n = (v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) ** 0.5
    return (v[0] / n, v[1] / n, v[2] / n) if n > 1e-12 else (0.0, 0.0, 1.0)


def average_normals(norms):
    return normalise((sum(n[0] for n in norms), sum(n[1] for n in norms), sum(n[2] for n in norms)))


# ---------------------------------------------------------------------------------------------
# one shape
# ---------------------------------------------------------------------------------------------
def plan_shape(sh, epsilon=1e-4, uv_mode="unwrap", axis="y", scale=None, offset=None,
               normals="average", tangents="keep", relax_iterations=1200,
               color_mode="mean", alpha_mode="median"):
    """Work out the welded shape without touching the file. Returns a dict describing the result."""
    if not sh.ok:
        return dict(ok=False, shape=sh.name, reason=sh.reason)
    vertices = read_vertices(sh)
    positions = [v["pos"] for v in vertices]
    uvs = [v["uv"] for v in vertices]
    normals = [v["normal"] for v in vertices]
    triangles = sh.triangles()
    groups, remap, tris, dropped = weld(vertices, triangles, epsilon)
    new_pos = [positions[g[0]] for g in groups]
    new_color = [merge_colors([vertices[i].get("color") for i in g], color_mode, alpha_mode) for g in groups]
    new_norm = ([average_normals([normals[i] for i in g]) if normals == "average" else normals[g[0]]
                 for g in groups])
    new_norm = list(new_norm)
    before = stretch_stats(positions, uvs, triangles)
    derived = None
    if uv_mode == "keep":
        new_uv = [uvs[g[0]] for g in groups]
        used_scale, used_offset = None, (0.0, 0.0)
    else:
        derived = original_uv_scale(positions, uvs, triangles)
        used_scale = scale if scale is not None else (derived if derived else 1.0)
        relax_before = relax_after = None
        if uv_mode == "unwrap":
            raw_uv, unreached = unwrap(new_pos, tris)
            if relax_iterations:
                raw_uv, relax_before, relax_after = relax(raw_uv, new_pos, tris, relax_iterations)
            base = [(u * used_scale, v * used_scale) for u, v in raw_uv]
        else:
            base = project(new_pos, new_norm, uv_mode, axis, used_scale, (0.0, 0.0))
        # Calibrate the SIZE, which is the "close enough not to be obvious" requirement: scale the finished map so
        # its mean texel density equals the original's. Deriving the scale from UV area against 3D area gets close,
        # but unwrapping changes the edge-length distribution, and measured it landed 13% off. This makes the
        # apparent texture size match by construction, and the figure is reported.
        calibration = 1.0
        if scale is None and before and before.get("mean"):
            achieved = stretch_stats(new_pos, base, tris)
            if achieved and achieved["mean"] > 0:
                calibration = achieved["mean"] / before["mean"]
                base = [(u * calibration, v * calibration) for u, v in base]
        if offset is None:
            used_offset = auto_offset(base, [uvs[g[0]] for g in groups])
        else:
            used_offset = offset
        new_uv = [(u + used_offset[0], v + used_offset[1]) for u, v in base]
    after = stretch_stats(new_pos, new_uv, tris)
    return dict(ok=True, shape=sh.name, index=sh.index, type=sh.type, stride=sh.stride, unreached=locals().get("unreached", 0),
                verts=sh.vert, unique=len(groups), merged=sh.vert - len(groups),
                tris=len(triangles), tris_after=len(tris), dropped=dropped,
                groups=groups, remap=remap, triangles=tris,
                positions=new_pos, normals=new_norm, uvs=new_uv, colors=new_color,
                old_uvs=[uvs[g[0]] for g in groups],
                scale=used_scale, derived_scale=derived, offset=used_offset,
                calibration=locals().get("calibration", 1.0),
                uv_mode=uv_mode, axis=axis, before=before, after=after,
                relax_before=locals().get("relax_before"), relax_after=locals().get("relax_after"),
                uv_range=(min((u for u, _ in new_uv), default=0.0), max((u for u, _ in new_uv), default=0.0),
                          min((v for _, v in new_uv), default=0.0), max((v for _, v in new_uv), default=0.0)),
                reason="")


def build_vertex_bytes(sh, plan, vertices, normals="average", tangents="keep"):
    """The new vertex array: one stride-sized record per welded group, with UV (and maybe normal) patched."""
    d = sh.d
    vf = d["vf"]
    out = []
    for g in plan["groups"]:
        first = vertices[g[0]]
        raw = bytearray(first["raw"])
        if vf & nv.VF_UV:
            u, v = plan["uvs"][len(out)]
            struct.pack_into("<2e", raw, d["uv0"] * 4, u, v)
        if sh.has_colors and plan.get("colors") and plan["colors"][len(out)] is not None:
            raw[sh.color_off: sh.color_off + 4] = bytes(bytearray(plan["colors"][len(out)]))
        if normals == "average" and (vf & nv.VF_NORMAL):
            raw[d["normal"] * 4: d["normal"] * 4 + 3] = encode_byte3(plan["normals"][len(out)])
        if tangents == "derive" and (vf & nv.VF_NORMAL) and (vf & nv.VF_TANGENT):
            n = plan["normals"][len(out)]
            if plan["uv_mode"] == "planar":
                i, j = AXES[plan["axis"]]
                t = [0.0, 0.0, 0.0]
                t[i] = 1.0
            else:
                t = [0.0, 0.0, 1.0]      # an unwrapped map has no single tangent direction to derive from
            raw[d["tangent"] * 4: d["tangent"] * 4 + 3] = encode_byte3(t)
        out.append(bytes(raw))
    return out


def apply_shape(sh, plan, vertices, normals="average", tangents="keep"):
    """Rewrite the shape in the in-memory file. Returns a record of what changed."""
    verts = build_vertex_bytes(sh, plan, vertices, normals, tangents)
    nv.replace_vertex_data(sh, verts, plan["triangles"])
    return dict(shape=plan["shape"], verts_before=plan["verts"], verts_after=plan["unique"],
                merged=plan["merged"], tris_before=plan["tris"], tris_after=plan["tris_after"],
                dropped=plan["dropped"], scale=plan["scale"], uv_mode=plan["uv_mode"],
                before=plan["before"], after=plan["after"])


# ---------------------------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------------------------
def selftest():
    ok = True

    def check(label, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print("   %-58s %s" % (label, "ok" if good else "FAIL got=%r want=%r" % (got, want)))

    def near(label, got, want, tol=1e-3):
        nonlocal ok
        good = abs(got - want) <= tol
        ok = ok and good
        print("   %-58s %s" % (label, "ok" if good else "FAIL got=%r want=%r" % (got, want)))

    # a square split into two triangles with the shared edge duplicated, as a UV seam does
    pos = [(0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 0), (1, 0, 1), (0, 0, 1)]
    uvs = [(0, 0), (1, 0), (1, 1), (0, 0), (1, 1), (0, 1)]
    tris = [(0, 1, 2), (3, 4, 5)]
    verts = [dict(index=i, pos=pos[i], uv=uvs[i], normal=(0, 1, 0), raw=b"") for i in range(6)]
    groups, remap, wtris, dropped = weld(verts, tris, 1e-4)
    check("weld: 6 vertices at 4 distinct points", len(groups), 4)
    check("weld: nothing collapsed", dropped, 0)
    check("weld: both triangles survive", len(wtris), 2)
    check("weld: shared corner mapped to one vertex", remap[0], remap[3])
    check("weld: diagonal kept distinct", remap[2] == remap[4], True)

    # everything at one point collapses to nothing but vertices; triangles must be dropped, not mangled
    sq = [dict(index=i, pos=(0, 0, 0), uv=(0, 0), normal=(0, 1, 0), raw=b"") for i in range(3)]
    g2, r2, t2, d2 = weld(sq, [(0, 1, 2)], 1e-4)
    check("weld: fully coincident triangle is dropped", (len(g2), len(t2), d2), (1, 0, 1))

    # projection: a 2 x 3 meter ground plane, scale 0.5 UV per meter
    p = [(0, 0, 0), (2, 0, 0), (2, 0, 3), (0, 0, 3)]
    n = [(0, 1, 0)] * 4
    out = project(p, n, "planar", "y", 0.5, (0.0, 0.0))
    check("project planar y: u spans 0..1, v spans 0..1.5", (out[0], out[2]), ((0.0, 0.0), (1.0, 1.5)))
    out2 = project(p, n, "axis", "y", 0.5, (0.0, 0.0))
    check("project axis on ground uses the same plane", out2, out)

    # a steep face: its normal points along X. The tangent-plane mode must measure along Y and Z there, not read
    # X (which is constant on the face and would collapse the whole surface to a line).
    steep = [(0, 0, 0), (0, 2, 0), (0, 2, 3), (0, 0, 3)]
    sn = [(1, 0, 0)] * 4
    check("dominant axis of an X-facing normal is x", dominant_axis((1, 0, 0)), "x")
    outp = project(steep, sn, "tangent", "y", 1.0, (0.0, 0.0))
    check("tangent mode maps a steep X face from YZ", outp[1], (2.0, 0.0))
    check("tangent mode: the far corner is (Y, Z)", outp[2], (2.0, 3.0))

    # the point of the tangent plane: a 45-degree slope keeps its surface length, which a top-down map cannot do
    slope = [(0.0, 0.0, 0.0), (0.0, 1.0, 1.0)]
    n45 = normalise((1.0, 0.0, 0.0))
    su = project(slope, [n45, n45], "tangent", "y", 1.0, (0.0, 0.0))
    len3d = (1.0 ** 2 + 1.0 ** 2) ** 0.5
    lenuv = ((su[1][0] - su[0][0]) ** 2 + (su[1][1] - su[0][1]) ** 2) ** 0.5
    near("tangent mode is isometric on a 45-degree slope", lenuv, len3d, 1e-9)
    topdown = project(slope, [n45, n45], "planar", "y", 1.0, (0.0, 0.0))
    lenplan = ((topdown[1][0] - topdown[0][0]) ** 2 + (topdown[1][1] - topdown[0][1]) ** 2) ** 0.5
    near("a top-down map loses that slope's steepness (1.0 of 1.414)", lenplan, 1.0, 1e-9)

    # the frame must be orthonormal and continuous: neighbouring normals give neighbouring frames
    u1, v1 = tangent_frame(normalise((0.0, 1.0, 0.0)))
    u2, v2 = tangent_frame(normalise((0.1, 0.99, 0.0)))
    near("tangent frame u is unit length", (u1[0] ** 2 + u1[1] ** 2 + u1[2] ** 2) ** 0.5, 1.0, 1e-9)
    near("tangent frame v is unit length", (v1[0] ** 2 + v1[1] ** 2 + v1[2] ** 2) ** 0.5, 1.0, 1e-9)
    near("tangent frame is orthogonal", u1[0] * v1[0] + u1[1] * v1[1] + u1[2] * v1[2], 0.0, 1e-9)
    near("a small normal change barely moves the frame", (u1[0] - u2[0]) ** 2 + (u1[2] - u2[2]) ** 2, 0.0, 0.01)

    # scale recovery: a mesh already mapped at 0.25 UV per meter must report 0.25
    p3 = [(0, 0, 0), (4, 0, 0), (0, 0, 4), (4, 0, 4)]
    uv3 = [(u * 0.25, v * 0.25) for u, v in ((0, 0), (4, 0), (0, 4), (4, 4))]
    t3 = [(0, 1, 2), (1, 3, 2)]
    recovered = original_uv_scale(p3, uv3, t3)
    near("original_uv_scale recovers 0.25 from a box map", recovered, 0.25, 1e-6)

    # a 4 m square mapped into 1x1 is stretched EVERYWHERE -- so the spread is 1 and the mean carries it.
    # My first version of this check expected a spread of 4 and was wrong: spread measures unevenness, not scale.
    uv_stretch = [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)]
    before = stretch_stats(p3, uv_stretch, t3)
    near("uniform stretch: spread is 1, the mean is 4 texels per unit", before["spread"], 1.0, 1e-6)
    near("uniform stretch: mean L/T is 4", before["mean"], 4.0, 1e-6)

    # squeezing only U is what "stretched in one direction" actually looks like: spread 2
    uv_squash = [(0.0, 0.0), (1.0, 0.0), (0.0, 0.5), (1.0, 0.5)]
    uneven = stretch_stats(p3, uv_squash, t3)
    near("uneven map reports a spread of 2x", uneven["spread"], 2.0, 1e-6)
    s = original_uv_scale(p3, uv_stretch, t3)
    fixed = project(p3, [(0, 1, 0)] * 4, "planar", "y", s, (0.0, 0.0))
    after = stretch_stats(p3, fixed, t3)
    near("re-projection collapses the spread to 1x", after["spread"], 1.0, 1e-9)
    near("re-projection preserves the mean texel density", after["mean"], before["mean"], 1e-6)

    # auto offset keeps the map where it was
    off = auto_offset(project(p3, [(0, 1, 0)] * 4, "planar", "y", s, (0.0, 0.0)),
                      [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)])
    moved = project(p3, [(0, 1, 0)] * 4, "planar", "y", s, off)
    near("auto offset centers the new map on the old one", sum(u for u, _ in moved) / 4,
         sum(u for u, _ in uv_stretch) / 4, 1e-9)

    # color merge: mean for B/G/R, median for alpha, and the median must survive an outlier
    one = merge_colors([(200, 100, 50, 255)])
    check("merge_colors: a single vertex passes through", one, (200, 100, 50, 255))
    plain = merge_colors([(200, 100, 50, 255), (220, 120, 70, 255), (210, 110, 60, 255)])
    check("merge_colors: color channels are averaged", plain[:3], (210, 110, 60))
    out = merge_colors([(200, 100, 50, 255), (200, 100, 50, 255), (200, 100, 50, 0)])
    check("merge_colors: one 0 among 255s keeps alpha at 255 by median", out[3], 255)
    check("merge_colors: the mean WOULD have dropped it (the reason for the median)",
          int(round((255 + 255 + 0) / 3.0)), 170)
    check("merge_colors: even count takes the middle pair", merge_colors(
        [(1, 1, 1, 10), (1, 1, 1, 20), (1, 1, 1, 200), (1, 1, 1, 210)])[3], 110)
    check("merge_colors: alpha mean is available on request", merge_colors(
        [(1, 1, 1, 255), (1, 1, 1, 255), (1, 1, 1, 0)], alpha_mode="mean")[3], 170)
    check("merge_colors: alpha min / max", (merge_colors([(1, 1, 1, 9), (1, 1, 1, 200)], alpha_mode="min")[3],
                                             merge_colors([(1, 1, 1, 9), (1, 1, 1, 200)], alpha_mode="max")[3]),
          (9, 200))
    check("merge_colors: color channels can be median too", merge_colors(
        [(0, 0, 0, 1), (255, 255, 255, 1), (10, 10, 10, 1)], color_mode="median")[:3], (10, 10, 10))
    check("merge_colors: no colors at all", merge_colors([None, None]), None)

    # normal averaging
    avg = average_normals([(0, 1, 0), (1, 0, 0)])
    near("average of +Y and +X normalizes to 0.707", avg[0], 0.7071, 1e-3)
    check("a zero-sum normal set falls back to +Z", average_normals([(0, 1, 0), (0, -1, 0)]), (0.0, 0.0, 1.0))

    # byte encoding round-trips through the decoder this project uses
    enc = encode_byte3((0.0, 0.0, 1.0))
    check("byte3 encode of +Z", enc, bytes([128, 128, 255]))
    dec = tuple((x - 128) / 127.0 for x in enc)
    near("byte3 round-trip keeps +Z", dec[2], 1.0, 1e-6)

    print("\nRESULT: %s" % ("all checks passed" if ok else "FAILURES above"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="weld duplicate vertices and recompute the UV map of a BSTriShape")
    ap.add_argument("path", nargs="?", default="")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--shape", action="append", default=[], help="shape-name pattern (wildcards, repeatable)")
    ap.add_argument("--recurse", action="store_true", help="treat path as a folder")
    ap.add_argument("--epsilon", type=float, default=1e-4, help="positions closer than this are the same point")
    ap.add_argument("--uv", choices=("unwrap", "planar", "keep"), default="unwrap")
    ap.add_argument("--axis", choices=("x", "y", "z"), default="y", help="plane for --uv planar")
    ap.add_argument("--scale", default="auto", help="UV per world unit, or 'auto' to keep the original density")
    ap.add_argument("--offset", default="auto", help="'auto', or u,v")
    ap.add_argument("--normals", choices=("average", "first"), default="average")
    ap.add_argument("--tangents", choices=("keep", "derive"), default="keep")
    ap.add_argument("--relax", type=int, default=1200,
                    help="maximum flattening passes; it stops early once the worst edge is within 5%% (0 to skip)")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--outdir", default="",
                    help="where --apply writes (default: beside the source, named <name>_weld.nif)")
    ap.add_argument("--no-open", action="store_true", help="do not open the result in NifSkope")
    ap.add_argument("--config", default="",
                    help="per-shape plan: one shape per line, settings as key=value (uv, epsilon, relax, normals, "
                         "tangents, color_merge, alpha_merge). Shapes not listed are left alone")
    ap.add_argument("--write-config", default="",
                    help="write a starter plan listing every shape, then exit (no file is modified)")
    ap.add_argument("--color-merge", choices=("mean", "median", "min", "max", "first"), default="mean",
                    help="how welded vertices' B/G/R channels are combined (default mean)")
    ap.add_argument("--alpha-merge", choices=("median", "mean", "min", "max", "first"), default="median",
                    help="how welded vertices' alpha is combined (default median, which ignores outliers)")
    args = ap.parse_args()

    if args.selftest or not args.path:
        return selftest()
    return run(args)


def run(args):
    import fnmatch
    if os.path.isdir(args.path):
        if not args.recurse:
            raise SystemExit("%s is a folder - add --recurse" % args.path)
        files = []
        for dp, _, fns in os.walk(args.path):
            for f in fns:
                if f.lower().endswith(".nif"):
                    files.append(os.path.join(dp, f))
        files.sort()
        root = os.path.abspath(args.path)
    else:
        files, root = [args.path], os.path.dirname(os.path.abspath(args.path))
    pats = [p.lower() for p in args.shape] or ["*"]
    plan_rows, plan_problems = ([], [])
    if args.config:
        plan_rows, plan_problems = nv.load_shape_plan(args.config)
        for prob in plan_problems:
            print("# plan: %s" % prob)
        if args.shape:
            print("# --config is given, so --shape is ignored: the plan decides which shapes are touched")
    scale = None
    if args.scale not in ("auto", ""):
        scale = float(args.scale)
    offset = None
    if args.offset not in ("auto", "") and "," in args.offset:
        a, b = args.offset.split(",", 1)
        offset = (float(a), float(b))

    print("# weld + reproject  %d file(s)  uv=%s axis=%s scale=%s eps=%g  %s"
          % (len(files), args.uv, args.axis, args.scale, args.epsilon, "APPLY" if args.apply else "report only"))
    print("# shape patterns: %s" % ", ".join(pats))
    print("   %-26s %-20s %-6s %-6s %-7s %-8s %s"
          % ("file", "shape", "verts", "unique", "tris", "dropped", "L/T spread, 5-95% band, mean"))
    tally = dict(files=0, shapes=0, written=0, merged=0, problems=0)
    for path in files:
        base = os.path.basename(path)[:26]
        try:
            with open(path, "rb") as fh:
                nif = nv.Nif(fh.read())
        except (nv.NifError, IOError) as exc:
            print("   %-26s %s" % (base, exc))
            tally["problems"] += 1
            continue
        if not nif.is_sse:
            print("   %-26s not a Skyrim SE NIF" % base)
            tally["problems"] += 1
            continue
        tally["files"] += 1
        if args.write_config:
            nv.write_shape_plan(args.write_config, nif.shapes(),
                                "uv=%s epsilon=%g relax=%d color_merge=%s alpha_merge=%s"
                                % (args.uv, args.epsilon, args.relax, args.color_merge, args.alpha_merge))
            print("# wrote a starter plan: %s" % args.write_config)
            continue
        picked = 0
        for sh in nif.shapes():
            if plan_rows:
                settings = nv.plan_for(sh.name, plan_rows)
                if settings is None or settings.get("skip") or settings.get("keep") is True:
                    continue
            elif not any(fnmatch.fnmatchcase(sh.name.lower(), p) for p in pats):
                continue
            else:
                settings = {}
            tally["shapes"] += 1

            def pick(key, default, cast=str):
                try:
                    return cast(settings.get(key, default))
                except (TypeError, ValueError):
                    return default
            plan = plan_shape(sh, pick("epsilon", args.epsilon, float), pick("uv", args.uv),
                              pick("axis", args.axis), scale, offset, pick("normals", args.normals),
                              pick("tangents", args.tangents), pick("relax", args.relax, int),
                              pick("color_merge", args.color_merge), pick("alpha_merge", args.alpha_merge))
            if not plan["ok"]:
                print("   %-26s %-20s %s" % (base, sh.name[:20], plan["reason"]))
                tally["problems"] += 1
                continue
            b, a = plan["before"], plan["after"]
            fmt = ("%.2fx -> %.2fx   band %.0f-%.0f -> %.0f-%.0f   mean %.0f -> %.0f"
                   % (b["spread"] if b else -1, a["spread"] if a else -1,
                      b["p05"] if b else 0, b["p95"] if b else 0,
                      a["p05"] if a else 0, a["p95"] if a else 0,
                      b["mean"] if b else 0, a["mean"] if a else 0))
            print("   %-26s %-20s %-6d %-6d %-7d %-8d %s"
                  % (base, sh.name[:20], plan["verts"], plan["unique"], plan["tris_after"],
                     plan["dropped"], fmt))
            picked += 1
            tally["merged"] += plan["merged"]
            if not args.apply:
                continue
            if sh.type != "BSTriShape":
                print("        ! %s is a %s - segment data would need remapping, refusing" % (sh.name, sh.type))
                tally["problems"] += 1
                continue
            vertices = read_vertices(sh)
            rec = apply_shape(sh, plan, vertices, args.normals, args.tangents)
            scale_txt = ("%.4f" % rec["scale"]) if rec["scale"] is not None else "n/a (uv kept)"
            print("        %d verts -> %d (%d merged, %d triangles dropped), scale %s, offset (%.3f, %.3f)%s"
                  % (rec["verts_before"], rec["verts_after"], rec["merged"], rec["dropped"],
                     scale_txt, plan["offset"][0], plan["offset"][1],
                     (", colors %s / alpha %s" % (pick("color_merge", args.color_merge),
                                                  pick("alpha_merge", args.alpha_merge))) if plan.get("colors") else ""))
        if args.apply and picked:
            rel = os.path.relpath(path, root) if os.path.isdir(args.path) else ""
            probe, problem = nv.output_path_for(path, "_weld", args.outdir)
            if problem:
                print("        ! %s" % problem)
                tally["problems"] += 1
                continue
            out_path = os.path.join(os.path.dirname(probe), rel, os.path.basename(probe)) if rel else probe
            d = os.path.dirname(os.path.abspath(out_path))
            if d and not os.path.isdir(d):
                os.makedirs(d)
            nif.save(out_path)
            tally["written"] += 1
            print("        written %s" % out_path)
            if not args.no_open:
                opened = nv.open_in_nifskope(out_path)
                print("        opened in NifSkope: %s" % out_path if opened == "opened" else "        ! " + opened)
    print("")
    print("shapes %d, files written %d, vertices merged %d, problems %d"
          % (tally["shapes"], tally["written"], tally["merged"], tally["problems"]))
    if not args.apply:
        print("Report only: no NIF was written. Add --apply to write <name>_weld.nif beside each source.")
    return 0 if tally["problems"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
