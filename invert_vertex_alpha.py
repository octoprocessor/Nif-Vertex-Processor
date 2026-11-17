#!/usr/bin/env python
r"""invert_vertex_alpha.py -- invert the vertex-color alpha of BSTriShape blocks by name (a -> 255 - a).

READS : a .nif (or a folder of them) and the shape names you ask for.
WRITES: nothing by default. With --apply, a NEW .nif under --outdir; the source is never modified.

Why this exists: SNIFF's `Vertexcolorpainting` command (ProcVertexPaint.pas, modes Set / Adjust / Remove /
Replace) can adjust alpha with only MULTIPLY or ADD -- `if fAdjustMod = 0 then a := a * fAdjustA else
a := a + fAdjustA` -- so `a -> 1 - a` cannot be expressed. This tool does exactly that one thing.

What it follows from SNIFF: selection by name, and its default rule that a shape with no vertex colors is
left alone rather than given them (`chkAddIfMissing` is off in SNIFF's default `Vertexcolorpainting`
section). `--add-missing` opts into creating them, which is what `vertex_alpha_mask.py --apply` always does.

The NIF layout, the reader and the writer all live in nif_vertex.py -- see its docstring for the VertexDesc
tables, and for why an earlier version of this file wrongly reported that no shape had colors.

The property edits are not optional: after writing the alphas this tool also sets `NiAlphaProperty` to 4333
(creating the block when the shape has none) and applies the mask shader flags (`SLSF2 ZBuffer_Write` cleared;
`SLSF1 Decal`, `Dynamic_Decal`, `Vertex_Alpha` and `SLSF2 Assume_Shadowmask`, `Vertex_Colors` set), then
re-reads the file to confirm all of it. A vertex-alpha mask whose flags were never applied renders nothing and
looks identical on disk, which is why "set" and "confirmed" are separate steps. `--no-props` turns both off,
and then `--verify` stops checking them too, because there is nothing to confirm.

An all-black result is stopped before anything is written, the same way the mask tool stops one: inverting an
all-white alpha turns a surface invisible, which is worth a question rather than a guess. The dialog offers flip
to white, write it all-black, or skip; `--all-black flip|keep|skip` answers it ahead of time, and with nothing to
ask with the shape is skipped. Nothing is written until every shape's decision is known.

Safety, and how it is proved rather than asserted
-------------------------------------------------
  * A shape is only touched when its VertexDesc validates: the flag-implied stride, the VF1 field and every
    offset nibble must all agree with `dataSize / numVertices`. Otherwise it is refused, not guessed at.
  * An inverted alpha is written at `vertexStart + v*stride + colorOffset + 3`, one byte per vertex. When
    the shape already has colors nothing is resized, no array is rebuilt and no offset moves.
  * `--verify` (on by default) re-reads the written file and asserts that every alpha is exactly
    255 - the original, that positions/UVs/normals/tangents and the triangle list are byte-identical, and
    that shapes you did not select are untouched.

Usage
  python tools\invert_vertex_alpha.py <file.nif> --name "triLandscape*"                     # report
  python tools\invert_vertex_alpha.py <file.nif> --name "*roots*" --apply
  python tools\invert_vertex_alpha.py <folder> --name "*" --apply --recurse                 # a tree
"""
import argparse
import fnmatch
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nif_vertex as nv                                                     # noqa: E402



def report(nif, patterns):
    """One row per matching shape: (name, verts, tris, stride, state, detail)."""
    rows = []
    for sh in nif.shapes():
        if not any(fnmatch.fnmatchcase(sh.name.lower(), p) for p in patterns):
            continue
        if not sh.ok:
            rows.append((sh.name, sh.vert, sh.tri, sh.stride, "REFUSED", sh.reason))
            continue
        al = sh.alphas()
        if al is None:
            rows.append((sh.name, sh.vert, sh.tri, sh.stride, "no colors", "left alone (SNIFF's default)"))
            continue
        rows.append((sh.name, sh.vert, sh.tri, sh.stride, "invertible",
                     "%d black, %d white, %d mid" % (al.count(0), al.count(255),
                                                     len(al) - al.count(0) - al.count(255))))
    return rows


def main():
    ap = argparse.ArgumentParser(description="invert vertex-color alpha of BSTriShape blocks by name")
    ap.add_argument("path")
    ap.add_argument("--name", action="append", default=[],
                    help="shape-name pattern (wildcards, case-insensitive). Repeatable. Default: all shapes")
    ap.add_argument("--recurse", action="store_true", help="treat path as a folder and walk it")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--outdir", default="",
                    help="where --apply writes (default: beside the source, named <name>_inv.nif)")
    ap.add_argument("--no-open", action="store_true", help="do not open the result in NifSkope")
    ap.add_argument("--add-missing", action="store_true",
                    help="give shapes without vertex colors an opaque one before inverting (off, like SNIFF)")
    ap.add_argument("--no-props", action="store_true",
                    help="do NOT set NiAlphaProperty 4333 or the mask shader flags (they are set by default)")
    ap.add_argument("--alpha-flags", type=lambda s: int(s, 0), default=nv.ALPHA_FLAGS_DEFAULT)
    ap.add_argument("--no-verify", action="store_true", help="skip re-reading the written file")
    ap.add_argument("--all-black", choices=("ask", "flip", "keep", "skip"), default="ask",
                    help="what to do when the result is black on every vertex (default: ask with a dialog)")
    args = ap.parse_args()

    if os.path.isdir(args.path):
        if not args.recurse:
            raise SystemExit("%s is a folder - add --recurse" % args.path)
        files = []
        for dp, _, fns in os.walk(args.path):
            for f in fns:
                if f.lower().endswith(".nif"):
                    files.append(os.path.join(dp, f))
        root = os.path.abspath(args.path)
    else:
        files = [args.path]
        root = os.path.dirname(os.path.abspath(args.path))
    files.sort()
    if not files:
        raise SystemExit("no .nif files found")

    pats = [p.lower() for p in args.name] or ["*"]
    print("# invert_vertex_alpha  %d file(s)  %s" % (len(files), "APPLY" if args.apply else "report only"))
    print("# name patterns: %s" % ", ".join(pats))
    print("   %-26s %-24s %-6s %-6s %-7s %-11s %s"
          % ("file", "shape", "verts", "tris", "stride", "state", "detail"))
    tally = dict(files=0, chosen=0, written=0, refused=0, skipped=0)
    for path in files:
        try:
            with open(path, "rb") as fh:
                nif = nv.Nif(fh.read())
        except (nv.NifError, IOError) as exc:
            print("   %-26s %s" % (os.path.basename(path)[:26], exc))
            tally["refused"] += 1
            continue
        if not nif.is_sse:
            print("   %-26s not a Skyrim SE NIF (version 0x%X userVersion %d)"
                  % (os.path.basename(path)[:26], nif.version, nif.user_version))
            tally["refused"] += 1
            continue
        tally["files"] += 1
        for row in report(nif, pats):
            print("   %-26s %-24s %-6d %-6d %-7d %-11s %s"
                  % (os.path.basename(path)[:26], row[0][:24], row[1], row[2], row[3], row[4], row[5]))
            if row[4] == "invertible":
                tally["chosen"] += 1
            elif row[4] == "no colors":
                tally["skipped"] += 1
            else:
                tally["refused"] += 1
        if not args.apply:
            continue
        rel = os.path.relpath(path, root) if os.path.isdir(args.path) else ""
        probe, problem = nv.output_path_for(path, "_inv", args.outdir)
        if problem:
            print("      ! %s" % problem)
            tally["refused"] += 1
            continue
        out_path = os.path.join(os.path.dirname(probe), rel, os.path.basename(probe)) if rel else probe

        def alphas_for(sh, _pats=pats):
            if not any(fnmatch.fnmatchcase(sh.name.lower(), p) for p in _pats) or not sh.ok:
                return None
            cur = sh.alphas()
            if cur is None:
                if not args.add_missing:
                    return None
                cur = [255] * sh.vert          # no colors: nothing to invert, so start from opaque
            return [255 - a for a in cur]

        res = nv.apply_file(path, out_path, alphas_for, args.alpha_flags,
                            not args.no_props, not args.no_props, not args.no_verify, args.all_black)
        if res["ok"]:
            tally["written"] += 1
            print("      written %s (%d -> %d bytes, %d shape(s))"
                  % (out_path, res["src_bytes"], res["bytes"], len(res["records"])))
            if not args.no_open:
                opened = nv.open_in_nifskope(out_path)
                print("      opened in NifSkope: %s" % out_path if opened == "opened" else "      ! " + opened)
            for r in res["records"]:
                if r["alpha"]:
                    print("      alpha  %s" % r["alpha"])
                if r["shader"]:
                    print("      shader %s" % r["shader"])
            for n in res["notes"]:
                print("      confirmed %s" % n)
            if args.no_props:
                print("      ! --no-props: NiAlphaProperty and the mask shader flags were NOT touched")
        elif not res["records"]:
            print("      nothing selected (%s)" % "; ".join(res["problems"]))
        else:
            tally["refused"] += 1
            print("      FAILED: %s" % "; ".join(res["problems"]))
    print("")
    print("shapes invertible %d, files written %d, skipped (no colors) %d, refused %d"
          % (tally["chosen"], tally["written"], tally["skipped"], tally["refused"]))
    if args.apply:
        print("Sources untouched; the new files were listed above. Not verified: in-game appearance.")
    return 0 if tally["refused"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())