#!/usr/bin/env python
"""nif_vertex_gui.py -- the front end for a single .nif opened from the right-click menu.

READS : the .nif named on the command line, plus shapes/alpha/property state from nif_vertex.py.
WRITES: a NEW .nif under the output folder (default D:\\0cto-AIO-Staging\\_upscaled_nifs\\vertex_alpha_mask),
        and a log line per action to _recon\\nif_vertex_gui.log. It never edits the file you opened.

This exists so "right-click a .nif -> Vertex alpha mask..." does something useful with one file: pick the
shapes, pick the mode, look at the numbers, then write. It is a view over the same library the command line
uses (vertex_alpha_mask.py for geometry, nif_vertex.py for reading/writing), so there is one implementation of
every rule -- including the merge (a mask may only darken) and the all-black question.

`--check` logs the argv it was handed and exits without opening a window; the registry installer uses that to
prove the shell command's exe path, quoting and %1 substitution all work.
"""
import io
import os
import sys
import tkinter
from tkinter import messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nif_vertex as nv                                                     # noqa: E402
import vertex_alpha_mask as vam                                             # noqa: E402

LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_recon", "nif_vertex_gui.log")

BG, PANEL, FG, MUTED, ACCENT = "#1e1e1e", "#252526", "#e6e6e6", "#9a9a9a", "#4a9eff"


def log(line):
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write("%s\t%s\n" % (__import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"), line))
    except OSError:
        pass


class App(object):
    def __init__(self, path, outdir="", no_open=False, config=""):
        self.path = os.path.abspath(path)
        self.outdir = outdir                      # empty means: beside the source, named <name>_mask.nif
        self.no_open = no_open
        self.per_shape = {}          # shape block index -> its own {'mode': .., 'buffer': .., 'merge': ..}
        self.plan_rows = []
        if config:
            self.plan_rows, problems = nv.load_shape_plan(config)
            for prob in problems:
                log("plan: %s" % prob)
        self.nif = None
        self.shapes = []
        self.rows = {}
        self.result = None

        self.root = tkinter.Tk()
        self.root.title("0cto vertex-alpha mask - %s" % os.path.basename(self.path))
        self.root.configure(bg=BG)
        self.root.geometry("980x620")
        self.root.minsize(760, 480)

        head = tkinter.Frame(self.root, bg=PANEL)
        head.pack(fill="x")
        tkinter.Label(head, text=os.path.basename(self.path), bg=PANEL, fg=FG,
                      font=("Segoe UI", 11, "bold")).pack(side="left", padx=12, pady=8)
        self.file_line = tkinter.Label(head, text=self.path, bg=PANEL, fg=MUTED)
        self.file_line.pack(side="left", padx=6)

        mid = tkinter.Frame(self.root, bg=BG)
        mid.pack(fill="both", expand=True, padx=12, pady=(10, 0))
        cols = ("shape", "mode", "ring", "verts", "tris", "stride", "colors", "alpha", "black", "kept", "props")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended", height=10)
        for c, w, t in (("shape", 170, "shape"), ("mode", 90, "mode (double-click to change)"),
                        ("ring", 105, "1 ring (units)"),
                        ("verts", 55, "verts"), ("tris", 55, "tris"),
                        ("stride", 55, "stride"), ("colors", 60, "colors"), ("alpha", 85, "alpha property"),
                        ("black", 120, "black (this mode)"), ("kept", 55, "kept"),
                        ("props", 230, "shader plan")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        sb.pack(side="left", fill="y")
        self.tree.configure(yscrollcommand=sb.set)
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tkinter.TclError:
            pass
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG, rowheight=22)
        style.configure("Treeview.Heading", background=BG, foreground=ACCENT)
        self.ring_cache = {}
        self.join_cache = {}
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._refresh_note())
        self.tree.bind("<Double-1>", self._on_double_click)

        opts = tkinter.Frame(self.root, bg=BG)
        opts.pack(fill="x", padx=12, pady=8)
        self.mode = tkinter.StringVar(value="edges")
        for text, value in (("edges", "edges"), ("buffer", "buffer"), ("gradient", "gradient"),
                            ("white", "white"), ("none", "none")):
            tkinter.Radiobutton(opts, text=text, value=value, variable=self.mode, bg=BG, fg=FG,
                                selectcolor=PANEL, activebackground=BG, activeforeground=FG,
                                command=self.preview).pack(side="left", padx=(0, 8))
        tkinter.Label(opts, text="rings", bg=BG, fg=MUTED).pack(side="left", padx=(8, 4))
        self.buffer = tkinter.IntVar(value=1)
        tkinter.Spinbox(opts, from_=0, to=64, width=4, textvariable=self.buffer,
                        command=self.preview, bg=PANEL, fg=FG, insertbackground=FG).pack(side="left")
        self.merge = tkinter.BooleanVar(value=True)
        tkinter.Checkbutton(opts, text="keep the lower of mask and original", variable=self.merge,
                            bg=BG, fg=FG, selectcolor=PANEL, activebackground=BG, activeforeground=FG,
                            command=self.preview).pack(side="left", padx=(14, 0))
        self.join = tkinter.BooleanVar(value=False)
        tkinter.Checkbutton(opts, text="join coincident vertices (for merged shapes)", variable=self.join,
                            bg=BG, fg=FG, selectcolor=PANEL, activebackground=BG, activeforeground=FG,
                            command=self.preview).pack(side="left", padx=(10, 0))
        tkinter.Button(opts, text="Preview / recalculate", command=self.preview, bg=PANEL, fg=FG,
                       activebackground=ACCENT).pack(side="right", padx=(6, 0))
        tkinter.Button(opts, text="Save plan...", command=self.save_plan, bg=PANEL, fg=FG,
                       activebackground=ACCENT).pack(side="right", padx=(0, 6))
        tkinter.Button(opts, text="Write", command=self.write, bg=ACCENT, fg="#101010",
                       activebackground="#7ab8ff").pack(side="right")
        tkinter.Button(opts, text="Select all rows", command=self.select_all, bg=PANEL, fg=FG,
                       activebackground=ACCENT).pack(side="right", padx=(0, 6))

        self.hint = tkinter.Label(
            self.root, bg=BG, fg=MUTED, justify="left", anchor="w", wraplength=980,
            text="Preview / recalculate rebuilds the table from the settings above and writes nothing. "
                 "Per row: ring = what ONE vertex step costs this shape in world units, so buffer/gradient N "
                 "removes about N x ring of coverage (that is how two different-sized shapes are feathered to the "
                 "same width); black = vertices this mode makes fully transparent; kept = vertices left at the "
                 "artist's darker alpha. Double-click a row to give that shape its own mode (or to keep it "
                 "untouched) without changing the others. Write saves a new <name>_mask.nif beside the source.")
        self.hint.pack(fill="x", padx=12, pady=(0, 2))
        self.note = tkinter.Label(self.root, text="", bg=BG, fg=MUTED, justify="left", anchor="w")
        self.note.pack(fill="x", padx=12)
        for var in (self.mode, self.buffer, self.merge, self.join):
            try:
                var.trace_add("write", lambda *_: self.preview())
            except AttributeError:                                   # very old tkinter
                pass
        self.status = tkinter.Label(self.root, text="", bg=PANEL, fg=FG, justify="left", anchor="w",
                                    wraplength=950)
        self.status.pack(fill="x", side="bottom", padx=0, pady=0, ipady=6)

    # -- data --------------------------------------------------------------------------------
    def load(self):
        with open(self.path, "rb") as fh:
            self.nif = nv.Nif(fh.read())
        if not self.nif.is_sse:
            raise nv.NifError("this is not a Skyrim SE NIF (version 0x%X, userVersion %d)"
                              % (self.nif.version, self.nif.user_version))
        self.shapes = [s for s in self.nif.shapes()]
        self.fill_rows()
        self.tree.selection_set([str(s.index) for s in self.shapes if str(s.index) in
                                 set(self.tree.get_children())])
        self._refresh_note()
        log("loaded %s: %d shape(s)" % (self.path, len(self.shapes)))

    def fill_rows(self):
        """(Re)build the table from the current mode/rings/merge -- this is what Preview runs.

        The first version of Preview only repainted two labels, so pressing it looked like nothing happened: the
        numbers that matter are per shape, and they belong in the table where the mode's effect is visible.
        """
        keep = set(self.tree.selection())
        for row in self.tree.get_children():
            self.tree.delete(row)
        self.rows = {}
        for s in self.shapes:
            if not s.ok:
                self.tree.insert("", "end", iid=str(s.index),
                                 values=(s.name, "-", "-", s.vert, s.tri, "-", "-", "-", "-", "-", s.reason))
                continue
            ap = nv.read_alpha_property(self.nif, s)
            if ap is None:
                alpha = "none"
            elif "error" in ap:
                alpha = ap["error"]
            else:
                alpha = "0x%04X" % ap["flags"]
            alphas, edge, _dist, kept = self._mask(s)
            st = self._resolve(s)
            mode_label = "keep"
            if st is not None:
                m = st.get("mode", self.mode.get())
                try:
                    r = int(st.get("buffer", self.buffer.get()))
                except (TypeError, ValueError):
                    r = self.buffer.get()
                mode_label = m + ((" %d" % r) if m in ("buffer", "gradient") else "")
            if alphas is None:
                self.tree.insert("", "end", iid=str(s.index), values=(
                    s.name, "keep", self._ring(s), s.vert, s.tri, s.stride, "yes" if s.has_colors else "NO", alpha,
                    "-", "-", "kept"))
                continue
            black = sum(1 for a in alphas if a == 0)
            self.rows[s.index] = dict(edge=len(edge), black=black, kept=kept, verts=len(alphas),
                                      all_black=bool(alphas) and all(a == 0 for a in alphas))
            self.tree.insert("", "end", iid=str(s.index), values=(
                s.name, mode_label, self._ring(s), s.vert, s.tri, s.stride, "yes" if s.has_colors else "NO", alpha,
                "%d (%.0f%%)" % (black, 100.0 * black / max(1, len(alphas))),
                kept, vam.property_plan(self.nif, s, nv.ALPHA_FLAGS_DEFAULT)))
        still = [i for i in keep if i in set(self.tree.get_children())]
        if still:
            self.tree.selection_set(still)

    def _ring(self, sh):
        """What one buffer/gradient ring costs this shape in world units, so ring counts can be chosen by width."""
        if sh.index not in self.ring_cache:
            try:
                self.ring_cache[sh.index] = vam.median_edge_length(sh.positions(), sh.triangles())
            except Exception as exc:
                log("ring length failed for %s: %s" % (sh.name, exc))
                self.ring_cache[sh.index] = 0.0
        step = self.ring_cache[sh.index]
        return ("%.3gu" % step) if step else "-"

    def chosen(self):
        picked = [s for s in self.shapes if str(s.index) in self.tree.selection()]
        if self.plan_rows:
            picked = [s for s in picked if self._settings(s) is not None]
        return picked

    def select_all(self):
        """Select every listed row. Write only ever touches SELECTED rows, and a plain click collapses the
        selection to that one row -- which is how a seven-shape file got written with one painted shape."""
        self.tree.selection_set([i for i in self.tree.get_children()])
        self._refresh_note()
        self.status.configure(text="all %d row(s) selected" % len(self.tree.get_children()))

    def _selection_note(self):
        """What Write will NOT touch, stated before it happens."""
        listed = [s for s in self.shapes if s.ok]
        picked = self.chosen()
        inside = [s for s in listed if str(s.index) in set(self.tree.get_children())]
        missed = [s for s in inside if s not in picked]
        if not missed:
            return ""
        names = ", ".join(s.name for s in missed[:6]) + (" ..." if len(missed) > 6 else "")
        return ("%d of %d listed shape(s) are NOT selected, so they will not be written: %s"
                % (len(missed), len(inside), names))

    # -- actions -----------------------------------------------------------------------------
    def _settings(self, sh):
        """Kept for callers that only want the plan's view: this shape's line, or None when the plan keeps it."""
        return self._resolve(sh)

    def _resolve(self, sh):
        """A row's own settings, then its plan line, then the global controls. None means keep this shape."""
        override = dict(self.per_shape.get(sh.index, {}))
        if self.plan_rows:
            base = nv.plan_for(sh.name, self.plan_rows)
            if base is None or base.get("skip") or base.get("keep") is True:
                return None
            merged = dict(base)
            merged.update(override)
            return merged
        return override

    def _mask(self, sh):
        st = self._resolve(sh)
        if st is None:
            return None, set(), [], 0
        mode = st.get("mode", self.mode.get())
        if str(mode).lower() in ("keep", "skip"):
            return None, set(), [], 0
        try:
            rings = int(st.get("buffer", self.buffer.get()))
        except (TypeError, ValueError):
            rings = self.buffer.get()
        replace = (not self.merge.get()) or str(st.get("merge", "")).lower() == "replace"
        join = st.get("join", self.join.get())
        join = str(join).strip().lower() in ("1", "on", "true", "yes") if not isinstance(join, bool) else join
        return vam.final_alphas(sh, mode, rings, replace, 1e-4 if join else None)

    def _join_info(self, sh):
        """(coincident vertices, vertices that stopped being on a rim) -- what joining does to this shape.

        The second number is the one that matters: it is how much of the mask would otherwise have been painted on a
        seam between two shapes that were merged into one.
        """
        if sh.index in self.join_cache:
            return self.join_cache[sh.index]
        try:
            pos = sh.positions()
            groups, remap = vam.coincident_groups(pos)
            tris = sh.triangles()
            wtris = [(remap[a], remap[b], remap[c]) for a, b, c in tris]
            wdist = vam.ring_distance(wtris, vam.edge_vertices(wtris, len(groups)), len(groups))
            raw_edge = vam.edge_vertices(tris, len(pos))
            moved = sum(1 for i in raw_edge if wdist[remap[i]] > 0)
            info = (sum(len(g) - 1 for g in groups if len(g) > 1), moved)
        except Exception as exc:
            log("join info failed for %s: %s" % (sh.name, exc))
            info = (0, 0)
        self.join_cache[sh.index] = info
        return info

    def _refresh_note(self):
        rows, total_kept, total_black, total = [], 0, 0, 0
        for sh in self.chosen():
            if not sh.ok:
                rows.append("%s: %s" % (sh.name, sh.reason))
                continue
            alphas, edge, _dist, kept = self._mask(sh)
            if alphas is None:
                rows.append("%-22s kept - this row has its own setting or the plan keeps it" % sh.name[:22])
                continue
            total_kept += kept
            black = sum(1 for a in alphas if a == 0)
            total_black += black
            total += len(alphas)
            flag = "  ALL BLACK -> will ask" if alphas and all(a == 0 for a in alphas) else ""
            rows.append("%-22s %5d verts  %4d edge  %5d black (%.0f%%)  kept original on %d%s"
                        % (sh.name[:22], sh.vert, len(edge), black, 100.0 * black / max(1, len(alphas)),
                           kept, flag))
        if total:
            rows.append("selected: %d shape(s), %d vertices, %.0f%% black overall"
                        % (len(self.chosen()), total, 100.0 * total_black / total))
        if self.join.get() or any(isinstance(st.get("join"), (bool, str)) and
                                  str(st.get("join")).strip().lower() in ("1", "on", "true", "yes")
                                  for st in (self.per_shape.get(s.index) or {} for s in self.chosen())):
            extra = sum(self._join_info(s)[0] for s in self.chosen() if s.ok)
            off_seam = sum(self._join_info(s)[1] for s in self.chosen() if s.ok)
            rows.append("joining coincident vertices: %d duplicate vert%s, %d of them were sitting on a rim that is "
                        "really interior" % (extra, "" if extra == 1 else "s", off_seam))
        self.note.configure(text="\n".join(rows))

    def preview(self, *_):
        try:
            self.fill_rows()
            self._refresh_note()
            target = nv.output_path_for(self.path, "_mask", self.outdir)[0]
            note = self._selection_note()
            self.status.configure(text="Table recalculated from the settings above - nothing written.  "
                                        "Write would save %s%s"
                                        % (target, ("\n" + note) if note else ""))
        except Exception as exc:
            self.status.configure(text="preview failed: %s" % exc)

    def _on_double_click(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self.edit_row(int(row))

    def edit_row(self, index):
        """A small dialog for ONE shape's settings, so modes can differ row by row."""
        sh = [s for s in self.shapes if s.index == index][0]
        st = self._resolve(sh) or {}
        dlg = tkinter.Toplevel(self.root)
        dlg.title("%s - this shape only" % sh.name)
        dlg.configure(bg=BG)
        dlg.transient(self.root)
        dlg.resizable(False, False)
        mode = tkinter.StringVar(value=st.get("mode", self.mode.get()))
        try:
            rings = tkinter.IntVar(value=int(st.get("buffer", self.buffer.get())))
        except (TypeError, ValueError):
            rings = tkinter.IntVar(value=self.buffer.get())
        keep = tkinter.BooleanVar(value=st.get("mode") == "keep")
        row = tkinter.Frame(dlg, bg=BG)
        row.pack(fill="x", padx=14, pady=(14, 4))
        for value in ("edges", "buffer", "gradient", "white", "none"):
            tkinter.Radiobutton(row, text=value, value=value, variable=mode, bg=BG, fg=FG, selectcolor=PANEL,
                                activebackground=BG, activeforeground=FG).pack(side="left", padx=(0, 6))
        row2 = tkinter.Frame(dlg, bg=BG)
        row2.pack(fill="x", padx=14, pady=4)
        tkinter.Label(row2, text="rings", bg=BG, fg=MUTED).pack(side="left", padx=(0, 4))
        tkinter.Spinbox(row2, from_=0, to=64, width=4, textvariable=rings, bg=PANEL, fg=FG,
                        insertbackground=FG).pack(side="left")
        tkinter.Checkbutton(row2, text="keep this shape untouched", variable=keep, bg=BG, fg=FG,
                            selectcolor=PANEL, activebackground=BG, activeforeground=FG).pack(side="left", padx=(14, 0))
        row3 = tkinter.Frame(dlg, bg=BG)
        row3.pack(fill="x", padx=14, pady=(8, 14))

        def apply_here():
            settings = {"mode": "keep"} if keep.get() else {"mode": mode.get(), "buffer": str(rings.get())}
            self.per_shape[index] = settings
            dlg.destroy()
            self.fill_rows()
            self._refresh_note()

        def use_global():
            self.per_shape.pop(index, None)
            dlg.destroy()
            self.fill_rows()
            self._refresh_note()

        tkinter.Button(row3, text="Use these for this shape", command=apply_here, bg=ACCENT, fg="#101010",
                       activebackground="#7ab8ff").pack(side="left")
        tkinter.Button(row3, text="Follow the global setting", command=use_global, bg=PANEL, fg=FG,
                       activebackground=ACCENT).pack(side="left", padx=6)
        tkinter.Button(row3, text="Cancel", command=dlg.destroy, bg=PANEL, fg=FG,
                       activebackground=ACCENT).pack(side="right")
        dlg.lift()
        return dlg

    def plan_lines(self):
        """The recipe as plan rows: one line per shape, `skip` for a shape this window leaves alone.

        Written from what each row would actually do, not from the overrides alone, so the file reproduces this
        exact result when it is handed back with --config. Names are the key, which is the point: a re-merged or
        re-exported mesh keeps its shape names but not its block indices, so a per-row setting would be lost while
        a plan still finds the right shape.
        """
        lines = ["# per-shape plan written from the mask window for %s" % os.path.basename(self.path),
                 "# shape\tsettings"]
        for sh in self.shapes:
            st = self._resolve(sh)
            if st is None or str(st.get("mode", "")).lower() in ("keep", "skip"):
                lines.append("%s\tskip" % sh.name)
                continue
            mode = str(st.get("mode", self.mode.get()))
            try:
                rings = int(st.get("buffer", self.buffer.get()))
            except (TypeError, ValueError):
                rings = self.buffer.get()
            replace = (not self.merge.get()) or str(st.get("merge", "")).lower() == "replace"
            join = st.get("join", self.join.get())
            join = str(join).strip().lower() in ("1", "on", "true", "yes") if not isinstance(join, bool) else join
            lines.append("%s\tmode=%s buffer=%d merge=%s join=%s"
                         % (sh.name, mode, rings, "replace" if replace else "keep", "on" if join else "off"))
        return lines

    def save_plan(self, path=""):
        if not path:
            from tkinter import filedialog
            stem = os.path.splitext(os.path.basename(self.path))[0]
            path = filedialog.asksaveasfilename(
                title="Save this recipe as a plan", defaultextension=".tsv",
                initialdir=os.path.dirname(os.path.abspath(self.path)),
                initialfile="%s_mask_plan.tsv" % stem,
                filetypes=[("plan (TSV)", "*.tsv"), ("all files", "*.*")])
            if not path:
                return ""
        lines = self.plan_lines()
        with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
        self.status.configure(text="plan saved to %s\n%s" % (path, "\n".join(lines[:6])))
        log("plan saved: %s (%d line(s))" % (path, len(lines)))
        return path

    def prompt_all_black(self, info):
        """Ask about an all-black result, parented to this window.

        nv.ask_all_black builds its own Tk root, which is the wrong thing to do inside a running mainloop; this
        owns a Toplevel instead and waits for it. No answer (closed window, timeout) means skip, so an
        unanswered prompt never writes anything.
        """
        dlg = tkinter.Toplevel(self.root)
        dlg.title("All-black vertex-alpha mask")
        dlg.resizable(False, False)
        answer = {"v": "skip"}
        try:
            dlg.transient(self.root)
            dlg.grab_set()
        except Exception:
            pass
        frame = tkinter.Frame(dlg, padx=16, pady=14, bg=PANEL)
        frame.pack(fill="both", expand=True)
        tkinter.Label(
            frame, bg=PANEL, fg=FG, justify="left", anchor="w",
            text=("%s\nshape: %s (%d vertices)\n\n"
                  "The computed mask is BLACK on every vertex, which makes the whole surface vanish.\n"
                  "These tools only remove coverage, so that is a mistake rather than an intention.\n"
                  "Flip it to white and write?\n\n"
                  "To remove a shape entirely, cull it in PGPatcher instead.")
                 % (os.path.basename(info.get("file", "")), info.get("shape", "?"), info.get("verts", 0))
        ).pack(fill="x")
        row = tkinter.Frame(frame, bg=PANEL)
        row.pack(fill="x", pady=(14, 0))

        def done(value):
            answer["v"] = value
            dlg.destroy()

        tkinter.Button(row, text="Flip to white and write", width=24, bg=ACCENT, fg="#101010",
                       command=lambda: done("flip")).pack(side="left")
        tkinter.Button(row, text="Skip this shape", width=16, bg=PANEL, fg=FG,
                       command=lambda: done("skip")).pack(side="left", padx=6)
        dlg.protocol("WM_DELETE_WINDOW", lambda: done("skip"))
        dlg.lift()
        self.root.wait_window(dlg)
        log("all-black prompt for %s: %s" % (info.get("shape"), answer["v"]))
        return answer["v"]

    def write(self):
        picked = self.chosen()
        if not picked:
            self.status.configure(text="nothing selected")
            return None
        out_path, problem = nv.output_path_for(self.path, "_mask", self.outdir)
        if problem:
            self.status.configure(text=problem)
            log("refused: %s" % problem)
            return None
        ids = set(str(s.index) for s in picked)
        mode, buf, merge = self.mode.get(), max(0, int(self.buffer.get())), self.merge.get()

        def alphas_for(sh):
            # one source of truth: _mask already returns None for a row that is kept, whether by its own
            # setting or by the plan, so the writer cannot disagree with the table.
            if str(sh.index) not in ids or not sh.ok:
                return None
            return self._mask(sh)[0]

        try:
            res = nv.apply_file(self.path, out_path, alphas_for, nv.ALPHA_FLAGS_DEFAULT,
                                True, True, True, "ask", self.prompt_all_black)
        except Exception as exc:
            self.status.configure(text="write failed: %s" % exc)
            log("write failed %s: %s" % (self.path, exc))
            return None
        self.result = res
        if res["ok"]:
            opened = "" if self.no_open else nv.open_in_nifskope(out_path)
            skipped = self._selection_note()
            self.status.configure(
                text="written %s%s\n%d -> %d bytes, %d of %d listed shape(s) written%s\n%s"
                     % (out_path, "  (opened in NifSkope)" if opened == "opened" else ("  " + opened if opened else ""),
                        res["src_bytes"], res["bytes"], len(res["records"]),
                        len([s for s in self.shapes if s.ok]),
                        ("\n!! " + skipped) if skipped else " (every listed shape)",
                        " | ".join(res["notes"][:3])))
            if skipped:
                log("write %s: PARTIAL - %s" % (self.path, skipped))
        else:
            self.status.configure(text="not written: %s" % "; ".join(res["problems"]))
        log("write %s -> %s ok=%s problems=%s" % (self.path, out_path, res["ok"], res["problems"]))
        return res

    def run(self):
        self.preview()
        self.root.mainloop()


def main(argv):
    paths = [a for a in argv[1:] if not a.startswith("--")]
    if "--check" in argv:
        log("check: argv=%r" % (argv,))
        print("check ok: %d path argument(s): %r" % (len(paths), paths))
        return 0
    if not paths:
        print("usage: nif_vertex_gui.py <file.nif> [--out DIR]")
        return 2
    path = paths[0]
    if not os.path.isfile(path):
        log("missing file: %s" % path)
        return 2
    if not path.lower().endswith(".nif"):
        log("not a .nif: %s" % path)
        return 2
    outdir = ""
    if "--out" in argv:
        outdir = argv[argv.index("--out") + 1]
    config = argv[argv.index("--config") + 1] if "--config" in argv else ""
    app = App(path, outdir, "--no-open" in argv, config)
    try:
        app.load()
    except Exception as exc:
        log("load failed %s: %s" % (path, exc))
        messagebox.showerror("0cto vertex-alpha mask", "%s\n\n%s" % (os.path.basename(path), exc))
        return 1
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
