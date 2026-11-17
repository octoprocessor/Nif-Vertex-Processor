#!/usr/bin/env python
"""nif_studio.py -- one window for the shape-level work: merge a copy, reUV, and paint vertex alpha in steps.

READS : one .nif, once. Everything after that is done **in memory** on a `nif_vertex.Nif` object, so stepping back and
        forth costs nothing and no temporary file is needed; the file is only written by Save.
WRITES: `<stem>_edit.nif` beside the source (or under --out) on Save, then opens it in NifSkope. Sources are never
        modified. Errors land in `_recon\\nif_studio.log`.

Three tabs, as asked:

  Shapes     the table. Double-click a row to give that shape its own operation. Select all / none. Every heading has a
             hover tooltip. The `op` column shows what that row will do.
  Operations one dropdown for the operation, its parameters beside it, and two buttons: *Apply to selection* (now) and
             *Apply staged ops* (everything the rows are set to, in one click).
  Preview    a software render of the selected shapes, redrawn on every edit, plus *Open in NifSkope* for the real
             renderer.

Being straight about the preview: NifSkope has no command-line render, and driving its render window would mean
UI-Automation screenshots of another process. What is here is a small painter's-algorithm renderer that shades each
triangle by its **vertex alpha**, which is what a mask looks like; it is for judging coverage while painting, not for
judging materials. The button next to it opens the same file in NifSkope, which stays authoritative.

Operations
  merge + weld      a new merged COPY, reduced to one vertex per position (the originals stay)
  merge, no weld    a new merged COPY with the vertex arrays concatenated exactly as SNIFF does it
  reUV (weld+unweld) weld coincident vertices, then rebuild the UV map at the original mean texel density
  invert alpha      a -> 255 - a
  blend edges       ring 0 black, the rest white
  blend + buffer N  rings 0..N black
  blend gradient N  ring 0 black, even steps out to white at ring N
  all white         every vertex opaque
  reset             put back the alpha each shape has on disk (the file as loaded)

colour & alpha math   one operation (multiply / add / subtract / floor / ceiling) applied to every vertex of the
                       selected shapes, on the chosen target (alpha only, RGB only, or RGB + alpha), with a value
                       entered on the 0-255 byte scale. Every result is clamped to 0-255, never wrapped.
                       Ticking *paint vertices by clicking the preview* turns the same value into a brush: a click (or
                       a drag) writes it into the vertices under the cursor, one undo step and one log entry per
                       stroke. A literal brush value overrides the operation for that click.

`keep the lower of mask and original` is the min() switch, on by default: a mask may only remove coverage.
"""

import base64
import io
import json
import os
import subprocess
import sys
import time
import tkinter
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import nif_vertex as nv                                                    # noqa: E402
import nif_shape_ops as sop                                                # noqa: E402
import nif_weld_uv as wu                                                   # noqa: E402
import vertex_alpha_mask as vam                                            # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "_recon", "nif_studio.log")
STATE = os.path.join(ROOT, "_recon", "nif_studio_state.json")


def load_state():
    try:
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(**kw):
    """A two-key settings file, so the Save As dialog remembers where it was last time, as a Windows app would."""
    state = load_state()
    state.update(kw)
    try:
        with open(STATE, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(state, fh, indent=1)
    except OSError as exc:
        log("could not write %s: %s" % (STATE, exc))

BG, PANEL, FG, MUTED, ACCENT = "#1e1e1e", "#252526", "#e6e6e6", "#9a9a9a", "#4a9eff"

OPS = [
    ("merge + weld", "merge_weld"),
    ("merge, no weld", "merge_plain"),
    ("reUV (weld + reproject)", "reuv"),
    ("invert alpha", "invert"),
    ("blend edges", "edges"),
    ("blend + buffer", "buffer"),
    ("blend gradient", "gradient"),
    ("all white", "white"),
    ("reset", "reset"),
    ("keep (stage only)", "keep"),
]
OP_ALPHA = ("invert", "edges", "buffer", "gradient", "white", "reset")
OP_IMMEDIATE = ("merge_weld", "merge_plain", "reuv")

# Color/alpha arithmetic. Values are entered on the 0-255 byte scale, the same numbers NifSkope shows; every result is
# clamped to 0-255 (= 0.0-1.0 normalized). `mul` takes the value as a factor times 255, so 255 is identity and 128 is
# about half -- the hint beside the entry says the normalized equivalent while typing.
MATH_TARGETS = [("alpha only", "alpha"), ("RGB only", "rgb"), ("RGB + alpha", "rgba")]
MATH_OPS = [("multiply by the value", "mul"), ("add the value", "add"), ("subtract the value", "sub"),
             ("floor - raise anything below it", "floor"), ("ceiling - lower anything above it", "ceil")]


def math_code(table, label, default):
    """The internal code behind a dropdown label. Accepts the code itself too, so a caller (or a test) can pass either
    and an unknown value falls back deliberately instead of silently picking the first operation."""
    for text, code in table:
        if label in (text, code):
            return code
    return default

HEADINGS = {
    "shape": "The trishape's name, as NifSkope lists it. Plans key on this name, because block indices do not survive a "
             "re-export or a merge while names do.",
    "op": "What this row will do when you press *Apply staged ops*, or *nothing staged* when it will be left alone. "
          "Double-click the row to set it.",
    "v": "Vertices in the shape.",
    "t": "Triangles in the shape.",
    "ring": "One buffer/gradient ring in world units: the shape's median triangle edge length. N x ring is the feather "
            "depth, so different-sized shapes need different ring counts.",
    "col": "yes/no: does the shape carry a vertex color component (VF_COLORS)? Without one there is no vertex alpha "
           "to write, and the tool adds one.",
    "prop": "The shape's NiAlphaProperty flags. 0x10ED = blending (the mask target); 0x12EC = alpha TEST, a hard "
            "cutout; 0x10CF = additive; none = no property.",
    "blk": "NIF block index. Diagnostic only - it changes whenever the file is edited.",
    "tex": "The shape's diffuse texture, from its own texture set.",
}


def log(line):
    try:
        with io.open(LOG, "a", encoding="utf-8", newline="\n") as fh:
            import time
            fh.write("%s\t%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), line))
    except OSError:
        pass


class Tooltip(object):
    """A one-line hint that follows the pointer. Bound to a widget (`<Enter>`/`<Leave>`) or driven by the caller."""

    def __init__(self, widget, text, delay=350):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.tip = None
        self.after = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e=None):
        self._cancel()
        self.after = self.widget.after(self.delay, self.show)

    def _cancel(self):
        if self.after:
            try:
                self.widget.after_cancel(self.after)
            except Exception:
                pass
            self.after = None

    def show(self, text=None, x=None, y=None):
        body = text or self.text
        if not body or self.tip:
            return
        self.tip = tkinter.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        try:
            self.tip.attributes("-topmost", True)
        except Exception:
            pass
        tkinter.Label(self.tip, text=body, justify="left", bg="#3a3a3a", fg=FG, relief="solid", bd=1,
                      wraplength=430, padx=6, pady=3).pack()
        px, py = x, y
        if px is None:
            px = self.widget.winfo_rootx() + 12
            py = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip.geometry("+%d+%d" % (px + 12, py + 18))

    def _hide(self, _e=None):
        self._cancel()
        if self.tip:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None


class PickTexture(tkinter.Toplevel):
    """Choose which source texture set the merged copy should use. Duplicates are removed before it opens."""

    def __init__(self, parent, choices):
        tkinter.Toplevel.__init__(self, parent)
        self.title("Which texture set for the merged copy?")
        self.configure(bg=BG)
        self.transient(parent)
        self.resizable(False, False)
        self.result = None
        tkinter.Label(self, bg=BG, fg=FG, justify="left", anchor="w", wraplength=640, padx=12, pady=10,
                      text="The sources use %d different texture sets. Pick the one the merged shape should carry:"
                           % len(choices)).pack(fill="x")
        body = tkinter.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        self.var = tkinter.IntVar(value=0)
        for i, c in enumerate(choices):
            row = tkinter.Frame(body, bg=BG)
            row.pack(fill="x", anchor="w", pady=1)
            tkinter.Radiobutton(row, variable=self.var, value=i, bg=BG, fg=FG, selectcolor=PANEL,
                                activebackground=BG, activeforeground=FG, text="(no texture set)"
                                if not c["paths"] else c["paths"][0].replace("textures\\", "")).pack(side="left")
            tkinter.Label(row, bg=BG, fg=MUTED, text="   used by %d: %s"
                          % (len(c["shapes"]), ", ".join(c["shapes"][:3]))).pack(side="left")
        bar = tkinter.Frame(self, bg=BG)
        bar.pack(fill="x", padx=12, pady=(4, 12))
        tkinter.Button(bar, text="Use this one", bg=ACCENT, fg="#101010", activebackground="#7ab8ff",
                       command=self._ok).pack(side="left")
        tkinter.Button(bar, text="Cancel", bg=PANEL, fg=FG, activebackground=ACCENT,
                       command=self.destroy).pack(side="left", padx=6)
        self.lift()

    def _ok(self):
        self.result = self.var.get()
        self.destroy()


class App(object):
    def __init__(self, path, outdir="", no_open=False, config="", live=True):
        self.path = os.path.abspath(path)
        self.outdir = outdir
        self.no_open = no_open
        self.live = live and not outdir          # write the file being edited, not a sibling copy
        with open(self.path, "rb") as fh:
            self.pristine_bytes = fh.read()
        self.backup_path = self.make_backup()
        self.nif = nv.Nif(self.pristine_bytes)
        self.pristine = nv.Nif(self.pristine_bytes)
        if not self.nif.is_sse:
            raise nv.NifError("not a Skyrim SE NIF (version 0x%X, userVersion %d)"
                              % (self.nif.version, self.nif.user_version))
        self.plan_rows = []
        if config:
            self.plan_rows, problems = nv.load_shape_plan(config)
            for p in problems:
                log("plan: %s" % p)
        self.staged = {}
        self.undo = []
        self.redo = []
        self.history = []                        # the durable undo chain: one entry per applied operation
        self.log_path = os.path.splitext(self.path)[0] + ".editlog.json"
        self.last_dir = load_state().get("last_save_dir", "")
        self.ring_cache = {}
        self.mesh_cache = {}
        self.status = None

        self.root = tkinter.Tk()
        self.root.title("0cto shape studio - %s" % os.path.basename(self.path))
        self.root.configure(bg=BG)
        self.root.geometry("1280x720")
        self.root.minsize(1080, 600)

        head = tkinter.Frame(self.root, bg=PANEL)
        head.pack(fill="x")
        self.title_label = tkinter.Label(head, text=os.path.basename(self.path), bg=PANEL, fg=FG,
                                         font=("Segoe UI", 11, "bold"))
        self.title_label.pack(side="left", padx=12, pady=6)
        self.path_label = tkinter.Label(head, text=self.path, bg=PANEL, fg=MUTED)
        self.path_label.pack(side="left", padx=4)
        tkinter.Button(head, text="Save", command=self.save, bg=ACCENT, fg="#101010",
                       activebackground="#7ab8ff").pack(side="right", padx=8, pady=4)
        tkinter.Button(head, text="Save As...", command=self.save_as, bg=PANEL, fg=FG,
                       activebackground=ACCENT).pack(side="right", pady=4)
        tkinter.Button(head, text="Undo", command=self.do_undo, bg=PANEL, fg=FG,
                       activebackground=ACCENT).pack(side="right", padx=(6, 0), pady=4)
        tkinter.Button(head, text="Redo", command=self.do_redo, bg=PANEL, fg=FG,
                       activebackground=ACCENT).pack(side="right", padx=(0, 4), pady=4)
        tkinter.Button(head, text="Restore .bak", command=self.restore_backup, bg=PANEL, fg=FG,
                       activebackground=ACCENT).pack(side="right", padx=(0, 6), pady=4)
        self.root.bind("<Control-s>", lambda e: self.save())
        self.root.bind("<Control-S>", lambda e: self.save_as())
        self.root.bind("<Control-Shift-S>", lambda e: self.save_as())
        self.root.bind("<Control-z>", lambda e: self.do_undo())
        self.root.bind("<Control-y>", lambda e: self.do_redo())
        tkinter.Button(head, text="Open in NifSkope", command=self.open_render, bg=PANEL, fg=FG,
                       activebackground=ACCENT).pack(side="right", padx=6, pady=4)
        tkinter.Button(head, text="Reload in NifSkope", command=self.reload_in_nifskope, bg=PANEL, fg=FG,
                       activebackground=ACCENT).pack(side="right", pady=4)

        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=8, pady=(6, 0))
        self.tab_shapes = tkinter.Frame(self.nb, bg=BG)
        self.tab_ops = tkinter.Frame(self.nb, bg=BG)
        self.tab_view = tkinter.Frame(self.nb, bg=BG)
        self.nb.add(self.tab_shapes, text="Shapes")
        self.nb.add(self.tab_ops, text="Operations")
        self.nb.add(self.tab_view, text="Preview")

        self._build_shapes()
        self._build_ops()
        self._build_view()

        self.status = tkinter.Label(self.root, text="", bg=PANEL, fg=FG, justify="left", anchor="w", wraplength=1240)
        self.status.pack(fill="x", side="bottom", ipady=4)
        self.reload_shapes()
        self.select_all()
        log("loaded %s: %d shape(s)" % (self.path, len(self.shapes)))

    # -- table -----------------------------------------------------------------------------------
    def _build_shapes(self):
        top = tkinter.Frame(self.tab_shapes, bg=BG)
        top.pack(fill="x", pady=(6, 2))
        for text, cmd in (("Select all rows", self.select_all),
                          ("Select none", self.select_none),
                          ("Merge copy of selection...", lambda: self.do_merge(True)),
                          ("Merge copy, no weld...", lambda: self.do_merge(False)),
                          ("Rename shape...", self.do_rename),
                          ("Rename texture path...", self.do_rename_texture)):
            tkinter.Button(top, text=text, command=cmd, bg=PANEL, fg=FG,
                           activebackground=ACCENT).pack(side="left", padx=(0, 6))
        mid = tkinter.Frame(self.tab_shapes, bg=BG)
        mid.pack(fill="both", expand=True)
        cols = ("shape", "op", "v", "t", "ring", "col", "prop", "blk", "tex")
        widths = {"shape": 160, "op": 96, "v": 38, "t": 38, "ring": 52, "col": 32, "prop": 54, "blk": 34,
                  "tex": 210}
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended", height=14)
        for c in cols:
            self.tree.heading(c, text=c)
            self.tree.column(c, width=widths[c], anchor="w", stretch=(c == "tex"))
            Tooltip(self.tree, HEADINGS[c])
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        sb.pack(side="left", fill="y")
        self.tree.configure(yscrollcommand=sb.set)
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tkinter.TclError:
            pass
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG, rowheight=20)
        style.configure("Treeview.Heading", background=BG, foreground=ACCENT)
        self.tree.bind("<Double-1>", self._on_double)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.render())
        self.tree.bind("<Motion>", self._hover)
        self.tip = Tooltip(self.tree, "")

    def _hover(self, event):
        col = self.tree.identify_column(event.x)
        if not row or not col:
            self.tip._hide()
            return
        name = self.tree["columns"][int(col[1:]) - 1]
        self.tip.show(HEADINGS.get(name, ""), event.x_root, event.y_root)

    def _on_double(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self.edit_row(int(row))

    def edit_row(self, index):
        """One shape's staged operation, in a small dialog: the row's own choice, or back to nothing."""
        sh = [s for s in self.shapes if s.index == index][0]
        dlg = tkinter.Toplevel(self.root)
        dlg.title("%s - this row only" % sh.name)
        dlg.configure(bg=BG)
        dlg.transient(self.root)
        dlg.resizable(False, False)
        cur = self.staged.get(index, {})
        var = tkinter.StringVar(value=dict((v, k) for k, v in OPS).get(cur.get("op", "keep"), OPS[-1][0]))
        rings = tkinter.IntVar(value=int(cur.get("rings", self.rings.get())))
        minimum = tkinter.BooleanVar(value=bool(cur.get("minimum", self.minimum.get())))
        tkinter.Label(dlg, text=sh.name, bg=BG, fg=FG, font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=12,
                                                                                          pady=(10, 0))
        tkinter.Label(dlg, text="%d verts, %d tris, one ring = %s" % (sh.vert, sh.tri, self._ring(sh)),
                      bg=BG, fg=MUTED).pack(anchor="w", padx=12)
        row = tkinter.Frame(dlg, bg=BG)
        row.pack(fill="x", padx=12, pady=(8, 0))
        ttk.Combobox(row, textvariable=var, state="readonly", width=26,
                     values=[k for k, _v in OPS]).pack(side="left")
        tkinter.Label(row, text="rings", bg=BG, fg=MUTED).pack(side="left", padx=(10, 3))
        tkinter.Spinbox(row, from_=0, to=64, width=4, textvariable=rings, bg=PANEL, fg=FG,
                        insertbackground=FG).pack(side="left")
        tkinter.Checkbutton(dlg, text="keep the lower of mask and original", variable=minimum, bg=BG, fg=FG,
                            selectcolor=PANEL, activebackground=BG, activeforeground=FG).pack(anchor="w", padx=12,
                                                                                            pady=(6, 0))
        bar = tkinter.Frame(dlg, bg=BG)
        bar.pack(fill="x", padx=12, pady=10)

        def use():
            code = dict(OPS)[var.get()]
            if code == "keep":
                self.staged.pop(index, None)
            else:
                self.staged[index] = dict(op=code, rings=int(rings.get()), minimum=bool(minimum.get()))
            dlg.destroy()
            self.reload_shapes()
            self._describe_staged()

        def clear():
            self.staged.pop(index, None)
            dlg.destroy()
            self.reload_shapes()
            self._describe_staged()

        tkinter.Button(bar, text="Use for this row", command=use, bg=ACCENT, fg="#101010").pack(side="left")
        tkinter.Button(bar, text="Nothing for this row", command=clear, bg=PANEL, fg=FG).pack(side="left", padx=6)
        tkinter.Button(bar, text="Cancel", command=dlg.destroy, bg=PANEL, fg=FG).pack(side="right")
        dlg.lift()
        return dlg

    def reload_shapes(self):
        keep = set(self.tree.selection())
        for row in self.tree.get_children():
            self.tree.delete(row)
        self.shapes = list(self.nif.shapes())
        self.ring_cache.clear()
        self.mesh_cache.clear()
        for sh in self.shapes:
            if not sh.ok:
                self.tree.insert("", "end", iid=str(sh.index),
                                 values=(sh.name, "-", sh.vert, sh.tri, "-", "-", "-", sh.index, sh.reason))
                continue
            ap = nv.read_alpha_property(self.nif, sh)
            prop = "none" if ap is None else ("?" if "error" in ap else "0x%04X" % ap["flags"])
            tex = sop.texture_paths(self.nif, sh)
            staged = self.staged.get(sh.index, {}).get("op", "")
            label = dict((v, k) for k, v in OPS).get(staged, "nothing staged")
            self.tree.insert("", "end", iid=str(sh.index), values=(
                sh.name, label, sh.vert, sh.tri, self._ring(sh), "yes" if sh.has_colors else "NO", prop, sh.index,
                (tex[0] if tex else "-").replace("textures\\", "")))
        still = [i for i in keep if i in set(self.tree.get_children())]
        if still:
            self.tree.selection_set(still)

    def _ring(self, sh):
        if sh.index not in self.ring_cache:
            try:
                self.ring_cache[sh.index] = vam.median_edge_length(sh.positions(), sh.triangles())
            except Exception:
                self.ring_cache[sh.index] = 0.0
        step = self.ring_cache[sh.index]
        return ("%.3gu" % step) if step else "-"

    def chosen(self):
        return [s for s in self.shapes if str(s.index) in self.tree.selection() and s.ok]

    def select_all(self):
        self.tree.selection_set([i for i in self.tree.get_children()])

    def select_none(self):
        self.tree.selection_remove([i for i in self.tree.get_children()])

    # -- operations tab -------------------------------------------------------------------------
    def _build_ops(self):
        box = tkinter.Frame(self.tab_ops, bg=BG)
        box.pack(fill="x", padx=10, pady=10)
        tkinter.Label(box, text="Operation", bg=BG, fg=MUTED).grid(row=0, column=0, sticky="w")
        self.op = tkinter.StringVar(value=OPS[4][0])
        self.op_box = ttk.Combobox(box, textvariable=self.op, state="readonly", width=26,
                                   values=[k for k, _v in OPS])
        self.op_box.grid(row=1, column=0, sticky="w")
        self.op_box.bind("<<ComboboxSelected>>", lambda e: self._op_changed())
        tkinter.Label(box, text="rings", bg=BG, fg=MUTED).grid(row=0, column=1, padx=(14, 0), sticky="w")
        self.rings = tkinter.IntVar(value=1)
        self.ring_box = tkinter.Spinbox(box, from_=0, to=64, width=4, textvariable=self.rings, bg=PANEL, fg=FG,
                                        insertbackground=FG)
        self.ring_box.grid(row=1, column=1, padx=(14, 0), sticky="w")
        self.minimum = tkinter.BooleanVar(value=True)
        tkinter.Checkbutton(box, text="keep the lower of mask and original", variable=self.minimum, bg=BG, fg=FG,
                            selectcolor=PANEL, activebackground=BG, activeforeground=FG).grid(
            row=1, column=2, padx=(18, 0), sticky="w")
        tkinter.Button(box, text="Apply to selection", command=self.apply_now, bg=ACCENT, fg="#101010",
                       activebackground="#7ab8ff").grid(row=1, column=3, padx=(18, 0))
        tkinter.Button(box, text="Apply staged ops", command=self.apply_staged, bg=PANEL, fg=FG,
                       activebackground=ACCENT).grid(row=1, column=4, padx=(6, 0))
        tkinter.Button(box, text="Stage on selection", command=self.stage_selection, bg=PANEL, fg=FG,
                       activebackground=ACCENT).grid(row=1, column=5, padx=(6, 0))

        help_text = ("Apply to selection  - run the operation on the selected rows now.\n"
                     "Stage on selection  - remember it for those rows; nothing changes yet.\n"
                     "Apply staged ops    - run every staged row in one click, in table order.\n\n"
                     "Merge operations always apply immediately: they change the shape list, so there is nothing to "
                     "stage them onto. They append a NEW shape and leave the originals alone.")
        tkinter.Label(self.tab_ops, text=help_text, bg=BG, fg=MUTED, justify="left", anchor="w",
                      wraplength=1000).pack(fill="x", padx=10, pady=(10, 0))
        self._build_math()
        self.staged_label = tkinter.Label(self.tab_ops, text="", bg=BG, fg=FG, justify="left", anchor="w",
                                         wraplength=1100)
        self.staged_label.pack(fill="x", padx=10, pady=(10, 0))
        self._op_changed()

    def _build_math(self):
        """Color and alpha arithmetic on whole shapes, plus a per-vertex brush that uses the same value."""
        box = tkinter.LabelFrame(self.tab_ops, text=" color & alpha math ", bg=BG, fg=FG, bd=1, relief="groove")
        box.pack(fill="x", padx=10, pady=(12, 0))
        tkinter.Label(box, text="target", bg=BG, fg=MUTED).grid(row=0, column=0, padx=(10, 4), pady=(6, 0), sticky="w")
        self.math_target = tkinter.StringVar(value=MATH_TARGETS[0][0])
        ttk.Combobox(box, textvariable=self.math_target, state="readonly", width=16,
                     values=[k for k, _v in MATH_TARGETS]).grid(row=1, column=0, padx=(10, 4), pady=(0, 8), sticky="w")
        tkinter.Label(box, text="operation", bg=BG, fg=MUTED).grid(row=0, column=1, padx=(10, 4), pady=(6, 0),
                                                                  sticky="w")
        self.math_op = tkinter.StringVar(value=MATH_OPS[0][0])
        ttk.Combobox(box, textvariable=self.math_op, state="readonly", width=26,
                     values=[k for k, _v in MATH_OPS]).grid(row=1, column=1, padx=(10, 4), pady=(0, 8), sticky="w")
        tkinter.Label(box, text="value (0-255)", bg=BG, fg=MUTED).grid(row=0, column=2, padx=(10, 4), pady=(6, 0),
                                                                      sticky="w")
        self.math_value = tkinter.StringVar(value="255")
        entry = tkinter.Entry(box, textvariable=self.math_value, width=8, bg=PANEL, fg=FG, insertbackground=FG)
        entry.grid(row=1, column=2, padx=(10, 4), pady=(0, 8), sticky="w")
        self.math_hint = tkinter.Label(box, text="", bg=BG, fg=MUTED)
        self.math_hint.grid(row=1, column=3, padx=(0, 10), sticky="w")
        tkinter.Button(box, text="Apply to selection", command=self.apply_math, bg=ACCENT, fg="#101010",
                       activebackground="#7ab8ff").grid(row=1, column=4, padx=(10, 4), pady=(0, 8))
        self.paint_on = tkinter.BooleanVar(value=False)
        tkinter.Checkbutton(box, text="paint vertices by clicking the preview", variable=self.paint_on, bg=BG, fg=FG,
                            selectcolor=PANEL, activebackground=BG, activeforeground=FG,
                            command=self.render).grid(
            row=2, column=0, columnspan=3, padx=(10, 4), pady=(0, 8), sticky="w")
        self.paint_value = tkinter.StringVar(value="")
        tkinter.Label(box, text="brush sets this value:", bg=BG, fg=MUTED).grid(row=2, column=3, sticky="e")
        tkinter.Entry(box, textvariable=self.paint_value, width=8, bg=PANEL, fg=FG,
                      insertbackground=FG).grid(row=2, column=4, padx=(4, 10), pady=(0, 8), sticky="w")
        tkinter.Label(box, text="255 = 1.0   ·   128 = about half   ·   results clamp to 0-255 (0-1 normalized)",
                      bg=BG, fg=MUTED).grid(row=3, column=0, columnspan=5, padx=10, pady=(0, 6), sticky="w")
        tkinter.Label(box, text="On a LANDSCAPE mesh Community Shaders divides vertex RGB by its own brightest "
                               "channel, so scaling RGB uniformly is invisible there - alpha is what a landscape mask "
                               "actually uses.", bg=BG, fg=MUTED, wraplength=1000, justify="left").grid(
            row=4, column=0, columnspan=5, padx=10, pady=(0, 6), sticky="w")
        entry.bind("<KeyRelease>", lambda e: self._math_hint())
        self._math_hint()

    def _math_hint(self):
        try:
            v = float(self.math_value.get())
        except ValueError:
            self.math_hint.configure(text="not a number")
            return
        self.math_hint.configure(text="= %.3f normalized" % (v / 255.0))

    def math_channel(self, old):
        """One channel of one vertex, after the chosen operation, clamped to 0-255."""
        try:
            v = float(self.math_value.get())
        except ValueError:
            v = 0.0
        op = math_code(MATH_OPS, self.math_op.get(), "mul")
        if op == "mul":
            r = old * v
        elif op == "add":
            r = old + v
        elif op == "sub":
            r = old - v
        elif op == "floor":
            r = max(old, v)
        else:
            r = min(old, v)
        return 0 if r < 0 else (255 if r > 255 else int(round(r)))

    def apply_math(self):
        target = math_code(MATH_TARGETS, self.math_target.get(), "alpha")
        picked = self.chosen()
        if not picked:
            self.status.configure(text="nothing selected")
            return
        try:
            float(self.math_value.get())
        except ValueError:
            self.status.configure(text="the value is not a number")
            return
        self._snapshot()
        entries = []
        bpv = 1
        for sh in picked:
            before = sh.colors() or [bytes((255, 255, 255, 255))] * sh.vert
            if target == "alpha":
                old = [c[3] for c in before]
                new = [self.math_channel(a) for a in old]
                sh.set_alphas(new)
                entries.append((sh.name, old, new))
            else:
                bpv = 4
                new_cols = []
                for c in before:
                    b, g, r, a = c
                    nb = self.math_channel(b) if target in ("rgb", "rgba") else b
                    ng = self.math_channel(g) if target in ("rgb", "rgba") else g
                    nr = self.math_channel(r) if target in ("rgb", "rgba") else r
                    na = self.math_channel(a) if target == "rgba" else a
                    new_cols.append((nb, ng, nr, na))
                sh.set_colors(new_cols)
                entries.append((sh.name, b"".join(bytes(c) for c in before),
                                b"".join(bytes(c) for c in new_cols)))
            self.mesh_cache.pop(sh.index, None)
        self.record("math:%s:%s" % (target, self.math_op.get()), entries, bpv=bpv)
        self.reload_shapes()
        self.render()
        self.status.configure(text="%s %s on %d shape(s)  (%s)" % (
            self.math_op.get(), self.math_target.get(), len(picked), self.math_value.get()))

    def _op_changed(self):
        label = self.op.get()
        code = dict(OPS)[label]
        state = "normal" if code in ("buffer", "gradient") else "disabled"
        try:
            self.ring_box.configure(state=state)
        except tkinter.TclError:
            pass

    def current_op(self):
        return dict(OPS).get(self.op.get(), "keep")

    def stage_selection(self):
        code = self.current_op()
        if code in OP_IMMEDIATE:
            self.status.configure(text="%s changes the shape list, so it applies immediately - use the button on the "
                                       "Shapes tab" % self.op.get())
            return
        for sh in self.chosen():
            self.staged[sh.index] = dict(op=code, rings=int(self.rings.get()), minimum=bool(self.minimum.get()))
        self.reload_shapes()
        self._describe_staged()

    def _describe_staged(self):
        if not self.staged:
            self.staged_label.configure(text="nothing staged")
            return
        lines = []
        for i, st in sorted(self.staged.items()):
            try:
                name = nv.Shape(self.nif, i).name
            except nv.NifError:
                name = "block %d" % i
            lines.append("%s -> %s%s%s" % (name, dict((v, k) for k, v in OPS).get(st["op"], st["op"]),
                                           (" %d" % st["rings"]) if st["op"] in ("buffer", "gradient") else "",
                                           "" if st["minimum"] else " (replace)"))
        self.staged_label.configure(text="staged: " + " | ".join(lines))

    # -- backup, history, undo/redo ---------------------------------------------------------------
    def make_backup(self):
        """Copy the file as loaded to `<stem>.bak.nif`, once.

        The workflow this supports is live editing: the Save button writes the file that is open in NifSkope, so a
        refresh shows the change. The backup is what makes that safe, and octoprocessor's note is that it is cheap —
        copies of these meshes already exist at each state from sending them back and forth, so the only thing a mistake
        can destroy is the current revision.
        """
        stem = os.path.splitext(self.path)[0]
        bak = stem + ".bak.nif"
        if not os.path.isfile(bak):
            with open(bak, "wb") as fh:
                fh.write(self.pristine_bytes)
            log("backup created: %s" % bak)
        return bak

    def record(self, op, entries, bpv=1):
        """Append one operation to the durable log: per shape, the values before and after, base64 encoded.

        `bpv` is bytes per vertex -- 1 for an alpha-only edit, 4 for a colour edit (BGRA). Keeping it explicit stops a
        4-byte colour record from being read back as alphas.
        """
        if not entries:
            return
        self.history.append(dict(
            op=op,
            at=time.strftime("%Y-%m-%d %H:%M:%S"),
            file=os.path.basename(self.path),
            shapes=[dict(name=n, bpv=bpv, before=base64.b64encode(bytes(b)).decode("ascii"),
                         after=base64.b64encode(bytes(a)).decode("ascii"))
                    for n, b, a in entries]))
        self.write_log()

    def write_log(self):
        """The undo chain on disk. Small on purpose: one entry per operation with the alphas only."""
        try:
            with open(self.log_path, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(dict(file=os.path.basename(self.path), backup=os.path.basename(self.backup_path),
                               entries=self.history), fh, indent=1)
        except OSError as exc:
            log("could not write the edit log: %s" % exc)

    # -- the actual edits -----------------------------------------------------------------------
    def _snapshot(self):
        self.undo.append(self.nif.serialize())
        if len(self.undo) > 20:
            self.undo.pop(0)
        self.redo = []

    def do_undo(self):
        if not self.undo:
            self.status.configure(text="nothing to undo")
            return
        self.redo.append(self.nif.serialize())
        if len(self.redo) > 20:
            self.redo.pop(0)
        self.nif = nv.Nif(self.undo.pop())
        self.staged.clear()
        self.reload_shapes()
        self.render()
        self.status.configure(text="undone")

    def do_redo(self):
        if not self.redo:
            self.status.configure(text="nothing to redo")
            return
        self.undo.append(self.nif.serialize())
        self.nif = nv.Nif(self.redo.pop())
        self.staged.clear()
        self.reload_shapes()
        self.render()
        self.status.configure(text="redone")

    def restore_backup(self):
        """Put the file back to how it was when this session loaded it."""
        if not os.path.isfile(self.backup_path):
            self.status.configure(text="no backup beside this file")
            return
        if not messagebox.askyesno("Restore the backup?",
                                   "Replace the working copy with\n%s\n\nNothing is written to disk until you press Save."
                                   % os.path.basename(self.backup_path)):
            return
        with open(self.backup_path, "rb") as fh:
            self._snapshot()
            self.nif = nv.Nif(fh.read())
        self.staged.clear()
        self.reload_shapes()
        self.render()
        self.status.configure(text="restored from %s - press Save to write it" % os.path.basename(self.backup_path))

    def alphas_for(self, sh, code, rings, minimum):
        """The alpha list an operation would write. `reset` comes from the file as loaded, not from the working copy."""
        if code == "reset":
            for src in self.pristine.shapes():
                if src.name == sh.name and src.alphas():
                    return list(src.alphas())
            return list(sh.alphas() or [255] * sh.vert)
        if code == "invert":
            cur = sh.alphas()
            if cur is None:
                # nothing to invert: a shape with no vertex colors has no alpha, and 255 - 255 = 0 would blank it.
                # Reported as a skip so the row says why rather than silently going black.
                return None
            return [255 - a for a in cur]
        mode = {"edges": "edges", "buffer": "buffer", "gradient": "gradient", "white": "white"}.get(code)
        if mode is None:
            return None
        out = vam.final_alphas(sh, mode, rings, not minimum)[0]
        return list(out)

    def _apply_rows(self, rows):
        done, skipped, entries = [], [], []
        for sh, code, rings, minimum in rows:
            alphas = self.alphas_for(sh, code, rings, minimum)
            if alphas is None:
                skipped.append("%s (%s)" % (sh.name, "no vertex colors to invert" if code == "invert"
                                            else "not applicable"))
                continue
            before = sh.alphas() or [255] * sh.vert
            nv.paint_shape(self.nif, sh, alphas)
            done.append("%s %s %d verts" % (sh.name, code, len(alphas)))
            entries.append((sh.name, before, alphas))
        return done, skipped, entries

    def apply_now(self):
        code = self.current_op()
        if code in OP_IMMEDIATE:
            self.do_merge(code == "merge_weld") if code.startswith("merge") else self.do_reuv()
            return
        picked = self.chosen()
        if not picked:
            self.status.configure(text="nothing selected")
            return
        self._snapshot()
        done, skipped, entries = self._apply_rows(
            [(s, code, int(self.rings.get()), bool(self.minimum.get())) for s in picked])
        self.record(code, entries)
        self.reload_shapes()
        self.render()
        self.status.configure(text="applied %s to %d shape(s): %s%s" % (
            self.op.get(), len(done), ", ".join(done[:4]), ("; skipped " + ", ".join(skipped)) if skipped else ""))

    def apply_staged(self):
        if not self.staged:
            self.status.configure(text="nothing is staged")
            return
        self._snapshot()
        rows = []
        for i, st in sorted(self.staged.items()):
            try:
                sh = nv.Shape(self.nif, i)
            except nv.NifError:
                continue
            if not sh.ok:
                continue
            rows.append((sh, st["op"], st["rings"], st["minimum"]))
        done, skipped, entries = self._apply_rows(rows)
        self.record("staged", entries)
        self.staged.clear()
        self.reload_shapes()
        self.render()
        self.status.configure(text="applied %d staged operation(s): %s%s" % (
            len(done), ", ".join(done[:5]), ("; skipped " + ", ".join(skipped)) if skipped else ""))

    def do_reuv(self):
        picked = self.chosen()
        if not picked:
            self.status.configure(text="nothing selected")
            return
        self._snapshot()
        done = []
        for sh in picked:
            if sh.type != "BSTriShape":
                continue
            try:
                plan = wu.plan_shape(sh, 1e-4, "unwrap", "y", None, None, "average", "keep", 1200, "mean", "median")
                if not plan["ok"]:
                    continue
                wu.apply_shape(sh, plan, wu.read_vertices(sh), "average", "keep")
                done.append("%s %d->%d verts" % (sh.name, plan["verts"], plan["unique"]))
            except Exception as exc:
                log("reuv failed %s: %s" % (sh.name, exc))
        self.reload_shapes()
        self.render()
        self.status.configure(text="reUV: %s" % (", ".join(done) or "nothing done"))

    def do_merge(self, weld):
        picked = self.chosen()
        if len(picked) < 2:
            self.status.configure(text="select at least two shapes to merge")
            return
        choices = sop.texture_set_choices(self.nif, [s.index for s in picked])
        texture_from = picked[0].index
        if len(choices) > 1:
            picked_index = self.ask_texture(choices)
            if picked_index is None:
                self.status.configure(text="merge canceled")
                return
            chosen = choices[picked_index]
            texture_from = None
            for sh in picked:
                if sh.name in chosen["shapes"]:
                    texture_from = sh.index
                    break
        name = self._ask_name(picked[0].name + ("_welded" if weld else "_merged"))
        if not name:
            self.status.configure(text="merge canceled")
            return
        self._snapshot()
        try:
            rep = sop.merge_copy(self.nif, [s.index for s in picked], weld=weld, name=name, texture_from=texture_from)
        except nv.NifError as exc:
            self.undo.pop()
            self.status.configure(text="merge refused: %s" % exc)
            log("merge refused: %s" % exc)
            return
        self.reload_shapes()
        self.tree.selection_set([str(rep["index"])])
        self.render()
        self.status.configure(text="merged %d shape(s) -> %s: %d verts, %d tris, welded %d, texture from %s"
                                   % (len(rep["sources"]), rep["name"], rep["verts"], rep["tris"], rep["welded"],
                                      rep["texture_from"]))

    def ask_texture(self, choices):
        """Ask which texture set the copy should use. Split out so a test can answer without a human.

        Not asked when every source agrees: `texture_set_choices` has already removed duplicates, so a single entry
        means there is nothing to decide.
        """
        dlg = PickTexture(self.root, choices)
        self.root.wait_window(dlg)
        return dlg.result

    def ask_name(self, default):
        return self._ask_name(default)

    def _ask_name(self, default):
        dlg = tkinter.Toplevel(self.root)
        dlg.title("Name for the new shape")
        dlg.configure(bg=BG)
        dlg.transient(self.root)
        var = tkinter.StringVar(value=default)
        tkinter.Label(dlg, text="The name is a string in the NIF's string table; it is what a plan keys on.",
                      bg=BG, fg=MUTED, justify="left").pack(padx=12, pady=(10, 4))
        entry = tkinter.Entry(dlg, textvariable=var, width=44, bg=PANEL, fg=FG, insertbackground=FG)
        entry.pack(padx=12)
        entry.focus_set()
        entry.select_range(0, "end")
        out = {"v": None}

        def ok():
            out["v"] = var.get().strip()
            dlg.destroy()
        bar = tkinter.Frame(dlg, bg=BG)
        bar.pack(fill="x", padx=12, pady=10)
        tkinter.Button(bar, text="Use this name", command=ok, bg=ACCENT, fg="#101010").pack(side="left")
        tkinter.Button(bar, text="Cancel", command=dlg.destroy, bg=PANEL, fg=FG).pack(side="left", padx=6)
        dlg.bind("<Return>", lambda e: ok())
        dlg.lift()
        self.root.wait_window(dlg)
        return out["v"]

    def do_rename(self):
        picked = self.chosen()
        if len(picked) != 1:
            self.status.configure(text="select exactly one shape to rename")
            return
        name = self._ask_name(picked[0].name)
        if not name:
            return
        self._snapshot()
        before, after = sop.rename_shape(self.nif, picked[0].index, name)
        self.reload_shapes()
        self.render()
        self.status.configure(text="renamed %s -> %s" % (before, after))

    def do_rename_texture(self):
        picked = self.chosen()
        if len(picked) != 1:
            self.status.configure(text="select exactly one shape to re-point")
            return
        paths = sop.texture_paths(self.nif, picked[0])
        if not paths:
            self.status.configure(text="%s has no texture set" % picked[0].name)
            return
        dlg = tkinter.Toplevel(self.root)
        dlg.title("Texture path")
        dlg.configure(bg=BG)
        dlg.transient(self.root)
        var = tkinter.StringVar(value=paths[0])
        slot = tkinter.IntVar(value=0)
        tkinter.Label(dlg, text="slot", bg=BG, fg=MUTED).pack(anchor="w", padx=12, pady=(10, 0))
        ttk.Combobox(dlg, state="readonly", width=6, values=[str(i) for i in range(len(paths))],
                     textvariable=slot).pack(anchor="w", padx=12)
        tkinter.Label(dlg, text="new path", bg=BG, fg=MUTED).pack(anchor="w", padx=12, pady=(8, 0))
        tkinter.Entry(dlg, textvariable=var, width=70, bg=PANEL, fg=FG, insertbackground=FG).pack(padx=12)
        tkinter.Label(dlg, text="The texture set is cloned first, so only this shape changes.",
                      bg=BG, fg=MUTED).pack(anchor="w", padx=12, pady=(4, 0))
        out = {"v": None}

        def ok():
            out["v"] = (int(slot.get()), var.get().strip())
            dlg.destroy()
        bar = tkinter.Frame(dlg, bg=BG)
        bar.pack(fill="x", padx=12, pady=10)
        tkinter.Button(bar, text="Use this path", command=ok, bg=ACCENT, fg="#101010").pack(side="left")
        tkinter.Button(bar, text="Cancel", command=dlg.destroy, bg=PANEL, fg=FG).pack(side="left", padx=6)
        dlg.lift()
        self.root.wait_window(dlg)
        if not out["v"]:
            return
        self._snapshot()
        s, path = out["v"]
        sop.rename_texture(self.nif, picked[0].index, s, path)
        self.reload_shapes()
        self.render()
        self.status.configure(text="%s slot %d -> %s" % (picked[0].name, s, path))

    # -- preview --------------------------------------------------------------------------------
    def _build_view(self):
        bar = tkinter.Frame(self.tab_view, bg=BG)
        bar.pack(fill="x", padx=10, pady=6)
        self.show_alpha = tkinter.BooleanVar(value=True)
        tkinter.Checkbutton(bar, text="shade by vertex alpha", variable=self.show_alpha, bg=BG, fg=FG,
                            selectcolor=PANEL, activebackground=BG, activeforeground=FG,
                            command=self.render).pack(side="left")
        tkinter.Label(bar, text="drag to rotate", bg=BG, fg=MUTED).pack(side="left", padx=10)
        self.view_hint = tkinter.Label(bar, text="", bg=BG, fg=ACCENT)
        self.view_hint.pack(side="left", padx=10)
        tkinter.Label(bar, text="preview is a coverage check, not a material check - Open in NifSkope for that",
                      bg=BG, fg=MUTED).pack(side="right")
        self.canvas = tkinter.Canvas(self.tab_view, bg="#151515", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.yaw, self.pitch = 0.6, -0.5
        self.canvas.bind("<B1-Motion>", self._drag_left)
        self.canvas.bind("<Button-1>", self._press_left)
        self.canvas.bind("<ButtonRelease-1>", self._release_left)
        self.canvas.bind("<Button-3>", self._press_right)
        self.canvas.bind("<B3-Motion>", self._drag)
        self._drag_from = None
        self._paint_last = None
        self._paint_entries = []
        self._paint_done = set()
        self._paint_bpv = 1
        self._screen = []            # [(x, y, shape index, vertex index)] filled by render()

    # -- preview: rotate, or paint vertices when the brush is on --------------------------------
    def _press_left(self, event):
        if self.paint_on.get():
            if self._paint_last is None:                 # one undo snapshot and one log entry per stroke
                self._snapshot()
                self._paint_entries = []
                self._paint_done = set()
                self._paint_last = True
            self._paint_at(event.x, event.y)
        else:
            self._drag_from = (event.x, event.y)

    def _drag_left(self, event):
        if self.paint_on.get():
            self._paint_at(event.x, event.y)
        else:
            self._drag(event)

    def _release_left(self, event):
        self._drag_from = None
        if self._paint_last is not None:
            if self._paint_entries:
                self.record("paint:%s" % self._paint_summary(), self._paint_entries, bpv=self._paint_bpv)
            self._paint_last = None
            self._paint_entries = []
            self._paint_done = set()

    def _press_right(self, event):
        self._drag_from = (event.x, event.y)

    def _paint_at(self, x, y, radius=12):
        """Write the brush value into every vertex within `radius` pixels of the click.

        The brush value is a literal 0-255 figure when the entry box has one, otherwise the chosen math operation
        applied to each channel. Coordinates come from `self._screen`, which `render()` fills with the screen position
        of every vertex of the shapes currently drawn -- so what can be clicked is exactly what is visible.
        """
        if not self._screen:
            return
        literal = self._brush_literal()
        target = math_code(MATH_TARGETS, self.math_target.get(), "alpha")
        by_shape = {}
        for sx, sy, shape_index, vert in self._screen:
            if (sx - x) ** 2 + (sy - y) ** 2 > radius * radius:
                continue
            if (shape_index, vert) in self._paint_done:
                continue
            by_shape.setdefault(shape_index, []).append(vert)
        if not by_shape:
            return

        def channel(old):
            return literal if literal is not None else self.math_channel(old)

        hits = 0
        for shape_index, verts in by_shape.items():
            sh = nv.Shape(self.nif, shape_index)
            cols = sh.colors()
            if cols is None:
                continue                      # no VF_COLORS: there is no vertex color to paint into
            new_cols = list(cols)
            for vert in verts:
                self._paint_done.add((shape_index, vert))
                b, g, r, a = cols[vert]
                nb = channel(b) if target in ("rgb", "rgba") else b
                ng = channel(g) if target in ("rgb", "rgba") else g
                nr = channel(r) if target in ("rgb", "rgba") else r
                na = channel(a) if target in ("alpha", "rgba") else a
                new_cols[vert] = (nb, ng, nr, na)
                if new_cols[vert] != cols[vert]:
                    hits += 1
            if new_cols == list(cols):
                continue
            sh.set_colors(new_cols)
            if target == "alpha":
                self._paint_bpv = 1
                self._paint_entries.append((sh.name, bytes(c[3] for c in cols), bytes(c[3] for c in new_cols)))
            else:
                self._paint_bpv = 4
                self._paint_entries.append((sh.name, b"".join(bytes(c) for c in cols),
                                            b"".join(bytes(c) for c in new_cols)))
        if hits:
            self.mesh_cache.clear()
            self.render()
            self.status.configure(text="painted %d vertex/vertices with %s" % (hits, self._paint_summary()))

    def _brush_literal(self):
        """The brush's literal 0-255 value, or None to use the math operation instead."""
        txt = self.paint_value.get().strip()
        if not txt:
            return None
        try:
            return 0 if float(txt) < 0 else (255 if float(txt) > 255 else int(round(float(txt))))
        except ValueError:
            return None

    def _paint_summary(self):
        txt = self.paint_value.get().strip()
        return ("%s (literal)" % txt) if txt else ("%s %s" % (self.math_op.get(), self.math_value.get()))

    def _drag(self, event):
        if self._drag_from is None:
            self._drag_from = (event.x, event.y)
        dx = event.x - self._drag_from[0]
        dy = event.y - self._drag_from[1]
        self._drag_from = (event.x, event.y)
        self.yaw += dx * 0.01
        self.pitch += dy * 0.01
        self.render()

    def _mesh(self, sh):
        if sh.index in self.mesh_cache:
            return self.mesh_cache[sh.index]
        pos = sh.positions()
        tris = sh.triangles()
        alphas = sh.alphas() or [255] * sh.vert
        cols = sh.colors()
        self.mesh_cache[sh.index] = (pos, tris, alphas, cols)
        return self.mesh_cache[sh.index]

    def render(self):
        if not hasattr(self, "canvas"):
            return
        c = self.canvas
        c.delete("all")
        self._screen = []            # a stale screen map would let a click paint a vertex that is no longer drawn
        if hasattr(self, "view_hint"):
            self.view_hint.configure(text=("paint mode: a click writes %s  (untick it on the Operations tab to rotate)"
                                           % self._paint_summary()) if self.paint_on.get() else "")
        picked = self.chosen() or [s for s in self.shapes if s.ok]
        if not picked:
            c.create_text(12, 12, anchor="nw", fill=MUTED, text="no shapes")
            return
        import math
        ca, sa = math.cos(self.yaw), math.sin(self.yaw)
        cb, sb = math.cos(self.pitch), math.sin(self.pitch)
        pts3 = []
        polys = []
        owners = []
        budget = 4000
        total_tris = sum(len(self._mesh(s)[1]) for s in picked)
        step = max(1, total_tris // budget + 1)
        for sh in picked:
            pos, tris, alphas, cols = self._mesh(sh)
            if not pos:
                continue
            start = len(pts3)
            for p in pos:
                x, y, z = p
                x, z = x * ca - z * sa, x * sa + z * ca
                y, z = y * cb - z * sb, y * sb + z * cb
                pts3.append((x, y, z))
            owners.append((sh.index, start, len(pos)))
            for i, (a, b, d) in enumerate(tris):
                if i % step:
                    continue
                z = (pts3[start + a][2] + pts3[start + b][2] + pts3[start + d][2]) / 3.0
                shade = alphas[a] if self.show_alpha else 255
                if cols and not self.show_alpha:
                    col = cols[a]
                    polys.append((z, start + a, start + b, start + d, (col[2], col[1], col[0])))
                else:
                    v = int(max(0, min(255, shade)))
                    polys.append((z, start + a, start + b, start + d, (v, v, v)))
        if not polys:
            c.create_text(12, 12, anchor="nw", fill=MUTED, text="nothing to draw")
            return
        xs = [p[0] for p in pts3]
        ys = [p[1] for p in pts3]
        w = max(1, c.winfo_width())
        h = max(1, c.winfo_height())
        span = max(max(xs) - min(xs), max(ys) - min(ys), 1e-6)
        scale = 0.9 * min(w, h) / span
        cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
        polys.sort()
        # where every vertex landed, so a click can paint the nearest one (only shapes that are on screen)
        self._screen = []
        for shape_index, start, count in owners:
            for v in range(count):
                x, y, _z = pts3[start + v]
                self._screen.append(((x - cx) * scale + w / 2, -(y - cy) * scale + h / 2, shape_index, v))
        for z, a, b, d, rgb in polys:
            xy = []
            for i in (a, b, d):
                x, y, _z = pts3[i]
                xy.append((x - cx) * scale + w / 2)
                xy.append(-(y - cy) * scale + h / 2)
            c.create_polygon(*xy, fill="#%02x%02x%02x" % rgb, outline="", width=0)
        c.create_text(10, 10, anchor="nw", fill=MUTED,
                      text="%d shape(s), %d of %d triangle(s) drawn%s" % (
                          len(picked), len(polys), total_tris,
                          "  (subsampled for speed - the geometry is untouched)" if step > 1 else ""))

    # -- save -----------------------------------------------------------------------------------
    def reload_in_nifskope(self):
        """Re-read this file in the NifSkope window that already has it open.

        NifSkope keeps the document in memory and **this build has no file watcher** (`QFileSystemWatcher` does not
        appear in NifSkope.exe at all), so nothing refreshes by itself. Its File > Reload action carries no keyboard
        shortcut, so the menu item is invoked through UI Automation (`tools\\reload_nifskope.ps1`) instead of sending a
        keystroke into whatever window happens to be focused.
        """
        script = os.path.join(ROOT, "tools", "reload_nifskope.ps1")
        if not os.path.isfile(script):
            self.status.configure(text="reload script missing: %s" % script)
            return
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
                                  "-File", self.path],
                                 capture_output=True, text=True, timeout=90)
            text = (out.stdout or out.stderr or "").strip().splitlines()
            self.status.configure(text=(text[-1] if text else "reload: no output")
                                  + "   (File > Reload has no shortcut; Open in NifSkope if no window has it)")
            log("reload %s -> rc=%d %s" % (self.path, out.returncode, text[-1] if text else ""))
        except Exception as exc:
            self.status.configure(text="reload failed: %s" % exc)

    def open_render(self):
        """Open the file being edited in NifSkope. Nothing does this on Save - octoprocessor keeps NifSkope open with a
        hotkey for it (2026-11-13), so an automatic launch only put a second window in the way."""
        target = self.path if self.live else self.current_out_path()
        if not os.path.isfile(target):
            self.status.configure(text="nothing to open yet: %s does not exist" % os.path.basename(target))
            return
        result = nv.open_in_nifskope(target)
        self.status.configure(text="open %s: %s" % (os.path.basename(target), result))

    def out_path_exists(self):
        return os.path.isfile(self.path if self.live else self.current_out_path())

    def current_out_path(self):
        return nv.output_path_for(self.path, "_edit", self.outdir)[0]

    def save(self):
        if self.live:
            out_path, problem = self.path, ""
        else:
            out_path, problem = nv.output_path_for(self.path, "_edit", self.outdir)
        if problem:
            self.status.configure(text=problem)
            return
        self._write(out_path, live=self.live)

    def save_as(self, path=None):
        """The generic Windows Save As: a native dialog, a suggested `<stem>_edit.nif`, overwrite confirmation, and the
        document becomes the chosen file."""
        if path is None:
            start = self.last_dir or os.path.dirname(self.path)
            suggested = os.path.splitext(os.path.basename(self.path))[0] + "_edit.nif"
            try:
                path = filedialog.asksaveasfilename(
                    parent=self.root, title="Save %s as" % os.path.basename(self.path),
                    initialdir=start, initialfile=suggested, defaultextension=".nif",
                    filetypes=[("NIF mesh", "*.nif"), ("All files", "*.*")], confirmoverwrite=True)
            except Exception as exc:                       # a dialog problem must not look like a save
                self.status.configure(text="save as failed to open a dialog: %s" % exc)
                return
            if not path:
                self.status.configure(text="save as canceled - nothing written")
                return
        path = os.path.abspath(path)
        if not path.lower().endswith(".nif"):
            path += ".nif"
        self.last_dir = os.path.dirname(path)
        save_state(last_save_dir=self.last_dir)
        self._write(path, live=True, take_over=True)

    def _write(self, out_path, live=True, take_over=False):
        d = os.path.dirname(os.path.abspath(out_path))
        if d and not os.path.isdir(d):
            os.makedirs(d)
        data = self.nif.serialize()
        with open(out_path, "wb") as fh:
            fh.write(data)
        # verify by re-reading: a written file that does not parse is worse than no write at all
        try:
            back = nv.Nif(open(out_path, "rb").read())
            bad = [s.name for s in back.shapes() if not s.ok]
        except Exception as exc:
            back, bad = None, ["re-read failed: %s" % exc]
        if take_over and not bad:
            # generic Save As behavior: the document is now this file, so Save follows it and Reset means "as saved"
            self.path = out_path
            self.live = True
            self.pristine_bytes = data
            self.pristine = nv.Nif(data)
            self.backup_path = self.make_backup()
            self.log_path = os.path.splitext(self.path)[0] + ".editlog.json"
            self.title_label.configure(text=os.path.basename(self.path))
            self.path_label.configure(text=self.path)
            self.root.title("0cto shape studio - %s" % os.path.basename(self.path))
        took = "  <- now editing this file" if take_over and not bad else ""
        self.status.configure(text="written %s%s%s (%d bytes, %d shape(s))%s%s" % (
            out_path,
            "  <- live file, refresh NifSkope" if (live and not take_over) else "",
            took,
            len(data), len(back.shapes()) if back is not None else 0,
            "" if not bad else "  !! %s" % "; ".join(bad[:3]),
            "\nstaged operations were NOT saved - press Apply staged ops first" if self.staged else
            ("\nbackup of the file as loaded: %s" % os.path.basename(self.backup_path))
            + "   |   refresh NifSkope yourself (File > Reload, or your hotkey)"))
        log("save %s -> %s ok=%s shapes=%d live=%s take_over=%s" % (
            self.path, out_path, not bad, len(back.shapes()) if back is not None else -1, live, take_over))

    def run(self):
        self.render()
        self.root.mainloop()


def main(argv):
    paths = [a for a in argv[1:] if not a.startswith("--")]
    if "--check" in argv:
        log("check: argv=%r" % (argv,))
        print("check ok: %d path argument(s): %r" % (len(paths), paths))
        return 0
    if not paths:
        print("usage: nif_studio.py <file.nif> [--out DIR | --save-copy] [--config plan.tsv]")
        print("  default: the file is backed up to <stem>.bak.nif on load and Save writes the SAME file,")
        print("           so a NifSkope refresh shows the edit. --out DIR or --save-copy writes <stem>_edit.nif instead.")
        print("  Save never launches NifSkope; the 'Open in NifSkope' button opens the file on request.")
        print("  --no-open is accepted for old shortcuts and is now ignored.")
        return 2
    path = paths[0]
    if not os.path.isfile(path) or not path.lower().endswith(".nif"):
        print("not a usable .nif: %s" % path)
        return 2
    outdir = argv[argv.index("--out") + 1] if "--out" in argv else ""
    config = argv[argv.index("--config") + 1] if "--config" in argv else ""
    live = "--save-copy" not in argv and not outdir
    app = App(path, outdir, "--no-open" in argv, config, live=live)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
