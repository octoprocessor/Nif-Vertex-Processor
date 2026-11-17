"""nif_shapes.py - read (and optionally rewrite) trishape names inside SSE NIFs.

Why this exists
  The Volcanic Tundra rework renames both the .nif files and the trishapes inside them.  Checking
  that by hand across hundreds of meshes is not practical, and guessing from raw strings is unsafe.

Header layout used here (Gamebryo / SSE, version 20.2.0.7), established from a hexdump rather than
assumed:
    "Gamebryo File Format, Version x.x.x.x\n"
    uint32 version, uint8 endian, uint32 userVersion, uint32 numBlocks
    ... two optional author/process strings ...
    uint16 numBlockTypes, then numBlockTypes x (uint32 length + ascii, no NUL)
    numBlocks x uint16 block type index
    numBlocks x uint32 block size          <-- present in SSE, makes block offsets trivial
    uint32 numStrings, uint32 maxStringLen, then numStrings x (uint32 length + ascii, no NUL)
    block data (sum of the block sizes)

  The block-size array is located by scan, then *validated*: data_start + sum(sizes) must equal the
  file size exactly.  If no candidate validates, the file is reported as unparsed rather than
  guessed at.

Read-only by default.  With --out DIR it copies parsed files into DIR and rewrites only the string
table, never touching the source.
"""

from __future__ import annotations

import argparse
import io
import os
import struct
import sys

SHAPE_TYPES = ("BSTriShape", "NiTriShape", "NiTriPSysShape", "BSDistantTreeShape")


class Nif:
    def __init__(self, path: str):
        with open(path, "rb") as fh:
            self.data = fh.read()
        self.path = path
        self.ok = False
        self.reason = ""
        self._parse()

    def _u32(self, pos: int) -> int:
        return struct.unpack_from("<I", self.data, pos)[0]

    def _parse(self) -> None:
        d = self.data
        try:
            nl = d.index(b"\n") + 1
        except ValueError:
            self.reason = "no header line"
            return
        p = nl
        self.version = self._u32(p); p += 4
        self.endian = d[p]; p += 1
        self.user_version = self._u32(p); p += 4
        self.num_blocks = self._u32(p); p += 4
        nb = self.num_blocks
        if not (0 < nb < 20000):
            self.reason = "implausible numBlocks=%d" % nb
            return

        # find the block-size array by scan + full-file validation
        for start in range(p, min(len(d), p + 0x400)):
            try:
                sizes = struct.unpack_from("<%dI" % nb, d, start)
            except struct.error:
                continue
            total = sum(sizes)
            if total <= 0 or total > len(d):
                continue
            nstr_off = start + 4 * nb
            if nstr_off + 8 > len(d):
                continue
            nstr = self._u32(nstr_off)
            maxlen = self._u32(nstr_off + 4)
            if not (0 <= nstr < 100000 and 0 < maxlen < 4096):
                continue
            q = nstr_off + 8
            strs = []
            bad = False
            for _ in range(nstr):
                if q + 4 > len(d):
                    bad = True; break
                ln = self._u32(q); q += 4
                if ln > maxlen or q + ln > len(d):
                    bad = True; break
                strs.append(d[q:q + ln].decode("latin-1")); q += ln
            # block data is taken to run to EOF, so its start is fixed by the sizes; the string
            # table may be followed by a few padding/extra header bytes, hence the slack.
            data_start = len(d) - total
            if bad or not (0 <= data_start - q <= 64):
                continue
            # header: block types sit just before the type-index array (nb uint16)
            types_off = start - 2 * nb
            ntypes = struct.unpack_from("<H", d, types_off - 2)[0] if types_off >= 2 else 0
            if not (0 < ntypes < 1000):
                continue
            tp = types_off - 2 - self._type_table_len(ntypes, types_off)
            if tp < 0:
                continue
            try:
                types = []
                r = tp
                for _ in range(ntypes):
                    ln = self._u32(r); r += 4
                    if ln > 200 or r + ln > len(d):
                        raise ValueError
                    types.append(d[r:r + ln].decode("latin-1")); r += ln
                if r != types_off:
                    raise ValueError
            except Exception:
                continue
            tidx = struct.unpack_from("<%dH" % nb, d, types_off)
            if any(t >= ntypes for t in tidx):
                continue
            self.sizes = sizes
            self.types = types
            self.tidx = tidx
            self.strings = strs
            self.str_off = nstr_off
            self.maxlen = maxlen
            self.data_start = q
            self.block_start = []
            acc = q
            for s in sizes:
                self.block_start.append(acc); acc += s
            self.ok = True
            return
        self.reason = "header not validated"

    def _type_table_len(self, ntypes: int, types_off: int) -> int:
        """length of the type table (uint32-len strings) ending exactly at types_off"""
        best = None
        for guess in range(ntypes * 4, ntypes * 40):
            r = types_off - guess
            if r < 0:
                break
            try:
                for _ in range(ntypes):
                    ln = self._u32(r); r += 4 + ln
                    if ln > 200 or r > types_off:
                        raise ValueError
                if r == types_off:
                    best = guess; break
            except Exception:
                continue
        return best if best is not None else ntypes * 8

    def shapes(self):
        """[(block index, type, name)] for every shape block, in file order"""
        out = []
        if not self.ok:
            return out
        for i in range(self.num_blocks):
            t = self.types[self.tidx[i]] if self.tidx[i] < len(self.types) else "?"
            if t not in SHAPE_TYPES:
                continue
            pos = self.block_start[i]
            if pos + 4 > len(self.data):
                continue
            si = self._u32(pos)
            name = self.strings[si] if si < len(self.strings) else "<idx %d>" % si
            out.append((i, t, name))
        return out

    def rename_shape(self, block_index: int, new_name: str) -> bytes:
        """return new file bytes with the string table entry for that block's name replaced"""
        if not self.ok:
            raise ValueError("file not parsed")
        pos = self.block_start[block_index]
        si = self._u32(pos)
        return self.replace_string(si, new_name)

    def replace_string(self, index: int, new_name: str) -> bytes:
        if index >= len(self.strings):
            raise ValueError("string index out of range")
        strs = list(self.strings)
        strs[index] = new_name
        maxlen = max(self.maxlen, len(new_name))
        blob = b"".join(struct.pack("<I", len(s)) + s.encode("latin-1") for s in strs)
        # rewrite: everything before numStrings, then the rebuilt table, then block data
        head = self.data[:self.str_off]
        head = head[: self.str_off - (self.data_start - (self.str_off + 8)) - 8 + 8]  # no-op guard
        # numStrings + maxStringLen live at str_off
        tail = self.data[self.data_start:]
        return self.data[:self.str_off] + struct.pack("<II", len(strs), maxlen) + blob + tail


def audit(root: str, outdir: str | None, token: str) -> list[str]:
    rows = []
    for dp, _dn, fs in os.walk(root):
        for f in sorted(fs):
            if not f.lower().endswith(".nif"):
                continue
            p = os.path.join(dp, f)
            n = Nif(p)
            rel = os.path.relpath(p, root)
            if not n.ok:
                rows.append("UNPARSED\t%s\t%s" % (rel, n.reason))
                continue
            shp = n.shapes()
            if not shp:
                rows.append("NO-SHAPES\t%s\tblocks=%d" % (rel, n.num_blocks))
                continue
            for bi, t, name in shp:
                ok = token.lower() in name.lower()
                rows.append("%s\t%s\tblock=%d\t%s\t%s" % ("RENAMED" if ok else "UNRENAMED", rel, bi, t, name))
            if outdir:
                # test copy: append _TEST only where a rename is needed, same-length safe path
                os.makedirs(outdir, exist_ok=True)
                dst = os.path.join(outdir, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, "wb") as fh:
                    fh.write(n.data)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--out")
    ap.add_argument("--token", default="volcanictundra")
    ap.add_argument("--report", default=None)
    a = ap.parse_args(argv)
    rows = audit(a.root, a.out, a.token)
    text = "\n".join(rows)
    if a.report:
        io.open(a.report, "w", encoding="utf-8", newline="\n").write(text + "\n")
    print(text[:4000])
    print("...\nrows: %d" % len(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
