#!/usr/bin/env python
"""nif_weld_uv_gui.py -- the front end for welding vertices and rebuilding a UV map on one .nif.

READS : the .nif named on the command line.
WRITES: a NEW .nif under the output folder on Write (default D:\\0cto-AIO-Staging\\_upscaled_nifs\\weld_uv), and
        one log line per action to _recon\\nif_weld_uv_gui.log. The file you opened is never modified.

Same library as the command line (`nif_weld_uv.py`), so the weld, the flattening, the relaxation and the
size calibration are one implementation. The window exists so a single mesh can be opened from the right-click
menu, looked at, and written without remembering flags.

`--check` logs its argv and exits, which is how the shell command itself is tested.
"""
import os
import sys
import tkinter
from tkinter import messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nif_vertex as nv                                                     # noqa: E402
import nif_weld_uv as wu                                                    # noqa: E402

LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_recon", "nif_weld_uv_gui.log")
BG, PANEL, FG, MUTED, ACCENT = "#1e1e1e", "#252526", "#e6e6e6", "#9a9a9a", "#4a9eff"


def log(line):
    import datetime
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write("%s\t%s\n" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), line))
    except OSError:
        pass


class App(object):
    def __init__(self, path, outdir="", no_open=False, config=""):
        self.path = os.path.abspath(path)
        self.outdir = outdir                      # empty means: beside the source, named <name>_weld.nif
        self.no_open = no_open
        self.plan_rows = []
        if config:
            self.plan_rows, problems = nv.load_shape_plan(config)
            for prob in problems:
                log("plan: %s" % prob)
        self.nif = None
        self.shapes = []
        self.plans = {}
        self.per_shape = {}                       # shape index -> settings that apply to that row only
        self.result = None

        self.root = tkinter.Tk()
        self.root.title("0cto weld + UV - %s" % os.path.basename(self.path))
        self.root.configure(bg=BG)
        self.root.geometry("1060x600")
        self.root.minsize(820, 460)

        head = tkinter.Frame(self.root, bg=PANEL)
        head.pack(fill="x")
        tkinter.Label(head, text=os.path.basename(self.path), bg=PANEL, fg=FG,
                      font=("Segoe UI", 11, "bold")).pack(side="left", padx=12, pady=8)
        tkinter.Label(head, text=self.path, bg=PANEL, fg=MUTED).pack(side="left", padx=6)

        mid = tkinter.Frame(self.root, bg=BG)
        mid.pack(fill="both", expand=True, padx=12, pady=(10, 0))
        cols = ("shape", "uv", "verts", "unique", "tris", "dropped", "spread", "band", "mean")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended", height=9)
        for c, width, title in (("shape", 190, "shape"), ("uv", 84, "uv"), ("verts", 60, "verts"), ("unique", 70, "unique"),
                                ("tris", 70, "tris"), ("dropped", 70, "dropped"),
                                ("spread", 130, "L/T spread"), ("band", 130, "5-95% band"),
                                ("mean", 110, "mean (kept)")):
            self.tree.heading(c, text=title)
            self.tree.column(c, width=width, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        sb.pack(side="left", fill="y")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<Double-1>", self._on_double_click)
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tkinter.TclError:
            pass
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG, rowheight=22)
        style.configure("Treeview.Heading", background=BG, foreground=ACCENT)

        opts = tkinter.Frame(self.root, bg=BG)
        opts.pack(fill="x", padx=12, pady=8)
        tkinter.Label(opts, text="uv", bg=BG, fg=MUTED).pack(side="left", padx=(0, 4))
        self.uv = tkinter.StringVar(value="unwrap")
        for value in ("unwrap", "planar", "keep"):
            tkinter.Radiobutton(opts, text=value, value=value, variable=self.uv, bg=BG, fg=FG,
                                selectcolor=PANEL, activebackground=BG, activeforeground=FG,
                                command=self.preview).pack(side="left", padx=(0, 6))
        tkinter.Label(opts, text="axis", bg=BG, fg=MUTED).pack(side="left", padx=(6, 4))
        self.axis = tkinter.StringVar(value="y")
        tkinter.OptionMenu(opts, self.axis, "x", "y", "z", command=lambda *_: self.preview()).configure(
            bg=PANEL, fg=FG, highlightbackground=BG)
        opts.winfo_children()[-1].pack(side="left")
        tkinter.Label(opts, text="epsilon", bg=BG, fg=MUTED).pack(side="left", padx=(10, 4))
        self.eps = tkinter.StringVar(value="0.0001")
        tkinter.Entry(opts, textvariable=self.eps, width=9, bg=PANEL, fg=FG, insertbackground=FG).pack(side="left")
        tkinter.Label(opts, text="relax", bg=BG, fg=MUTED).pack(side="left", padx=(10, 4))
        self.relax = tkinter.IntVar(value=120)
        tkinter.Spinbox(opts, from_=0, to=2000, increment=40, width=6, textvariable=self.relax,
                        command=self.preview, bg=PANEL, fg=FG, insertbackground=FG).pack(side="left")
        tkinter.Button(opts, text="Preview / recalculate", command=self.preview, bg=PANEL, fg=FG,
                       activebackground=ACCENT).pack(side="right", padx=(6, 0))
        tkinter.Button(opts, text="Write", command=self.write, bg=ACCENT, fg="#101010",
                       activebackground="#7ab8ff").pack(side="right")
        tkinter.Button(opts, text="Select all rows", command=self.select_all, bg=PANEL, fg=FG,
                       activebackground=ACCENT).pack(side="right", padx=(0, 6))

        self.hint = tkinter.Label(
            self.root, bg=BG, fg=MUTED, justify="left", anchor="w", wraplength=1000,
            text="Preview / recalculate rebuilds the table from the settings above and writes nothing: "
                 "unique = vertices left after welding, L/T spread = texels per unit before -> after, "
                 "band = the 5-95% range of that (how even the map is), mean = apparent texture size, which the "
                 "size calibration keeps. Write saves a new <name>_weld.nif beside the source.  "
                 "Double-click a row to give that shape its own uv / axis / epsilon / relax (or to keep it "
                 "unchanged) without touching the others; a uv with a * is overridden.")
        self.hint.pack(fill="x", padx=12, pady=(0, 2))
        for var in (self.eps, self.relax, self.uv, self.axis):
            try:
                var.trace_add("write", lambda *_: self.preview())
            except AttributeError:
                pass
        self.status = tkinter.Label(self.root, text="", bg=PANEL, fg=FG, justify="left", anchor="w",
                                    wraplength=1030)
        self.status.pack(fill="x", side="bottom", ipady=6)

    def load(self):
        with open(self.path, "rb") as fh:
            self.nif = nv.Nif(fh.read())
        if not self.nif.is_sse:
            raise nv.NifError("this is not a Skyrim SE NIF (version 0x%X, userVersion %d)"
                              % (self.nif.version, self.nif.user_version))
        self.shapes = list(self.nif.shapes())
        self.preview()                                  # fills the rows, then select them all
        self.tree.selection_set([str(s.index) for s in self.shapes if str(s.index) in
                                 set(self.tree.get_children())])
        log("loaded %s: %d shape(s)" % (self.path, len(self.shapes)))

    def chosen(self):
        return [s for s in self.shapes if str(s.index) in self.tree.selection()]

    def _settings(self, sh):
        """Kept for callers that only want the plan's view."""
        return self._resolve(sh)

    def _resolve(self, sh):
        """A row's own settings, then its plan line, then the global controls. None means keep this shape."""
        override = dict(self.per_shape.get(sh.index, {}))
        if override.get("keep"):
            return None
        if self.plan_rows:
            base = nv.plan_for(sh.name, self.plan_rows)
            if base is None or base.get("skip") or base.get("keep") is True:
                return None
            merged = dict(base)
            merged.update(override)
            return merged
        return override

    def _plan(self, sh):
        st = self._settings(sh)
        if st is None:
            return None
        try:
            eps = float(st.get("epsilon", self.eps.get()))
        except (TypeError, ValueError):
            eps = 1e-4
        try:
            relax = int(st.get("relax", self.relax.get()))
        except (TypeError, ValueError):
            relax = 1200
        return wu.plan_shape(sh, eps, st.get("uv", self.uv.get()), st.get("axis", self.axis.get()),
                             None, None, st.get("normals", "average"), st.get("tangents", "keep"), relax,
                             st.get("color_merge", "mean"), st.get("alpha_merge", "median"))

    def preview(self):
        # Any settings change rebuilds the table, so the selection has to survive it: without this, changing one
        # setting silently unselects every row and Write then reports "nothing selected".
        keep = set(self.tree.selection())
        for row in self.tree.get_children():
            self.tree.delete(row)
        self.plans = {}
        for sh in self.shapes:
            if not sh.ok:
                self.tree.insert("", "end", iid=str(sh.index),
                                 values=(sh.name, "-", sh.vert, "-", sh.tri, "-", sh.reason, "", ""))
                continue
            plan = self._plan(sh)
            if plan is None:
                self.tree.insert("", "end", iid=str(sh.index),
                                 values=(sh.name, "keep", sh.vert, "-", sh.tri, "-", "kept by this row or the plan",
                                         "", ""))
                continue
            if not plan["ok"]:
                self.tree.insert("", "end", iid=str(sh.index),
                                 values=(sh.name, self._uv_label(sh), sh.vert, "-", sh.tri, "-", plan["reason"],
                                         "", ""))
                continue
            self.plans[sh.index] = plan
            b, a = plan["before"], plan["after"]
            self.tree.insert("", "end", iid=str(sh.index), values=(
                sh.name, self._uv_label(sh), plan["verts"], plan["unique"], plan["tris_after"], plan["dropped"],
                ("%.2fx -> %.2fx" % (b["spread"], a["spread"])) if (b and a) else "-",
                ("%.0f-%.0f -> %.0f-%.0f" % (b["p05"], b["p95"], a["p05"], a["p95"])) if (b and a) else "-",
                ("%.0f -> %.0f" % (b["mean"], a["mean"])) if (b and a) else "-"))
        still = [i for i in keep if i in set(self.tree.get_children())]
        if still:
            self.tree.selection_set(still)
        total_merged = sum(p["merged"] for p in self.plans.values())
        self.status.configure(text="Table recalculated - nothing written.  %d shape(s), %d vertices would merge. "
                                   "L/T is texels per unit: the 5-95%% band is"
                                   " how even the map is, and the mean is the apparent texture size, which the "
                                   "calibration keeps.  Nothing written yet."
                                   % (len(self.plans), total_merged))

    def write(self):
        picked = [s for s in self.chosen() if s.index in self.plans]
        if not picked:
            self.status.configure(text="nothing selected, or nothing selected could be planned")
            return None
        out_path, problem = nv.output_path_for(self.path, "_weld", self.outdir)
        if problem:
            self.status.configure(text=problem)
            log("refused: %s" % problem)
            return None
        d = os.path.dirname(os.path.abspath(out_path))
        if d and not os.path.isdir(d):
            os.makedirs(d)
        records = []
        for sh in picked:
            plan = self.plans[sh.index]
            if sh.type != "BSTriShape":
                self.status.configure(text="%s is a %s - segment data would need remapping; skipped"
                                           % (sh.name, sh.type))
                continue
            try:
                rec = wu.apply_shape(sh, plan, wu.read_vertices(sh), "average", "keep")
                records.append(rec)
            except Exception as exc:
                self.status.configure(text="failed on %s: %s" % (sh.name, exc))
                log("write failed %s %s: %s" % (self.path, sh.name, exc))
                return None
        if not records:
            return None
        self.nif.save(out_path)
        self.result = records
        opened = "" if self.no_open else nv.open_in_nifskope(out_path)
        skipped = self._selection_note()
        self.status.configure(text="written %s%s\n%s%s" % (
            out_path, "  (opened in NifSkope)" if opened == "opened" else ("  " + opened if opened else ""),
            " | ".join("%s %d->%d verts" % (r["shape"], r["verts_before"], r["verts_after"]) for r in records),
            ("\n!! " + skipped) if skipped else ""))
        if skipped:
            log("write %s: PARTIAL - %s" % (self.path, skipped))
        log("write %s -> %s shapes=%d merged=%d" % (self.path, out_path, len(records),
                                                    sum(r["merged"] for r in records)))
        return records

    def select_all(self):
        """Write only touches SELECTED rows, and a plain click collapses the selection to one row."""
        self.tree.selection_set([i for i in self.tree.get_children()])
        self.status.configure(text="all %d row(s) selected" % len(self.tree.get_children()))

    def _selection_note(self):
        inside = [s for s in self.shapes if str(s.index) in set(self.tree.get_children())]
        picked = self.chosen()
        missed = [s for s in inside if s not in picked]
        if not missed:
            return ""
        names = ", ".join(s.name for s in missed[:6]) + (" ..." if len(missed) > 6 else "")
        return ("%d of %d listed shape(s) are NOT selected, so they will not be written: %s"
                % (len(missed), len(inside), names))

    def _uv_label(self, sh):
        st = self._resolve(sh) or {}
        label = str(st.get("uv", self.uv.get()))
        if sh.index in self.per_shape:
            label += " *"
        return label

    def _on_double_click(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self.edit_row(int(row))

    def edit_row(self, index):
        """A small dialog for ONE shape's settings, so a weld can differ row by row."""
        sh = [s for s in self.shapes if s.index == index][0]
        st = self._resolve(sh) or {}
        dlg = tkinter.Toplevel(self.root)
        dlg.title("%s - this shape only" % sh.name)
        dlg.configure(bg=BG)
        dlg.transient(self.root)
        dlg.resizable(False, False)
        uv = tkinter.StringVar(value=str(st.get("uv", self.uv.get())))
        axis = tkinter.StringVar(value=str(st.get("axis", self.axis.get())))
        eps = tkinter.StringVar(value=str(st.get("epsilon", self.eps.get())))
        try:
            relax = tkinter.IntVar(value=int(st.get("relax", self.relax.get())))
        except (TypeError, ValueError):
            relax = tkinter.IntVar(value=self.relax.get())
        keep = tkinter.BooleanVar(value=bool(self.per_shape.get(index, {}).get("keep")))
        row = tkinter.Frame(dlg, bg=BG)
        row.pack(fill="x", padx=14, pady=(14, 4))
        tkinter.Label(row, text="uv", bg=BG, fg=MUTED).pack(side="left", padx=(0, 4))
        for value in ("unwrap", "planar", "keep"):
            tkinter.Radiobutton(row, text=value, value=value, variable=uv, bg=BG, fg=FG, selectcolor=PANEL,
                                activebackground=BG, activeforeground=FG).pack(side="left", padx=(0, 6))
        tkinter.Label(row, text="axis", bg=BG, fg=MUTED).pack(side="left", padx=(10, 4))
        tkinter.OptionMenu(row, axis, "x", "y", "z").configure(bg=PANEL, fg=FG, highlightbackground=BG)
        row.winfo_children()[-1].pack(side="left")
        row2 = tkinter.Frame(dlg, bg=BG)
        row2.pack(fill="x", padx=14, pady=4)
        tkinter.Label(row2, text="epsilon", bg=BG, fg=MUTED).pack(side="left", padx=(0, 4))
        tkinter.Entry(row2, textvariable=eps, width=9, bg=PANEL, fg=FG, insertbackground=FG).pack(side="left")
        tkinter.Label(row2, text="relax", bg=BG, fg=MUTED).pack(side="left", padx=(10, 4))
        tkinter.Spinbox(row2, from_=0, to=2000, increment=40, width=6, textvariable=relax, bg=PANEL, fg=FG,
                        insertbackground=FG).pack(side="left")
        tkinter.Checkbutton(row2, text="keep this shape as it is", variable=keep, bg=BG, fg=FG,
                            selectcolor=PANEL, activebackground=BG, activeforeground=FG).pack(side="left", padx=(14, 0))
        row3 = tkinter.Frame(dlg, bg=BG)
        row3.pack(fill="x", padx=14, pady=(8, 14))

        def apply_here():
            if keep.get():
                settings = {"keep": True}
            else:
                settings = {"uv": uv.get(), "axis": axis.get(), "epsilon": eps.get(), "relax": str(relax.get())}
            self.per_shape[index] = settings
            dlg.destroy()
            self.preview()

        def use_global():
            self.per_shape.pop(index, None)
            dlg.destroy()
            self.preview()

        tkinter.Button(row3, text="Use these for this shape", command=apply_here, bg=ACCENT, fg="#101010",
                       activebackground="#7ab8ff").pack(side="left")
        tkinter.Button(row3, text="Follow the global setting", command=use_global, bg=PANEL, fg=FG,
                       activebackground=ACCENT).pack(side="left", padx=6)
        tkinter.Button(row3, text="Cancel", command=dlg.destroy, bg=PANEL, fg=FG,
                       activebackground=ACCENT).pack(side="right")
        dlg.lift()
        return dlg

    def run(self):
        self.root.mainloop()


def main(argv):
    paths = [a for a in argv[1:] if not a.startswith("--")]
    if "--check" in argv:
        log("check: argv=%r" % (argv,))
        print("check ok: %d path argument(s): %r" % (len(paths), paths))
        return 0
    if not paths:
        print("usage: nif_weld_uv_gui.py <file.nif> [--out DIR]")
        return 2
    path = paths[0]
    if not os.path.isfile(path) or not path.lower().endswith(".nif"):
        log("not a usable .nif: %s" % path)
        return 2
    outdir = argv[argv.index("--out") + 1] if "--out" in argv else ""
    config = argv[argv.index("--config") + 1] if "--config" in argv else ""
    app = App(path, outdir, "--no-open" in argv, config)
    try:
        app.load()
    except Exception as exc:
        log("load failed %s: %s" % (path, exc))
        messagebox.showerror("0cto weld + UV", "%s\n\n%s" % (os.path.basename(path), exc))
        return 1
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
