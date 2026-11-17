#!/usr/bin/env python
"""nif_studio_math_test.py -- headless self-test for the colour/alpha math and the vertex brush in
`nif_studio.py`.

READS : the .nif given with --mesh (default: the staged AIO MountainTrim01), plus `nif_studio.py` itself.
WRITES: nothing but `_recon\\nif_studio_math_test.txt` (a log) and a throwaway edit log in `_recon\\`.

Why a fake instead of the window: the panel's own methods are what need testing, and every one of them reads no Tk
widget except the three variables holding the target / operation / value. Those are replaced with tiny stand-ins, so
`apply_math`, `math_channel`, `_brush_literal`, `_paint_at`, `_snapshot`, `do_undo` and `record` all run as written.

The cases worth having are the ones that were actually wrong before:
  * multiply past 255 must clamp, not wrap (200 * 2 used to become 44)
  * subtract past 0 must clamp
  * a shape with no VF_COLORS must gain one with a defined starting colour
  * the brush must touch only the vertices inside its radius, and only once per stroke
  * one stroke = one log entry = one undo step
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import nif_studio as gui                                                     # noqa: E402
import nif_vertex as nv                                                      # noqa: E402


class FakeVar(object):
    """Stands in for a tkinter StringVar / BooleanVar."""

    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class FakeStatus(object):
    def __init__(self):
        self.text = ""

    def configure(self, **kwargs):
        if "text" in kwargs:
            self.text = kwargs["text"]


def make_app(path, picks, log_path):
    """An App with everything the math and brush code reads, and no widgets at all."""
    app = gui.App.__new__(gui.App)
    with open(path, "rb") as fh:
        raw = fh.read()
    app.path = os.path.abspath(path)
    app.nif = nv.Nif(raw)
    app.pristine = nv.Nif(raw)
    app.pristine_bytes = raw
    app.log_path = log_path
    app.backup_path = log_path + ".bak"
    app.history = []
    app.undo = []
    app.redo = []
    app.staged = {}
    app.mesh_cache = {}
    app.ring_cache = {}
    app.status = FakeStatus()
    app.math_target = FakeVar(gui.MATH_TARGETS[0][0])
    app.math_op = FakeVar(gui.MATH_OPS[0][0])
    app.math_value = FakeVar("255")
    app.paint_value = FakeVar("")
    app.paint_on = FakeVar(True)
    app._paint_last = None
    app._paint_entries = []
    app._paint_done = set()
    app._paint_bpv = 1
    app._screen = []
    app.all = list(app.nif.shapes())
    usable = [s.index for s in app.all if s.ok]
    app._picks = usable[:picks] if picks else list(usable)

    def chosen():
        return [s for s in app.all if s.index in app._picks and s.ok]

    def reload_shapes():
        app.all = list(app.nif.shapes())
        app.mesh_cache.clear()
        app.ring_cache.clear()

    app.chosen = chosen
    app.reload_shapes = reload_shapes
    return app


def alphas_of(app, index):
    sh = [s for s in app.all if s.index == index][0]
    cols = sh.colors()
    return None if cols is None else [c[3] for c in cols]


def colours_of(app, index):
    return [s for s in app.all if s.index == index][0].colors()


def run(path, log_path):
    checks = []

    def check(name, condition, detail=""):
        checks.append((name, bool(condition), detail))

    app = make_app(path, 1, log_path)
    names = ["%d:%s" % (s.index, s.name) for s in app.all if s.ok][:4]
    first = next((s for s in app.all if s.ok and s.colors() is not None), None)
    check("the mesh really has vertex colors to work on", first is not None,
          "no painted shape in %s" % os.path.basename(path))
    if first is None:
        return checks, names, app
    app._picks = [first.index]
    base = alphas_of(app, first.index)

    # -- math_channel: the arithmetic itself ----------------------------------------------------
    cases = [
        (("multiply by the value", "2"), 200, 255, "multiply past 255 clamps"),
        (("multiply by the value", "0.5"), 200, 100, "multiply rounds"),
        (("add the value", "100"), 200, 255, "add past 255 clamps"),
        (("subtract the value", "100"), 40, 0, "subtract past 0 clamps"),
        (("floor - raise anything below it", "77"), 10, 77, "floor raises a low value"),
        (("floor - raise anything below it", "77"), 200, 200, "floor leaves a high value"),
        (("ceiling - lower anything above it", "100"), 200, 100, "ceiling lowers a high value"),
        (("ceiling - lower anything above it", "100"), 10, 10, "ceiling leaves a low value"),
        (("multiply by the value", "1"), 0, 0, "multiply by 1 is not the old wrap"),
    ]
    for (op, value), old, want, label in cases:
        app.math_target.set("alpha only")
        app.math_op.set(op)
        app.math_value.set(value)
        got = app.math_channel(old)
        check(label, got == want, "%s with %s on %d -> %d (wanted %d)" % (op, value, old, got, want))

    # -- _brush_literal ------------------------------------------------------------------------
    app.paint_value.set("")
    check("an empty brush falls back to the math operation", app._brush_literal() is None)
    for text, want in (("300", 255), ("-5", 0), ("128", 128), ("abc", None), ("127.6", 128)):
        app.paint_value.set(text)
        check("brush literal %r -> %s" % (text, want), app._brush_literal() == want,
              "got %r" % (app._brush_literal(),))
    app.paint_value.set("")

    # -- apply_math: alpha only, then undo ----------------------------------------------------
    original = list(base)
    original_cols = colours_of(app, first.index)
    log_before = len(app.history)
    app.math_target.set("alpha only")
    app.math_op.set("subtract the value")
    app.math_value.set("255")
    app.apply_math()
    zeroed = alphas_of(app, first.index)
    check("subtract 255 zeroes every alpha", set(zeroed) == {0},
          "%d distinct values, max %d" % (len(set(zeroed)), max(zeroed)))
    check("one operation makes one log entry", len(app.history) == log_before + 1,
          "%d new entries" % (len(app.history) - log_before))
    check("the log records one byte per vertex", app.history[-1]["shapes"][0]["bpv"] == 1)
    app.do_undo()
    check("undo restores every alpha", alphas_of(app, first.index) == original)

    app.math_op.set("floor - raise anything below it")
    app.math_value.set("77")
    app.apply_math()
    floored = alphas_of(app, first.index)
    check("floor 77 raises nothing above 255 and nothing below 77", min(floored) >= 77 and max(floored) <= 255,
          "range %d-%d" % (min(floored), max(floored)))
    check("floor keeps values that were already higher",
          all(f >= o for f, o in zip(floored, original)))
    app.do_undo()

    # -- apply_math: RGB and RGBA -------------------------------------------------------------
    app.math_target.set("RGB + alpha")
    app.math_op.set("multiply by the value")
    app.math_value.set("0")
    app.apply_math()
    greyscale = colours_of(app, first.index)
    check("multiply RGB+alpha by 0 blacks the shape out and strips its alpha",
          set(greyscale) == {b"\x00\x00\x00\x00"},
          "distinct colors: %s" % sorted(set(greyscale))[:4])
    check("a color edit records four bytes per vertex", app.history[-1]["shapes"][0]["bpv"] == 4)
    app.do_undo()

    app.math_target.set("RGB only")
    app.math_op.set("multiply by the value")
    app.math_value.set("0")
    app.apply_math()
    rgb_only = colours_of(app, first.index)
    check("RGB only leaves the alpha channel alone",
          [c[3] for c in rgb_only] == original)
    check("RGB only zeroes the color channels", set(c[:3] for c in rgb_only) == {b"\x00\x00\x00"},
          "distinct: %s" % sorted(set(c[:3] for c in rgb_only))[:4])
    app.do_undo()
    check("undo after a color edit restores the whole color",
          colours_of(app, first.index) == original_cols,
          "first was %s, now %s" % (original_cols[0], colours_of(app, first.index)[0]))

    # -- _paint_at: only the vertices under the brush ------------------------------------------
    positions = [s for s in app.all if s.index == first.index][0].positions()
    screen = []
    for vert, (x, y, _z) in enumerate(positions):
        screen.append((200.0 + x * 10.0, 200.0 - y * 10.0, first.index, vert))
    app._screen = screen
    click = screen[0][:2]
    inside = [v for sx, sy, _s, v in screen if (sx - click[0]) ** 2 + (sy - click[1]) ** 2 <= 12 * 12]
    outside = [v for sx, sy, _s, v in screen if v not in inside]
    app.paint_value.set("0")
    app.math_target.set("alpha only")
    log_before = len(app.history)
    undo_before = len(app.undo)
    app._press_left(type("E", (), {"x": click[0], "y": click[1]})())
    painted = alphas_of(app, first.index)
    check("the brush found at least one vertex under the click", len(inside) > 0, "%d" % len(inside))
    check("every vertex under the brush was set to the brush value",
          all(painted[v] == 0 for v in inside), "%d of %d" % (sum(1 for v in inside if painted[v] == 0),
                                                              len(inside)))
    check("no vertex outside the brush moved", all(painted[v] == original[v] for v in outside),
          "%d moved" % sum(1 for v in outside if painted[v] != original[v]))
    check("a press pushes exactly one undo step", len(app.undo) == undo_before + 1,
          "%d new" % (len(app.undo) - undo_before))

    before_second = list(painted)
    app._paint_at(click[0], click[1])
    check("a second click in the same stroke re-touches nothing",
          alphas_of(app, first.index) == before_second)
    app._release_left(type("E", (), {"x": click[0], "y": click[1]})())
    check("releasing the stroke clears its bookkeeping",
          app._paint_last is None and app._paint_entries == [] and app._paint_done == set())
    check("the stroke recorded exactly one log entry", len(app.history) == log_before + 1,
          "%d new entries" % (len(app.history) - log_before))
    app.do_undo()
    check("one undo removes a whole stroke", alphas_of(app, first.index) == original)

    # -- a shape with no VF_COLORS gains a defined starting color -----------------------------
    bare = next((s for s in app.all if s.ok and s.colors() is None), None)
    if bare is None:
        # No unpainted shape on this mesh, so build one: a real single-triangle-pair NIF with no color component.
        synthetic = os.path.join(ROOT, "_recon", "tmp_math_nocolors.nif")
        with open(synthetic, "wb") as fh:
            fh.write(nv._synthetic_nif(with_colors=False, with_alpha=False))
        app = make_app(synthetic, 0, log_path)
        bare = next((s for s in app.all if s.ok), None)
        check("synthetic control: a shape with no VF_COLORS was built", bare is not None)
    if bare is not None:
        app._picks = [bare.index]
        check("control: the shape really starts with no colors", bare.colors() is None)
        app.math_target.set("alpha only")
        app.math_op.set("multiply by the value")
        app.math_value.set("0.5")
        app.apply_math()
        gained = colours_of(app, bare.index)
        check("a shape with no colors gains white ones (not black)",
              gained is not None and set(c[:3] for c in gained) == {b"\xff\xff\xff"},
              "%s" % (None if gained is None else sorted(set(gained))[:2]))
        check("its alphas are the 255 baseline halved", set(c[3] for c in gained) == {128},
              "%s" % sorted(set(c[3] for c in gained))[:5])

    return checks, names, app


def gui_smoke(mesh, work_dir, log_path):
    """Open the real window on a throwaway copy of the mesh and paint one click through the actual bindings.

    This is the only way to test the parts a fake cannot: that `_build_math` ran before `_build_view` bound the
    canvas, that `render()` fills `self._screen`, and that a `<Button-1>` on the canvas reaches `_paint_at`.
    """
    import shutil
    checks = []

    def check(name, condition, detail=""):
        checks.append((name, bool(condition), detail))

    if not os.path.isdir(work_dir):
        os.makedirs(work_dir)
    local = os.path.join(work_dir, os.path.basename(mesh))
    shutil.copy2(mesh, local)
    with open(local, "rb") as fh:
        original_bytes = fh.read()

    app = gui.App(local, outdir=work_dir, no_open=True, live=False)
    try:
        check("the window built every tab", hasattr(app, "canvas") and hasattr(app, "paint_on"))
        check("the math panel exists with its three variables",
              all(hasattr(app, n) for n in ("math_target", "math_op", "math_value", "paint_value")))
        app.render()
        app.root.update()
        check("render() filled the screen map", len(app._screen) > 0, "%d entries" % len(app._screen))
        check("the screen map names a real shape and vertex",
              all(0 <= s < len(app.nif.blocks) and v >= 0 for _x, _y, s, v in app._screen[:50]))

        picked = [s for s in app.shapes if s.ok][0]
        app.tree.selection_set([str(picked.index)])
        app.nb.select(app.tab_view)
        app.root.update()
        app.render()
        app.root.update()
        before = [c[3] for c in picked.colors()] if picked.colors() else None
        check("the shape under test has colors", before is not None)
        if before is None:
            return checks

        app.paint_on.set(True)
        app.paint_value.set("0")
        app.math_target.set("alpha only")
        event = type("E", (), {"x": 0, "y": 0})()
        spot = None
        for x, y, s, _v in app._screen:
            if s == picked.index:
                spot = (x, y)
                break
        check("a vertex of the selected shape is on screen", spot is not None)
        if spot is None:
            return checks
        event.x, event.y = spot
        app._press_left(event)
        app._release_left(event)
        after = [c[3] for c in picked.colors()]
        check("clicking the preview painted through the real bindings", before != after,
              "no alpha changed")
        check("the click wrote exactly the brush value there", 0 in after)
        check("the stroke left one undo step", len(app.undo) == 1, "%d" % len(app.undo))
        app.do_undo()
        check("undo through the window restores the alphas",
              [c[3] for c in [s for s in app.shapes if s.index == picked.index][0].colors()] == before)
        check("the edit log was written", os.path.isfile(app.log_path), app.log_path)

        # save the painted file and prove the writer's own verification passed and the bytes re-parse
        app._press_left(event)
        app._release_left(event)
        painted = [c[3] for c in [s for s in app.shapes if s.index == picked.index][0].colors()]
        app.save()
        status_text = app.status.cget("text") if hasattr(app.status, "cget") else app.status.text
        out_file = app.current_out_path()
        check("Save wrote a file and said so", os.path.isfile(out_file) and ("written" in status_text.lower()),
              status_text)
        with open(out_file, "rb") as fh:
            reread = nv.Nif(fh.read())
        same = [s for s in reread.shapes() if s.index == picked.index][0]
        check("the saved file re-parses and the painted alphas are on disk",
              [c[3] for c in same.colors()] == painted,
              "disk %s vs memory %s" % ([c[3] for c in same.colors()][:4], painted[:4]))
        check("the saved file still validates", all(s.ok for s in reread.shapes() if s.index == picked.index))
        with open(app.backup_path, "rb") as fh:
            backup_bytes = fh.read()
        check("the .bak from load still holds the pre-session bytes", backup_bytes == original_bytes,
              "%d vs %d bytes" % (len(backup_bytes), len(original_bytes)))
    finally:
        try:
            app.root.destroy()
        except Exception:
            pass
    return checks


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mesh", default=os.path.join("D:\\0cto-AIO-Staging\\AIO\\meshes\\landscape\\Mountains",
                                                   "MountainTrim01.nif"))
    ap.add_argument("--log", default=os.path.join(ROOT, "_recon", "tmp_math_test.editlog.json"))
    ap.add_argument("--gui", action="store_true",
                    help="also open the real window on a copy under _recon\\ and click the preview once")
    args = ap.parse_args(argv)
    if not os.path.isfile(args.mesh):
        print("mesh not found: %s" % args.mesh)
        return 2
    checks, names, app = run(args.mesh, args.log)
    if args.gui:
        checks = checks + gui_smoke(args.mesh, os.path.join(ROOT, "_recon", "tmp_math_gui"), args.log)
    lines = ["# nif_studio math self-test", "mesh: %s" % args.mesh, "shapes: %s" % ", ".join(names),
             "mode: %s" % ("headless + real window" if args.gui else "headless"), ""]
    failures = 0
    for name, ok, detail in checks:
        lines.append("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok or not detail else "  -- " + detail))
        failures += 0 if ok else 1
    lines.append("")
    lines.append("%d check(s), %d failed" % (len(checks), failures))
    out = os.path.join(ROOT, "_recon", "nif_studio_math_test.txt")
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
