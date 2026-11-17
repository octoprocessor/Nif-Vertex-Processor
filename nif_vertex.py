#!/usr/bin/env python
"""nif_vertex.py -- read and WRITE per-vertex color alpha on Skyrim SE BSTriShape blocks, and apply the
two shader changes that make a vertex-alpha mask actually render (NiAlphaProperty + BSLightingShaderProperty).

READS : .nif files, version 20.2.0.7 / userVersion 12 (Skyrim SE, and the same layout family as FO4).
WRITES: nothing unless asked. `audit` is report-only and writes its TSV to _recon\\ by default. `apply`
        never edits a source file: it writes a new file into an output folder.

Why this file exists
--------------------
Painting alpha into a mesh means finding one byte per vertex. Doing that safely means understanding the
VertexDesc, which is where this went wrong once already: an earlier version tested `desc & 0x20` for
"has vertex colors" and therefore reported that **no** shape in the AIO had colors. That was a false
negative. 0x20 is not a flag bit; it is the low nibble of the UV-offset byte.

VertexDesc, from xEdit's own definition (Core\\wbDataFormatNif.pas, wbVertexDesc / wbVertexDesc_SetValue):
the u64 is five u8s, a u16 and one more u8:

    byte 0   VF1   vertex size in 4-BYTE UNITS  (so *4 = stride in bytes)
    byte 1   VF2   low nibble = UV0 offset, high nibble = UV1 offset, in 4-byte units
    byte 2   VF3   low nibble = normal offset, high nibble = tangent offset
    byte 3   VF4   low nibble = COLOUR offset, high nibble = skinning offset
    byte 4   VF5   low nibble = landscape offset, high nibble = eye offset
    byte 5-6 VF    u16 of VF_* bit flags
    byte 7   VF8   always 0

and the VF flags are bit positions, not a mask of attribute presence:

    VF_VERTEX 1<<4   VF_UV 1<<5   VF_UV_2 1<<6   VF_NORMAL 1<<7   VF_TANGENT 1<<8
    VF_COLORS 1<<9   VF_SKINNED 1<<10   VF_LANDDATA 1<<11   VF_EYEDATA 1<<12   VF_FULLPREC 1<<14

Attribute sizes in Skyrim SE (always full precision):
    vertex  16  = float3 position + float bitangent X
    uv       4  = half2, per UV set
    normal   4  = byte3 normal + byte1 bitangent Y
    tangent  4  = byte3 tangent + byte1 bitangent Z  (only when normal is present)
    color   4  = BGRA bytes, so alpha is the FOURTH byte of the four
    skin    12  = 4 half weights + 4 byte bone indices
    eye      4  = float
    land     0  = stored outside the vertex

That gives two independent readings of the same layout -- the nibbles, and the sizes implied by the flags --
and both must equal `dataSize / numVertices`. Any disagreement is a refusal, not a guess.

Measured on MountainTrim01.nif: desc 0x0003B00007650408 -> VF1 8 (stride 32), VF2 4, VF3 0x65 (normal 5,
tangent 6), VF4 0x07 (color 7 = byte 28, skin 0), VF 0x03B0 = VERTEX|UV|NORMAL|TANGENT|COLORS, VF8 0.
32 = 16+4+4+4+4 exactly, and every nibble equals the running offset. So these meshes already carry
colors and the alpha byte sits at vertex*32 + 31.

Shader changes, from NifSkope's nif.xml bitflags (SkyrimShaderPropertyFlags1 / Flags2):
    SLSF1 bit3  Vertex_Alpha        "Enables using alpha component of vertex colors"
    SLSF1 bit26 Decal
    SLSF1 bit27 Dynamic_Decal       (Flags 1, not Flags 2 -- easy to get wrong from memory)
    SLSF2 bit0  ZBuffer_Write       -> cleared
    SLSF2 bit5  Vertex_Colors
    SLSF2 bit7  Assume_Shadowmask
NiAlphaProperty is Flags (u16) then Threshold (u8); 4333 = 0x10ED = blending on, testing off,
source SrcAlpha, destination InvSrcAlpha.

Usage
  python tools\\nif_vertex.py audit <file-or-folder> [--recurse] [--tsv out.tsv]
  python tools\\nif_vertex.py selftest
  python tools\\nif_vertex.py roundtrip <file.nif>          # prove a no-op save reproduces the bytes
"""
import argparse
import io
import os
import struct
import sys

MAGIC = b"Gamebryo File Format, Version "
VERSION_SSE = 0x14020007

VF_VERTEX, VF_UV, VF_UV_2, VF_NORMAL, VF_TANGENT = 1 << 4, 1 << 5, 1 << 6, 1 << 7, 1 << 8
VF_COLORS, VF_SKINNED, VF_LANDDATA, VF_EYEDATA, VF_FULLPREC = 1 << 9, 1 << 10, 1 << 11, 1 << 12, 1 << 14

TRISHAPE_TYPES = ("BSTriShape", "BSSubIndexTriShape", "BSDynamicTriShape", "BSSegmentedTriShape")
SHADER_TYPES = ("BSLightingShaderProperty", "BSEffectShaderProperty")

SZ_VERTEX, SZ_UV, SZ_NORMAL, SZ_TANGENT, SZ_COLOR, SZ_SKIN, SZ_EYE = 16, 4, 4, 4, 4, 12, 4

SLSF1_VERTEX_ALPHA = 1 << 3
SLSF1_DECAL = 1 << 26
SLSF1_DYNAMIC_DECAL = 1 << 27
SLSF2_ZBUFFER_WRITE = 1 << 0
SLSF2_VERTEX_COLORS = 1 << 5
SLSF2_ASSUME_SHADOWMASK = 1 << 7

SLSF1_NAMES = {SLSF1_VERTEX_ALPHA: "Vertex_Alpha", SLSF1_DECAL: "Decal",
               SLSF1_DYNAMIC_DECAL: "Dynamic_Decal"}
SLSF2_NAMES = {SLSF2_ZBUFFER_WRITE: "ZBuffer_Write", SLSF2_VERTEX_COLORS: "Vertex_Colors",
               SLSF2_ASSUME_SHADOWMASK: "Assume_Shadowmask"}

# The property state a vertex-alpha mask requires. Both are enforced on every write and then re-read to
# confirm, because a mask whose flags were not actually applied renders nothing and looks identical on disk.
MASK_SET1 = SLSF1_DECAL | SLSF1_DYNAMIC_DECAL | SLSF1_VERTEX_ALPHA
MASK_SET2 = SLSF2_ASSUME_SHADOWMASK | SLSF2_VERTEX_COLORS
MASK_CLEAR2 = SLSF2_ZBUFFER_WRITE

ALPHA_FLAGS_DEFAULT = 4333
NONE_REF = 0xFFFFFFFF


class NifError(Exception):
    pass


# ---------------------------------------------------------------------------------------------
# header
# ---------------------------------------------------------------------------------------------
class Nif(object):
    """A parsed NIF: the header fields needed to re-serialize it, and the blocks as bytearrays."""

    def __init__(self, data):
        if not data.startswith(MAGIC):
            raise NifError("not a NIF (bad magic)")
        nl = data.index(b"\n")
        self.magic_line = bytes(data[:nl + 1])
        p = nl + 1
        self.version, = struct.unpack_from("<I", data, p); p += 4
        self.endian = data[p]; p += 1
        self.user_version, = struct.unpack_from("<I", data, p); p += 4
        num_blocks, = struct.unpack_from("<I", data, p); p += 4
        self.num_blocks_field_at = p - 4
        self.user_version_2 = 0
        if self.version >= VERSION_SSE:
            self.user_version_2, = struct.unpack_from("<I", data, p); p += 4

        def short():
            nonlocal p
            n = data[p]; p += 1
            s = bytes(data[p:p + n]); p += n
            return bytes([n]) + s         # keep the length byte: this slice is re-emitted verbatim

        self.export_raw = short() + short() + short()

        ntypes, = struct.unpack_from("<H", data, p); p += 2
        self.types = []
        self.type_raw = []                            # verbatim slices: some writers terminate these
        for _ in range(ntypes):                       # strings with a null and some (pyNifly) do not
            n, = struct.unpack_from("<I", data, p); p += 4
            raw = bytes(data[p:p + n])
            self.type_raw.append(raw)
            self.types.append(raw.split(b"\x00")[0].decode("latin-1"))
            p += n
        self.index = list(struct.unpack_from("<%dH" % num_blocks, data, p)); p += 2 * num_blocks
        if self.version >= VERSION_SSE:
            self.sizes = list(struct.unpack_from("<%dI" % num_blocks, data, p)); p += 4 * num_blocks
        else:
            self.sizes = [0] * num_blocks
        self.strings = []
        self.string_raw = []
        self.max_string_length = 0
        if self.version >= 0x14010003:
            nstr, self.max_string_length = struct.unpack_from("<II", data, p); p += 8
            for _ in range(nstr):
                n, = struct.unpack_from("<I", data, p); p += 4
                raw = bytes(data[p:p + n])
                self.string_raw.append(raw)
                self.strings.append(raw.split(b"\x00")[0].decode("latin-1"))
                p += n
        self.groups_raw = b""
        if self.version >= 0x05000001:
            gstart = p
            ngroups, = struct.unpack_from("<I", data, p); p += 4
            p += 4 * ngroups * 2                      # group id + name ref per group
            self.groups_raw = bytes(data[gstart:p])   # keep the count u32 too, so the slice is verbatim

        self.data_start = p
        self.blocks = []
        off = self.data_start
        for i in range(num_blocks):
            size = self.sizes[i]
            if off + size > len(data):
                raise NifError("block %d (%s) runs past the end of the file" % (i, self.types[self.index[i]]))
            self.blocks.append(bytearray(data[off:off + size]))
            off += size
        self.trailer = bytes(data[off:])
        self.source_size = len(data)
        self.is_sse = (self.version == VERSION_SSE and self.user_version == 12)

    # -- header helpers ---------------------------------------------------------------------
    def type_of(self, block_index):
        return self.types[self.index[block_index]]

    def need_type(self, name):
        if name not in self.types:
            self.types.append(name)
            terminated = any(raw.endswith(b"\x00") for raw in self.type_raw) if self.type_raw else False
            self.type_raw.append(name.encode("latin-1") + (b"\x00" if terminated else b""))
        return self.types.index(name)

    def serialize(self):
        out = bytearray(self.magic_line)
        out += struct.pack("<I", self.version) + bytes([self.endian]) + struct.pack("<I", self.user_version)
        out += struct.pack("<I", len(self.blocks))
        if self.version >= VERSION_SSE:
            out += struct.pack("<I", self.user_version_2)
        out += self.export_raw
        out += struct.pack("<H", len(self.type_raw))
        for raw in self.type_raw:
            out += struct.pack("<I", len(raw)) + raw
        for ti in self.index:
            out += struct.pack("<H", ti)
        for b in self.blocks:
            out += struct.pack("<I", len(b))
        maxlen = self.max_string_length
        for raw in self.string_raw:
            maxlen = max(maxlen, len(raw))
        out += struct.pack("<II", len(self.string_raw), maxlen)
        for raw in self.string_raw:
            out += struct.pack("<I", len(raw)) + raw
        out += self.groups_raw
        for b in self.blocks:
            out += b
        out += self.trailer
        return bytes(out)

    def save(self, path):
        d = os.path.dirname(os.path.abspath(path))
        if d and not os.path.isdir(d):
            os.makedirs(d)
        with open(path, "wb") as fh:
            fh.write(self.serialize())
        return path

    def shapes(self):
        out = []
        for i in range(len(self.blocks)):
            if self.type_of(i) in TRISHAPE_TYPES:
                try:
                    out.append(Shape(self, i))
                except NifError:
                    pass
        return out


# ---------------------------------------------------------------------------------------------
# VertexDesc
# ---------------------------------------------------------------------------------------------
def decode_desc(desc):
    b = struct.unpack("<8B", struct.pack("<Q", desc))
    vf = b[5] | (b[6] << 8)
    return dict(desc=desc, stride4=b[0], uv0=b[1] & 0xF, uv1=b[1] >> 4, normal=b[2] & 0xF, tangent=b[2] >> 4,
                color=b[3] & 0xF, skin=b[3] >> 4, land=b[4] & 0xF, eye=b[4] >> 4,
                vf=vf, vf8=b[7], bytes=b)


def encode_desc(d):
    return struct.unpack("<Q", struct.pack("<8B", d["stride4"], (d["uv1"] << 4) | d["uv0"],
                                          (d["tangent"] << 4) | d["normal"], (d["skin"] << 4) | d["color"],
                                          (d["eye"] << 4) | d["land"], d["vf"] & 0xFF, (d["vf"] >> 8) & 0xFF,
                                          d["vf8"]))[0]


def layout_from_flags(vf):
    """Byte offsets implied by the VF bits, and the stride they add up to."""
    off, parts = 0, {}
    if vf & VF_VERTEX:
        parts["vertex"] = off; off += SZ_VERTEX
    if vf & VF_UV:
        parts["uv0"] = off; off += SZ_UV
    if vf & VF_UV_2:
        parts["uv1"] = off; off += SZ_UV
    if vf & VF_NORMAL:
        parts["normal"] = off; off += SZ_NORMAL
    if (vf & VF_NORMAL) and (vf & VF_TANGENT):
        parts["tangent"] = off; off += SZ_TANGENT
    if vf & VF_COLORS:
        parts["color"] = off; off += SZ_COLOR
    if vf & VF_SKINNED:
        parts["skin"] = off; off += SZ_SKIN
    if vf & VF_EYEDATA:
        parts["eye"] = off; off += SZ_EYE
    return parts, off


def layout_from_nibbles(d):
    """Byte offsets stated by the nibbles themselves."""
    parts = {}
    if d["vf"] & VF_VERTEX:
        parts["vertex"] = 0
    if d["vf"] & VF_UV:
        parts["uv0"] = d["uv0"] * 4
    if d["vf"] & VF_UV_2:
        parts["uv1"] = d["uv1"] * 4
    if d["vf"] & VF_NORMAL:
        parts["normal"] = d["normal"] * 4
    if (d["vf"] & VF_NORMAL) and (d["vf"] & VF_TANGENT):
        parts["tangent"] = d["tangent"] * 4
    if d["vf"] & VF_COLORS:
        parts["color"] = d["color"] * 4
    if d["vf"] & VF_SKINNED:
        parts["skin"] = d["skin"] * 4
    if d["vf"] & VF_EYEDATA:
        parts["eye"] = d["eye"] * 4
    return parts


# ---------------------------------------------------------------------------------------------
# shape
# ---------------------------------------------------------------------------------------------
class Shape(object):
    """One BSTriShape. Everything is absolute file offsets, so it survives a header rebuild."""

    def __init__(self, nif, index):
        self.nif = nif
        self.index = index
        self.type = nif.type_of(index)
        blk = nif.blocks[index]
        self.block_start = None                             # offsets are block-relative, see below
        p = 0
        name_idx, = struct.unpack_from("<I", blk, p); p += 4
        if name_idx >= len(nif.strings):
            raise NifError("block %d: name index %d outside the string table" % (index, name_idx))
        self.name = nif.strings[name_idx]
        self.n_extra, = struct.unpack_from("<I", blk, p); p += 4
        p += 4 * self.n_extra
        self.controller_off = p; p += 4
        self.flags_off = p; p += 4
        self.transform_off = p; p += 52
        self.collision_off = p; p += 4
        self.bounds_off = p; p += 16
        self.skin_ref_off = p
        self.skin_ref, = struct.unpack_from("<i", blk, p); p += 4
        self.shader_ref_off = p
        self.shader_ref, = struct.unpack_from("<i", blk, p); p += 4
        self.alpha_ref_off = p
        self.alpha_ref, = struct.unpack_from("<i", blk, p); p += 4
        self.prologue_len = p + 16                          # + desc(8) + counts(4) + dataSize(4)
        self.desc_off = p
        self.desc, = struct.unpack_from("<Q", blk, p); p += 8
        self.counts_off = p
        packed, = struct.unpack_from("<I", blk, p); p += 4
        self.tri, self.vert = packed & 0xFFFF, packed >> 16
        self.dsz_off = p
        self.dsz, = struct.unpack_from("<I", blk, p); p += 4
        self.vstart = p                                     # block-relative
        self.d = decode_desc(self.desc)
        self.has_colors = bool(self.d["vf"] & VF_COLORS)
        self.color_off = self.d["color"] * 4 if self.has_colors else None
        self.stride = 0
        self.tstart = self.vstart + self.vert * self.stride
        self.insert_off = None
        self.reason = ""
        if not self.vert:
            self.reason = "no vertices"
            return
        if self.dsz <= self.tri * 6:
            self.reason = "dataSize %d does not exceed the triangle array %d" % (self.dsz, self.tri * 6)
            return
        vbytes = self.dsz - self.tri * 6
        if vbytes % self.vert:
            self.reason = "vertex bytes %d not divisible by %d vertices" % (vbytes, self.vert)
            return
        self.stride = vbytes // self.vert
        self.tstart = self.vstart + self.vert * self.stride
        parts, implied = layout_from_flags(self.d["vf"])
        nib = layout_from_nibbles(self.d)
        if implied != self.stride:
            self.reason = ("stride %d (dataSize/verts) != %d implied by VF flags 0x%03X"
                           % (self.stride, implied, self.d["vf"]))
        elif self.d["stride4"] * 4 != self.stride:
            self.reason = "VF1 says stride %d, dataSize/verts says %d" % (self.d["stride4"] * 4, self.stride)
        else:
            for attr in parts:
                if nib.get(attr) != parts[attr]:
                    self.reason = "%s offset nibble says %d, layout says %d" % (attr, nib.get(attr), parts[attr])
                    break
        # where a color component would go: immediately before the skinning block (xEdit's order)
        if "skin" in parts:
            self.insert_off = parts["skin"]
        elif "eye" in parts:
            self.insert_off = parts["eye"]
        else:
            self.insert_off = self.stride

    @property
    def ok(self):
        return not self.reason

    def triangles(self):
        blk = self.nif.blocks[self.index]
        flat = struct.unpack_from("<%dH" % (self.tri * 3), blk, self.tstart)
        return [(flat[i], flat[i + 1], flat[i + 2]) for i in range(0, len(flat), 3)]

    def positions(self):
        """The vertex positions, or [] when the shape does not carry VF_VERTEX."""
        blk = self.nif.blocks[self.index]
        if not (self.d["vf"] & VF_VERTEX) or not self.vert:
            return []
        return [struct.unpack_from("<3f", blk, self.vstart + v * self.stride) for v in range(self.vert)]

    def alphas(self):
        blk = self.nif.blocks[self.index]
        if not self.has_colors:
            return None
        return [blk[self.vstart + v * self.stride + self.color_off + 3] for v in range(self.vert)]

    def colors(self):
        blk = self.nif.blocks[self.index]
        if not self.has_colors:
            return None
        return [bytes(blk[self.vstart + v * self.stride + self.color_off:
                          self.vstart + v * self.stride + self.color_off + 4]) for v in range(self.vert)]

    def add_colors(self, bgra=(255, 255, 255, 255)):
        """Rebuild the vertex array with a 4-byte color inserted, and set VF_COLORS. Returns True if it ran."""
        if not self.ok:
            raise NifError("%s: refusing to rebuild an unvalidated shape (%s)" % (self.name, self.reason))
        if self.has_colors:
            return False
        blk = self.nif.blocks[self.index]
        ins = self.insert_off
        verts_off = self.vstart
        tris_off = self.tstart
        head = bytearray(blk[:verts_off])
        verts = blk[verts_off:tris_off]
        rest = bytearray(blk[tris_off:])
        col = bytes(bytearray(bgra))
        out = bytearray()
        for v in range(self.vert):
            s = v * self.stride
            out += verts[s:s + ins] + col + verts[s + ins:s + self.stride]
        # patch the prologue copy: desc and dataSize
        d = decode_desc(self.desc)
        new_stride = self.stride + 4
        d["stride4"] = new_stride // 4
        d["color"] = ins // 4
        if d["vf"] & VF_SKINNED:
            d["skin"] = (ins + 4) // 4
        if d["vf"] & VF_EYEDATA:
            d["eye"] = (ins + 4) // 4
        d["vf"] |= VF_COLORS
        struct.pack_into("<Q", head, self.desc_off, encode_desc(d))
        struct.pack_into("<I", head, self.dsz_off, self.dsz + 4 * self.vert)
        self.nif.blocks[self.index] = head + out + rest
        # re-read this shape from the modified block so every field matches what is on disk
        fresh = Shape(self.nif, self.index)
        self.__dict__.update(fresh.__dict__)
        return True

    def set_alphas(self, alphas):
        """Write one alpha per vertex. Adds the color component first when the shape has none.

        Values are **clamped to 0-255, not wrapped**: a byte field cannot hold 300, and the old `& 0xFF` turned it into
        44 — a silent inversion of intent rather than a saturated value. Measured on this writer before the change.
        """
        if len(alphas) != self.vert:
            raise NifError("%s: %d alphas for %d vertices" % (self.name, len(alphas), self.vert))
        self.add_colors()
        blk = self.nif.blocks[self.index]
        base = self.vstart + self.color_off + 3
        for v, a in enumerate(alphas):
            blk[base + v * self.stride] = 0 if a < 0 else (255 if a > 255 else int(a))

    def set_colors(self, colors):
        """Write one BGRA color per vertex, each component clamped to 0-255. Adds colors when the shape has none."""
        if len(colors) != self.vert:
            raise NifError("%s: %d colors for %d vertices" % (self.name, len(colors), self.vert))
        self.add_colors()
        blk = self.nif.blocks[self.index]
        for v, c in enumerate(colors):
            base = self.vstart + v * self.stride + self.color_off
            b, g, r, a = tuple(int(x) for x in c)[:4]
            blk[base + 0] = 0 if b < 0 else (255 if b > 255 else b)
            blk[base + 1] = 0 if g < 0 else (255 if g > 255 else g)
            blk[base + 2] = 0 if r < 0 else (255 if r > 255 else r)
            blk[base + 3] = 0 if a < 0 else (255 if a > 255 else a)

    def describe(self):
        return ("%-24s %-22s verts=%-6d tris=%-6d stride=%-3d colors=%-5s color_off=%-4s vf=0x%04X"
                % (self.name[:24], self.type, self.vert, self.tri, self.stride,
                   "yes" if self.has_colors else "no", self.color_off, self.d["vf"]))


# ---------------------------------------------------------------------------------------------
# property edits
# ---------------------------------------------------------------------------------------------
def _property_flags_offset(nif, block_index):
    """Offset of Flags for a NiProperty-derived block.

    NiObjectNET is Name(string ref) + Extra Data List(u32 count + refs) + Controller(i32) -- except that
    at version 20.2.0.7 and only for BSLightingShaderProperty it carries a **Shader Type u32 BEFORE Name**
    (nif.xml, NiObjectNET: field "Shader Type" since="20.2.0.7" until="20.2.0.7"
    onlyT="BSLightingShaderProperty"). That one field is why a naive 12-byte prologue reads flags 4 bytes
    early and produces nonsense -- measured on MountainTrim01.nif block 8, where the real flags 0x82000109
    sit at +16 and not +12.
    """
    blk = nif.blocks[block_index]
    base = 0
    if nif.type_of(block_index) == "BSLightingShaderProperty" and nif.version == VERSION_SSE:
        base = 4
    n_extra, = struct.unpack_from("<I", blk, base + 4)
    if n_extra > 16:
        raise NifError("%s block %d: extra-data count %d is not plausible"
                       % (nif.type_of(block_index), block_index, n_extra))
    return base + 12 + 4 * n_extra


def read_alpha_property(nif, sh):
    if sh.alpha_ref < 0:
        return None
    if nif.type_of(sh.alpha_ref) != "NiAlphaProperty":
        return dict(error="alpha ref points at %s" % nif.type_of(sh.alpha_ref))
    blk = nif.blocks[sh.alpha_ref]
    off = _property_flags_offset(nif, sh.alpha_ref)
    flags, = struct.unpack_from("<H", blk, off)
    return dict(block=sh.alpha_ref, flags=flags, threshold=blk[off + 2])


def ensure_alpha_property(nif, sh, flags=ALPHA_FLAGS_DEFAULT, threshold=0):
    """Set the shape's NiAlphaProperty to `flags`, creating the block when the shape has none.

    Returns (action, detail). `action` is one of 'unchanged', 'patched', 'created', 'refused'.
    """
    cur = read_alpha_property(nif, sh)
    if cur and "error" in cur:
        return "refused", cur["error"]
    if cur:
        if cur["flags"] == flags:
            return "unchanged", "block %d already 0x%04X (threshold %d)" % (cur["block"], flags, cur["threshold"])
        blk = nif.blocks[cur["block"]]
        off = _property_flags_offset(nif, cur["block"])
        struct.pack_into("<H", blk, off, flags)
        return "patched", "block %d 0x%04X -> 0x%04X (threshold %d kept)" % (
            cur["block"], cur["flags"], flags, cur["threshold"])
    ti = nif.need_type("NiAlphaProperty")
    body = bytearray(struct.pack("<III", NONE_REF, 0, NONE_REF) + struct.pack("<H", flags) + bytes([threshold]))
    nif.blocks.append(body)
    nif.index.append(ti)
    new_ref = len(nif.blocks) - 1
    struct.pack_into("<i", nif.blocks[sh.index], sh.alpha_ref_off, new_ref)
    sh.alpha_ref = new_ref
    return "created", "block %d (flags 0x%04X, threshold %d)" % (new_ref, flags, threshold)


def read_shader_flags(nif, sh):
    if sh.shader_ref < 0:
        return dict(error="no shader property")
    t = nif.type_of(sh.shader_ref)
    if t not in SHADER_TYPES:
        return dict(error="shader ref points at %s" % t)
    blk = nif.blocks[sh.shader_ref]
    off = _property_flags_offset(nif, sh.shader_ref)
    f1, f2 = struct.unpack_from("<II", blk, off)
    return dict(block=sh.shader_ref, type=t, flags1=f1, flags2=f2, offset=off)


def apply_shader_flags(nif, sh, set1=MASK_SET1, clear1=0, set2=MASK_SET2, clear2=MASK_CLEAR2):
    """Apply the mask-friendly shader flags -- always setting what is missing. Returns (action, changed)."""
    cur = read_shader_flags(nif, sh)
    if "error" in cur:
        return "refused", cur["error"]
    f1, f2 = cur["flags1"], cur["flags2"]
    n1 = (f1 | set1) & ~clear1
    n2 = (f2 | set2) & ~clear2
    if n1 == f1 and n2 == f2:
        return "unchanged", ""
    changed = []
    for bit, name in sorted(SLSF1_NAMES.items()):
        if (f1 & bit) != (n1 & bit):
            changed.append("%s %s" % ("+" if n1 & bit else "-", name))
    for bit, name in sorted(SLSF2_NAMES.items()):
        if (f2 & bit) != (n2 & bit):
            changed.append("%s %s" % ("+" if n2 & bit else "-", name))
    blk = nif.blocks[cur["block"]]
    struct.pack_into("<II", blk, cur["offset"], n1, n2)
    return "patched", ", ".join(changed)


# ---------------------------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------------------------
def verify_written(source, out_bytes, expectations, alpha_flags=ALPHA_FLAGS_DEFAULT,
                   check_alpha_prop=True, check_shader=True):
    """Re-parse a written file and check it against the source. Returns (ok, problems, notes).

    `expectations` maps shape block index -> the alpha list that was painted. With the two check flags on
    (the default) the properties are also **confirmed after the write**: every painted shape has a
    NiAlphaProperty at `alpha_flags`, and every required shader bit is set while ZBuffer_Write is clear.
    That confirmation is what makes "always set" verifiable instead of merely attempted.
    """
    problems, notes = [], []
    out = Nif(out_bytes)
    src = source if isinstance(source, Nif) else Nif(source)
    if out.is_sse != src.is_sse:
        problems.append("version flags differ")
    if len(out.blocks) < len(src.blocks):
        problems.append("block count fell from %d to %d" % (len(src.blocks), len(out.blocks)))
    if out.trailer != src.trailer:
        problems.append("trailer changed (%d -> %d bytes)" % (len(src.trailer), len(out.trailer)))
    if not out.is_sse:
        problems.append("output is not a Skyrim SE NIF")
    src_shapes = {s.index: s for s in src.shapes() if s.ok}
    shapes = {s.index: s for s in out.shapes() if s.ok}
    for idx, want in expectations.items():
        name = src_shapes[idx].name if idx in src_shapes else ("block %d" % idx)
        if idx not in shapes:
            problems.append("%s: shape missing or unparseable after the write" % name)
            continue
        sh = shapes[idx]
        if not sh.has_colors:
            problems.append("%s: VF_COLORS still clear" % name)
            continue
        got = sh.alphas()
        if got != list(want):
            bad = sum(1 for a, b in zip(got, want) if a != b)
            problems.append("%s: %d of %d alphas read back wrong" % (name, bad, len(want)))
        if sh.d["stride4"] * 4 != sh.stride:
            problems.append("%s: VF1 %d disagrees with stride %d" % (name, sh.d["stride4"] * 4, sh.stride))
        parts, implied = layout_from_flags(sh.d["vf"])
        if implied != sh.stride:
            problems.append("%s: VF flags imply %d bytes, stride is %d" % (name, implied, sh.stride))
        tris = sh.triangles()
        if tris and max(max(t) for t in tris) >= sh.vert:
            problems.append("%s: triangle index %d >= %d vertices" % (name, max(max(t) for t in tris), sh.vert))
        expected_len = sh.prologue_len + sh.dsz + 4
        actual_len = len(sh.nif.blocks[sh.index])
        if actual_len < expected_len:
            problems.append("%s: block is %d bytes, prologue+dataSize+4 is %d"
                            % (name, actual_len, expected_len))
        if idx in src_shapes and geometry_signature(sh) != geometry_signature(src_shapes[idx]):
            problems.append("%s: geometry changed (positions/UVs/normals/tangents/triangles)" % name)
        if check_alpha_prop:
            ap = read_alpha_property(out, sh)
            if ap is None:
                problems.append("%s: no NiAlphaProperty after the write" % name)
            elif "error" in ap:
                problems.append("%s: %s" % (name, ap["error"]))
            elif ap["flags"] != alpha_flags:
                problems.append("%s: NiAlphaProperty is 0x%04X, expected 0x%04X"
                                % (name, ap["flags"], alpha_flags))
            else:
                notes.append("%s: alpha property block %d confirmed at 0x%04X"
                             % (name, ap["block"], ap["flags"]))
        if check_shader:
            sf = read_shader_flags(out, sh)
            if "error" in sf:
                problems.append("%s: shader flags unreadable (%s)" % (name, sf["error"]))
            else:
                missing1 = [n for b, n in SLSF1_NAMES.items() if (MASK_SET1 & b) and not (sf["flags1"] & b)]
                missing2 = [n for b, n in SLSF2_NAMES.items() if (MASK_SET2 & b) and not (sf["flags2"] & b)]
                if missing1 or missing2:
                    problems.append("%s: shader flags still missing %s"
                                    % (name, ", ".join(missing1 + missing2)))
                if sf["flags2"] & MASK_CLEAR2:
                    problems.append("%s: SLSF2 ZBuffer_Write still set" % name)
                if not (missing1 or missing2) and not (sf["flags2"] & MASK_CLEAR2):
                    notes.append("%s: shader 0x%08X / 0x%08X confirmed" % (name, sf["flags1"], sf["flags2"]))
        notes.append("%s: %d verts, stride %d, color at %d, alphas read back" %
                     (name, sh.vert, sh.stride, sh.color_off))
    # shapes that were not painted must be untouched
    for idx, sh in shapes.items():
        if idx in expectations or idx not in src_shapes:
            continue
        if geometry_signature(sh) != geometry_signature(src_shapes[idx]):
            problems.append("%s: geometry changed but this shape was not selected" % src_shapes[idx].name)
    return (not problems), problems, notes


class AllBlackDialog(object):
    """A small topmost dialog: flip to white / skip.

    There is deliberately no "write it all black" button. These tools only ever remove coverage, so a mask that is
    black on every vertex is always a mistake -- and a shape that should not be there at all is CULLED by PGPatcher
    before runtime, not made invisible by painting. Offered anyway, it would be the one option that silently ships a
    trishape nobody can see.

    Deliberately NOT `tkinter.messagebox` on a withdrawn root. Measured 2026-11-11: that combination can create
    a dialog that never maps (an invisible modal), and then the run blocks with nothing on screen to click. This
    class owns its window, so it can be centred, lifted, given a timeout, and -- the reason it is a class -- its
    buttons can be invoked from a self-test, which turns "there is a dialog" into something checkable.

    Returns 'flip' or 'skip'. Every exit that is not a deliberate choice (timeout, window closed, no
    display) answers 'skip', so an unanswered prompt never writes anything.
    """

    def __init__(self, info, timeout_seconds=120):
        import tkinter
        self.result = "skip"
        self.root = tkinter.Tk()
        self.root.title("All-black vertex-alpha mask")
        self.root.resizable(False, False)
        try:
            self.root.attributes("-topmost", True)
        except Exception:
            pass
        frame = tkinter.Frame(self.root, padx=16, pady=14)
        frame.pack()
        text = ("%s\nshape: %s (%d vertices)\n\n"
                "The computed mask is BLACK on every vertex, which would make the whole surface vanish.\n"
                "These tools only remove coverage, so that is a mistake rather than an intention.\n"
                "Flip it to white and write?\n\n"
                "To remove a shape entirely, cull it in PGPatcher instead -- that is the supported way.\n\n"
                "This window skips the shape and closes itself after %d seconds."
                % (os.path.basename(info["file"]), info["shape"], info["verts"], timeout_seconds))
        tkinter.Label(frame, text=text, justify="left", anchor="w").pack(fill="x")
        row = tkinter.Frame(frame)
        row.pack(fill="x", pady=(14, 0))
        self.buttons = {
            "flip": tkinter.Button(row, text="Flip to white and write", width=24,
                                   command=lambda: self._done("flip")),
            "skip": tkinter.Button(row, text="Skip this shape", width=16, command=lambda: self._done("skip")),
        }
        for name in ("flip", "skip"):
            self.buttons[name].pack(side="left", padx=(0, 6))
        self.root.bind("<Return>", lambda e: self._done("flip"))
        self.root.bind("<Escape>", lambda e: self._done("skip"))
        self.root.protocol("WM_DELETE_WINDOW", lambda: self._done("skip"))
        self.root.update_idletasks()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        self.root.geometry("+%d+%d" % ((self.root.winfo_screenwidth() - w) // 2,
                                       (self.root.winfo_screenheight() - h) // 3))
        self.buttons["flip"].focus_set()
        self.root.after(int(timeout_seconds * 1000), lambda: self._done("skip"))

    def _done(self, value):
        self.result = value
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        self.root.lift()
        try:
            self.root.focus_force()
        except Exception:
            pass
        self.root.mainloop()
        return self.result

    def press(self, name):
        """Invoke a button as a user would -- the hook the self-test uses."""
        self.buttons[name].invoke()


def ask_all_black(info, timeout_seconds=120):
    """Ask what to do about an all-black mask. Returns 'flip' or 'skip'.

    A real dialog when a desktop is available, a console question when the run is interactive, and a refusal
    otherwise -- an unattended run must not paint a mesh invisible because nobody was there to answer.
    """
    force_console = bool(os.environ.get("NIF_VERTEX_NO_GUI"))
    if not force_console:
        try:
            return AllBlackDialog(info, timeout_seconds).run()
        except Exception:
            pass
    if force_console or (sys.stdin and sys.stdin.isatty()):
        body = ("%s\nshape: %s (%d vertices)\n\nThe computed mask is BLACK on every vertex, which would make the "
                "whole surface vanish. Flip it to white first?"
                % (os.path.basename(info["file"]), info["shape"], info["verts"]))
        print("\nAll-black vertex-alpha mask\n%s\n" % body)
        try:
            while True:
                choice = input("[f]lip to white / [s]kip this shape: ").strip().lower()[:1]
                if choice == "f":
                    return "flip"
                if choice == "s":
                    return "skip"
        except (EOFError, OSError):
            print("   ! no answer on the console -- skipping %s" % info["shape"])
            return "skip"
    print("   ! %s: all-black mask on %s, and no way to ask -- skipping it (use --all-black "
          "flip|skip to decide ahead of time)" % (os.path.basename(info["file"]), info["shape"]))
    return "skip"


def resolve_all_black(policy, info, prompter=None):
    """'ask' | 'flip' | 'skip' -> 'flip' | 'skip'."""
    if policy in ("flip", "skip"):
        return policy
    if policy != "ask":
        raise NifError("unknown all-black policy %r" % policy)
    return (prompter or ask_all_black)(info)


def apply_file(path, out_path, alphas_for, alpha_flags=ALPHA_FLAGS_DEFAULT, do_shader=True,
               do_alpha=True, verify=True, all_black="ask", prompter=None):
    """Paint a file: alphas_for(shape) -> list of alphas, or None to leave that shape alone.

    Block indices are stable because new blocks are only ever appended, so every expectation, record and
    check is keyed by block index -- shape names repeat inside real NIFs and cannot be used as keys.

    An all-black result is resolved **before anything is written**: `all_black` is 'ask' (dialog), 'flip'
    (invert it, which for an all-black mask means white) or 'skip'. Nothing is painted until every
    shape's decision is known, so a cancelled prompt cannot leave a half-written file behind.
    """
    with open(path, "rb") as fh:
        raw = fh.read()
    ref = Nif(raw)
    work = Nif(raw)
    shapes = {}
    for sh in work.shapes():
        shapes[sh.index] = sh
    problems, wants, skipped = [], {}, []
    for idx, sh in shapes.items():
        if not sh.ok:
            problems.append("%s: %s" % (sh.name, sh.reason))
            continue
        want = alphas_for(sh)
        if want is None:
            continue
        wants[idx] = [int(a) & 0xFF for a in want]

    # the all-black decision, taken before a single byte is written
    decisions = {}
    for idx in sorted(wants):
        want = wants[idx]
        if not want or any(a != 0 for a in want):
            continue
        info = dict(file=path, shape=shapes[idx].name, verts=shapes[idx].vert, block=idx)
        action = resolve_all_black(all_black, info, prompter)
        decisions[idx] = action
        if action == "flip":
            wants[idx] = [255 - a for a in want]
        elif action == "skip":
            del wants[idx]
            skipped.append(info)

    records = []
    for idx, want in wants.items():
        rec = paint_shape(work, shapes[idx], want, alpha_flags, do_shader, do_alpha)
        rec["all_black"] = decisions.get(idx, "")
        records.append(rec)
    if not records:
        reason = "every selected shape was skipped (all-black mask)" if skipped else "no shape selected"
        return dict(ok=False, problems=problems + [reason], records=[], out_path=None,
                    src_bytes=len(raw), bytes=0, notes=[], skipped=skipped)
    d = os.path.dirname(os.path.abspath(out_path))
    if d and not os.path.isdir(d):
        os.makedirs(d)
    work.save(out_path)
    with open(out_path, "rb") as fh:
        out_bytes = fh.read()
    notes = ["%s: all-black mask -> %s" % (s["shape"], decisions.get(s["block"], "")) for s in skipped]
    for rec in records:
        if rec["all_black"]:
            notes.append("%s: all-black mask -> %s" % (rec["shape"], rec["all_black"]))
    if verify:
        ok, problems2, notes2 = verify_written(ref, out_bytes, {r["index"]: wants[r["index"]] for r in records},
                                              alpha_flags, do_alpha, do_shader)
        problems.extend(problems2)
        notes.extend(notes2)
    else:
        ok = True
    return dict(ok=ok and not problems, problems=problems, records=records, notes=notes,
                out_path=out_path, src_bytes=len(raw), bytes=len(out_bytes),
                expectations={r["index"]: wants[r["index"]] for r in records}, skipped=skipped)


# The viewer every tool in this folder opens. Order matters: an explicit NIFSKOPE_EXE in the environment wins, then
# the CS PBR build (octoprocessor made it the default on 2026-11-15 because it can render PBR material sets), then the
# plain Dev 11 build, so a tool still works if one of them is moved or rebuilt away.
def _pick_nifskope():
    cands = []
    env = os.environ.get("NIFSKOPE_EXE", "").strip()
    if env:
        cands.append(env)
    cands += [
        r"D:\0cto-Tools\NifSkope-CS\NifSkopeCS.exe",
        r"D:\0cto-Tools\NifSkope-CS\NifSkope.exe",
        r"D:\0cto-Tools\NifSkope-Tentacle\NifSkope.exe",
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    return cands[0]


NIFSKOPE = _pick_nifskope()



def _strip_suffix(stem, suffixes):
    """`foo_mask` asked for `_mask` again gives `foo_mask`, not `foo_mask_mask`."""
    low = stem.lower()
    for s in suffixes:
        if low.endswith(s):
            return stem[:-len(s)]
    return stem


def output_path_for(src, suffix, outdir=""):
    """Where a tool's output goes: next to the source with an appended name, unless `outdir` says otherwise.

    There is no destination restriction. The user asked for that explicitly on 2026-11-11 -- these are authoring tools
    for their own files, and a rule about *which folder is safe* only gets in the way when the file being edited is a
    live mesh. What is unchanged is the naming: the output is always a NEW file (`<stem><suffix>.nif`), never the
    source written over, so a tool run cannot destroy the mesh it read.
    """
    src = os.path.abspath(src)
    stem, _ext = os.path.splitext(os.path.basename(src))
    name = _strip_suffix(stem, ("_mask", "_inv", "_weld")) + suffix + ".nif"
    d = outdir or os.path.dirname(src)
    return os.path.abspath(os.path.join(d, name)), ""


def load_shape_plan(path):
    """Read a per-shape plan: one shape per line, settings as `key=value` tokens.

    Format (tab- or space-separated, `#` comments, blank lines ignored):

        # shape            settings
        triLandscape       mode=buffer buffer=3 merge=keep
        triMountainMask*   mode=edges
        triRockSkirt       skip

    The first pattern that matches a shape wins, so put specific names before wildcards. A shape with no line is
    **left alone** -- with a plan, silence means keep, which is what "edited or kept differently per line" needs.
    `skip` (or `keep`) is shorthand for leaving that shape alone even though it matched.

    Returns (rows, problems); rows are (pattern, settings dict) in file order.
    """
    rows, problems = [], []
    # utf-8-sig: PowerShell's Set-Content writes a BOM, and a BOM on a comment line turns it into a shape pattern
    # named "\ufeff" -- harmless (it matches nothing) but it means the plan silently gains a phantom line.
    with io.open(path, encoding="utf-8-sig") as fh:
        for n, line in enumerate(fh, 1):
            text = line.split("#", 1)[0].strip()
            if not text:
                continue
            # tabs separate the shape from its settings, and the settings themselves may be space- or tab-separated
            fields = [f for f in text.split("\t") if f.strip()]
            if not fields:
                continue
            pattern, settings = fields[0].strip(), {}
            tokens = [t for f in fields[1:] for t in f.split()]
            for tok in tokens:
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    settings[k.strip().lower()] = v.strip()
                else:
                    settings[tok.strip().lower()] = True
            if not any(ch in pattern for ch in "*?[]"):
                pattern_l = pattern.lower()
            else:
                pattern_l = pattern.lower()
            rows.append((pattern_l, settings))
            if not pattern:
                problems.append("line %d: no shape pattern" % n)
    if not rows:
        problems.append("%s contains no shape lines" % path)
    return rows, problems


def plan_for(name, rows):
    """The settings for a shape name, first match wins. None means the plan does not mention it."""
    import fnmatch
    low = name.lower()
    for pattern, settings in rows:
        if fnmatch.fnmatchcase(low, pattern) or low == pattern:
            return settings
    return None


def write_shape_plan(path, shapes, defaults=""):
    """Emit a starter plan listing every shape, so it can be edited rather than typed from scratch."""
    lines = ["# per-shape plan for %s" % os.path.basename(path), "# shape\tsettings"]
    for sh in shapes:
        lines.append("%s\t%s" % (sh.name, defaults))
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def open_in_nifskope(path, exe=""):
    """Open a file in NifSkope. Returns 'opened', 'missing' or the error text; never blocks."""
    exe = exe or NIFSKOPE
    if not os.path.isfile(exe):
        return "missing NifSkope at %s" % exe
    try:
        import subprocess
        subprocess.Popen([exe, os.path.abspath(path)])
        return "opened"
    except Exception as exc:
        return "could not launch NifSkope: %s" % exc


def replace_vertex_data(sh, vertex_bytes, triangles):
    """Replace a shape's vertex array and triangle list, keeping everything else in the block.

    Used by the weld/UV tool: the vertex count changes, so dataSize, the packed triangle/vertex counts and the
    block length all change with it. Everything after the triangle array -- the trailing particle-data size and
    any segment block -- is carried over untouched, which is why BSSubIndexTriShape is refused: its segment
    indices refer to vertex numbers that this operation renumbers.
    """
    if sh.type != "BSTriShape":
        raise NifError("%s is a %s: segment data would need remapping, which this does not do" % (sh.name, sh.type))
    if not sh.ok:
        raise NifError("%s: refusing to rewrite an unvalidated shape (%s)" % (sh.name, sh.reason))
    n_vert = len(vertex_bytes)
    n_tri = len(triangles)
    if n_vert == 0:
        raise NifError("%s: refusing to write zero vertices" % sh.name)
    if n_vert > 0xFFFF or n_tri > 0xFFFF:
        raise NifError("%s: %d vertices / %d triangles does not fit the u16 counts" % (sh.name, n_vert, n_tri))
    for v in vertex_bytes:
        if len(v) != sh.stride:
            raise NifError("%s: vertex record is %d bytes, stride is %d" % (sh.name, len(v), sh.stride))
    for t in triangles:
        if len(t) != 3 or any(i < 0 or i >= n_vert for i in t):
            raise NifError("%s: triangle %r is out of range for %d vertices" % (sh.name, t, n_vert))
    blk = sh.nif.blocks[sh.index]
    head = bytearray(blk[:sh.vstart])
    tail = bytearray(blk[sh.tstart + sh.tri * 6:])
    struct.pack_into("<I", head, sh.counts_off, (n_vert << 16) | n_tri)
    struct.pack_into("<I", head, sh.dsz_off, n_vert * sh.stride + n_tri * 6)
    flat = [i for t in triangles for i in t]
    body = b"".join(vertex_bytes) + struct.pack("<%dH" % len(flat), *flat)
    sh.nif.blocks[sh.index] = head + body + tail
    fresh = Shape(sh.nif, sh.index)
    sh.__dict__.update(fresh.__dict__)
    return dict(verts=n_vert, tris=n_tri, dsz=sh.dsz, block_bytes=len(sh.nif.blocks[sh.index]))


def geometry_signature(sh):
    """Every vertex byte except the color component, plus the triangle list.

    This is what a stride rebuild is most likely to destroy, and it is independent of where the color
    component sits, so a correct in-place patch and a correct rebuild produce the same signature.
    """
    blk = sh.nif.blocks[sh.index]
    rows = []
    for v in range(sh.vert):
        s = sh.vstart + v * sh.stride
        if sh.has_colors:
            rows.append(bytes(blk[s:s + sh.color_off]) + bytes(blk[s + sh.color_off + 4:s + sh.stride]))
        else:
            rows.append(bytes(blk[s:s + sh.stride]))
    return (sh.vert, sh.tri, tuple(rows), tuple(sh.triangles()))


def paint_shape(nif, sh, alphas, alpha_flags=ALPHA_FLAGS_DEFAULT, do_shader=True, do_alpha=True):
    """Add colors if missing, write the alphas, then make the property edits. Returns a report dict."""
    added = sh.add_colors()
    sh.set_alphas(alphas)
    rec = dict(index=sh.index, shape=sh.name, verts=sh.vert, tris=sh.tri, stride=sh.stride,
               color_off=sh.color_off, added_colors=added, black=sum(1 for a in alphas if a == 0),
               alpha="", shader="")
    if do_alpha:
        act, detail = ensure_alpha_property(nif, sh, alpha_flags)
        rec["alpha"] = "%s (%s)" % (act, detail)
    if do_shader:
        act, detail = apply_shader_flags(nif, sh)
        rec["shader"] = act if not detail else "%s (%s)" % (act, detail)
    return rec


# ---------------------------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------------------------
def audit_file(path, rows):
    with open(path, "rb") as fh:
        data = fh.read()
    try:
        nif = Nif(data)
    except NifError as exc:
        rows.append(dict(file=path, shape="<unparsable>", type="", verts=0, tris=0, stride=0, colors="?",
                         color_off="", vf="", alpha="", alpha_flags="", shader="", slsf1="", slsf2="",
                         note=str(exc)))
        return
    if not nif.is_sse:
        rows.append(dict(file=path, shape="<not SSE>", type="", verts=0, tris=0, stride=0, colors="?",
                         color_off="", vf="", alpha="", alpha_flags="", shader="", slsf1="", slsf2="",
                         note="version 0x%X userVersion %d" % (nif.version, nif.user_version)))
        return
    for i in range(len(nif.blocks)):
        t = nif.type_of(i)
        if t not in TRISHAPE_TYPES:
            continue
        try:
            sh = Shape(nif, i)
        except NifError as exc:
            rows.append(dict(file=path, shape="<unparsable>", type=t, verts=0, tris=0, stride=0, colors="?",
                             color_off="", vf="", alpha="", alpha_flags="", shader="", slsf1="", slsf2="",
                             note=str(exc)))
            continue
        try:
            ap = read_alpha_property(nif, sh)
        except (struct.error, IndexError) as exc:
            ap = dict(error="alpha property unreadable: %s" % exc)
        try:
            sf = read_shader_flags(nif, sh)
        except (struct.error, IndexError) as exc:
            sf = dict(error="%s block unreadable: %s" %
                      (nif.type_of(sh.shader_ref) if sh.shader_ref >= 0 else "no shader", exc))
        notes = sh.reason
        if ap and "error" in ap:
            notes = (notes + "; " if notes else "") + ap["error"]
        if "error" in sf:
            notes = (notes + "; " if notes else "") + sf["error"]
        rows.append(dict(
            file=path, shape=sh.name, type=t, verts=sh.vert, tris=sh.tri, stride=sh.stride,
            colors="yes" if sh.has_colors else "NO", color_off=sh.color_off if sh.has_colors else "",
            vf="0x%04X" % sh.d["vf"],
            alpha="block %d" % ap["block"] if ap and "block" in ap else ("none" if ap is None else "?"),
            alpha_flags=("0x%04X" % ap["flags"]) if ap and "flags" in ap else "",
            shader=("block %d" % sf["block"]) if "block" in sf else "-",
            slsf1="0x%08X" % sf["flags1"] if "flags1" in sf else "",
            slsf2="0x%08X" % sf["flags2"] if "flags2" in sf else "",
            note=notes))


AUDIT_COLS = ["file", "shape", "type", "verts", "tris", "stride", "colors", "color_off", "vf",
              "alpha", "alpha_flags", "shader", "slsf1", "slsf2", "note"]


def cmd_audit(args):
    files = []
    if os.path.isdir(args.path):
        if not args.recurse:
            raise SystemExit("%s is a folder - add --recurse" % args.path)
        for dp, _, fns in os.walk(args.path):
            for f in fns:
                if f.lower().endswith(".nif"):
                    files.append(os.path.join(dp, f))
    else:
        files.append(args.path)
    files.sort()
    rows = []
    for f in files:
        audit_file(f, rows)
    out = args.tsv or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "_recon", "nif_vertex_audit.tsv")
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("#" + "\t".join(AUDIT_COLS) + "\n")
        for r in rows:
            fh.write("\t".join(str(r.get(c, "")) for c in AUDIT_COLS) + "\n")
    shapes = [r for r in rows if r["verts"]]
    nocol = [r for r in shapes if r["colors"] == "NO"]
    noalpha = [r for r in shapes if r["alpha"] == "none"]
    zbuf = [r for r in shapes if r["slsf2"] and (int(r["slsf2"], 16) & SLSF2_ZBUFFER_WRITE)]
    decal = [r for r in shapes if r["slsf1"] and (int(r["slsf1"], 16) & SLSF1_DECAL)]
    dyn = [r for r in shapes if r["slsf1"] and (int(r["slsf1"], 16) & SLSF1_DYNAMIC_DECAL)]
    valpha = [r for r in shapes if r["slsf1"] and (int(r["slsf1"], 16) & SLSF1_VERTEX_ALPHA)]
    bad = [r for r in rows if r["note"]]
    flags = {}
    for r in shapes:
        flags[r["alpha_flags"]] = flags.get(r["alpha_flags"], 0) + 1
    print("# nif_vertex audit  %d file(s), %d shape(s)" % (len(files), len(shapes)))
    print("# reported as:       %s" % out)
    print("  shapes with no VF_COLORS .......... %d" % len(nocol))
    print("  shapes with no NiAlphaProperty .... %d" % len(noalpha))
    print("  shapes with SLSF2 ZBuffer_Write ... %d" % len(zbuf))
    print("  shapes with SLSF1 Decal ........... %d" % len(decal))
    print("  shapes with SLSF1 Dynamic_Decal ... %d" % len(dyn))
    print("  shapes with SLSF1 Vertex_Alpha .... %d" % len(valpha))
    print("  shapes that failed validation ..... %d" % len(bad))
    print("  NiAlphaProperty flag histogram .... %s"
          % ", ".join("%s x%d" % (k or "none", v) for k, v in sorted(flags.items(), key=lambda kv: -kv[1])))
    for r in bad[:10]:
        print("   ! %s  %s  %s" % (os.path.basename(r["file"]), r["shape"], r["note"]))
    return 0


# ---------------------------------------------------------------------------------------------
# self-test and round-trip
# ---------------------------------------------------------------------------------------------
def _synthetic_nif(with_colors=False, with_alpha=False, with_shader=True, skinned=False):
    """Build a small but structurally real SSE NIF: one BSTriShape, one square, optionally colors."""
    verts = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0)]
    tris = [(0, 1, 2), (0, 2, 3)]
    vf = VF_VERTEX | VF_UV | VF_NORMAL
    if with_colors:
        vf |= VF_COLORS
    if skinned:
        vf |= VF_SKINNED
    parts, stride = layout_from_flags(vf)
    desc_d = dict(stride4=stride // 4, uv0=parts["uv0"] // 4, uv1=0, normal=parts["normal"] // 4, tangent=0,
                  color=(parts["color"] // 4) if with_colors else 0,
                  skin=(parts["skin"] // 4) if skinned else 0, land=0, eye=0, vf=vf, vf8=0)
    vb = bytearray()
    for x, y, z in verts:
        vb += struct.pack("<3f", x, y, z) + struct.pack("<f", 1.0)
        vb += struct.pack("<2e", 0.0, 0.0)
        vb += bytes([128, 128, 255, 0])
        if with_colors:
            vb += bytes([200, 100, 50, 255])
        if skinned:
            vb += struct.pack("<4e", 0.25, 0.25, 0.25, 0.25) + bytes([0, 1, 0, 0])
    tb = bytearray()
    for t in tris:
        tb += struct.pack("<3H", *t)
    dsz = len(vb) + len(tb)
    strings = ["testShape"]
    body = bytearray()
    body += struct.pack("<I", 0)                    # name = string 0
    body += struct.pack("<I", 0)                    # no extra data
    body += struct.pack("<i", -1)                   # controller       @8
    body += struct.pack("<I", 0)                    # flags           @12
    body += struct.pack("<52s", b"\x00" * 52)       # transform       @16
    body += struct.pack("<i", -1)                   # collision       @68
    body += struct.pack("<16s", b"\x00" * 16)       # bounds          @72
    body += struct.pack("<i", -1)                   # skin            @88
    body += struct.pack("<i", -1)                   # shader          @92
    body += struct.pack("<i", -1)                   # alpha           @96
    body += struct.pack("<Q", encode_desc(desc_d))
    body += struct.pack("<I", (len(verts) << 16) | len(tris))
    body += struct.pack("<I", dsz)
    body += vb + tb + struct.pack("<I", 0)          # trailing particle data size
    blocks = [body]
    types = ["BSTriShape"]
    block_types = ["BSTriShape"]
    if with_shader:
        types.append("BSLightingShaderProperty")
        block_types.append("BSLightingShaderProperty")
        sb = bytearray()
        # 20.2.0.7 puts a Shader Type u32 before Name on NiObjectNET, for BSLightingShaderProperty only
        sb += struct.pack("<I", 0)                           # shader type   @0
        sb += struct.pack("<I", NONE_REF) + struct.pack("<I", 0) + struct.pack("<i", -1)   # name, extras, controller
        sb += struct.pack("<II", 0x80000001, 0x00000021)     # flags1, flags2 (ZBuffer_Write set)
        sb += struct.pack("<64s", b"\x00" * 64)
        struct.pack_into("<i", blocks[0], 92, len(blocks))
        blocks.append(sb)
    if with_alpha:
        types.append("NiAlphaProperty")
        block_types.append("NiAlphaProperty")
        ab = bytearray(struct.pack("<III", NONE_REF, 0, NONE_REF) + struct.pack("<H", 0x0001) + bytes([0]))
        struct.pack_into("<i", blocks[0], 96, len(blocks))
        blocks.append(ab)
    out = bytearray(MAGIC + b"20.2.0.7\n")
    out += struct.pack("<I", VERSION_SSE) + bytes([1]) + struct.pack("<I", 12)
    out += struct.pack("<I", len(blocks))
    out += struct.pack("<I", 0)
    out += bytes([0, 0, 0])
    out += struct.pack("<H", len(types))
    for t in types:
        raw = t.encode() + b"\x00"
        out += struct.pack("<I", len(raw)) + raw
    for t in block_types:
        out += struct.pack("<H", types.index(t))
    for b in blocks:
        out += struct.pack("<I", len(b))
    out += struct.pack("<II", len(strings), max(len(s) + 1 for s in strings))
    for s in strings:
        raw = s.encode() + b"\x00"
        out += struct.pack("<I", len(raw)) + raw
    out += struct.pack("<I", 0)
    for b in blocks:
        out += b
    return bytes(out)


def selftest():
    ok = True

    def check(label, cond, detail=""):
        nonlocal ok
        ok = ok and bool(cond)
        print("   %-58s %s%s" % (label, "ok" if cond else "FAIL", (" " + str(detail)) if detail and not cond else ""))

    # 1. desc round-trip
    d = decode_desc(0x0003B00007650408)
    check("real desc decodes to stride 32", d["stride4"] * 4 == 32, d)
    check("real desc color nibble 7 (byte 28)", d["color"] == 7, d["color"])
    check("real desc VF has COLORS", bool(d["vf"] & VF_COLORS), hex(d["vf"]))
    check("real desc re-encodes to the same u64", encode_desc(d) == 0x0003B00007650408)
    check("real desc: flags-implied stride == 32", layout_from_flags(d["vf"])[1] == 32)

    # 2. synthetic NIF without colors parses, and reports no colors
    raw = _synthetic_nif(with_colors=False)
    nif = Nif(raw)
    shapes = nif.shapes()
    check("synthetic: one shape found", len(shapes) == 1, len(shapes))
    sh = shapes[0]
    check("synthetic: parses cleanly", sh.ok, sh.reason)
    check("synthetic: no colors", not sh.has_colors)
    check("synthetic: stride 24 (16 vertex + 4 uv + 4 normal)", sh.stride == 24, sh.stride)
    check("synthetic: triangles round-trip", sh.triangles() == [(0, 1, 2), (0, 2, 3)], sh.triangles())
    check("synthetic: positions come back as the unit square that was written",
          [tuple(round(c, 4) for c in p) for p in sh.positions()],
          [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0)])

    # 3. no-op save is byte-identical -- the control that proves the header serializer
    check("no-op save reproduces the input byte for byte", nif.serialize() == raw)

    # 4. add colors to a shape that has none, and paint alphas
    sh.set_alphas([0, 255, 0, 255])
    out = nif.serialize()
    again = Nif(out)
    s2 = again.shapes()[0]
    check("after add: VF_COLORS set", s2.has_colors, hex(s2.d["vf"]))
    check("after add: stride 28", s2.stride == 28, s2.stride)
    check("after add: color offset 24", s2.color_off == 24, s2.color_off)
    check("after add: alphas read back", s2.alphas() == [0, 255, 0, 255], s2.alphas())
    check("after add: block grew by 16 bytes", len(again.blocks[0]) == len(nif_blocks_of(raw)) + 16,
          (len(again.blocks[0]), len(nif_blocks_of(raw))))
    check("after add: triangles unchanged", s2.triangles() == [(0, 1, 2), (0, 2, 3)], s2.triangles())
    check("after add: shape validates", s2.ok, s2.reason)

    # 5. positions survive the rebuild -- the thing a stride change is most likely to corrupt
    # NB. Shape offsets are BLOCK-relative, so both sides of this comparison index block 0.
    src_sh = nif.shapes()[0]
    src_v0 = bytes(nif.blocks[0][src_sh.vstart:src_sh.vstart + 12])
    v0 = bytes(again.blocks[0][s2.vstart:s2.vstart + 12])
    check("after add: first vertex position byte-identical", src_v0 == v0, (src_v0, v0))
    src_uv = bytes(nif.blocks[0][src_sh.vstart + 16:src_sh.vstart + 20])
    new_uv = bytes(again.blocks[0][s2.vstart + 16:s2.vstart + 20])
    check("after add: first vertex UV byte-identical", src_uv == new_uv, (src_uv, new_uv))

    # 6. existing colors patch in place, no size change
    raw2 = _synthetic_nif(with_colors=True)
    n2 = Nif(raw2)
    s = n2.shapes()[0]
    before = len(n2.blocks[0])
    s.set_alphas([1, 2, 3, 4])
    check("in place: block size unchanged", len(n2.blocks[0]) == before, (len(n2.blocks[0]), before))
    check("in place: alphas", Nif(n2.serialize()).shapes()[0].alphas() == [1, 2, 3, 4])

    # 6b. out-of-range values clamp instead of wrapping, and colors can be written component-wise
    s.set_alphas([300, -5, 128, 255])
    check("alphas clamp: 300 -> 255, -5 -> 0", Nif(n2.serialize()).shapes()[0].alphas() == [255, 0, 128, 255],
          Nif(n2.serialize()).shapes()[0].alphas())
    s.set_colors([(10, 20, 30, 40), (300, -1, 128, 256), (1, 2, 3, 4), (5, 6, 7, 8)])
    got = Nif(n2.serialize()).shapes()[0].colors()
    check("set_colors writes BGRA in order", got[0] == bytes((10, 20, 30, 40)), got[0])
    check("set_colors clamps each component", got[1] == bytes((255, 0, 128, 255)), got[1])

    # 7. NiAlphaProperty: create when absent, set flags when present
    raw3 = _synthetic_nif(with_colors=True, with_alpha=False)
    n3 = Nif(raw3)
    s3 = n3.shapes()[0]
    act, detail = ensure_alpha_property(n3, s3, 4333)
    check("alpha property created when absent", act == "created", (act, detail))
    n3b = Nif(n3.serialize())
    s3b = n3b.shapes()[0]
    ap = read_alpha_property(n3b, s3b)
    check("created alpha property reads 4333", ap and ap["flags"] == 4333, ap)
    act2, _ = ensure_alpha_property(n3b, s3b, 4333)
    check("second call is a no-op", act2 == "unchanged", act2)

    raw4 = _synthetic_nif(with_colors=True, with_alpha=True)
    n4 = Nif(raw4)
    s4 = n4.shapes()[0]
    act, detail = ensure_alpha_property(n4, s4, 4333)
    check("existing alpha property patched", act == "patched", (act, detail))
    check("patched value reads 4333", read_alpha_property(Nif(n4.serialize()), Nif(n4.serialize()).shapes()[0])["flags"] == 4333)

    # 8. shader flags
    raw5 = _synthetic_nif(with_colors=True)
    n5 = Nif(raw5)
    s5 = n5.shapes()[0]
    before = read_shader_flags(n5, s5)
    act, changed = apply_shader_flags(n5, s5)
    after = read_shader_flags(n5, s5)
    check("shader flags patched", act == "patched", act)
    check("ZBuffer_Write cleared", not (after["flags2"] & SLSF2_ZBUFFER_WRITE) and (before["flags2"] & SLSF2_ZBUFFER_WRITE))
    check("Decal set", bool(after["flags1"] & SLSF1_DECAL))
    check("Dynamic_Decal set", bool(after["flags1"] & SLSF1_DYNAMIC_DECAL))
    check("Vertex_Alpha set", bool(after["flags1"] & SLSF1_VERTEX_ALPHA))
    check("Assume_Shadowmask set", bool(after["flags2"] & SLSF2_ASSUME_SHADOWMASK))
    check("Vertex_Colors set", bool(after["flags2"] & SLSF2_VERTEX_COLORS))
    check("shader block size unchanged", len(n5.blocks[after["block"]]) == len(Nif(raw5).blocks[before["block"]]))

    # 9. verify_written catches a deliberately wrong expectation
    n6 = Nif(_synthetic_nif(with_colors=False))
    s6 = n6.shapes()[0]
    s6.set_alphas([9, 9, 9, 9])
    # property confirmation deliberately off here: this check is about the alpha comparison alone
    good, problems, notes = verify_written(Nif(_synthetic_nif(with_colors=False)), n6.serialize(),
                                           {0: [9, 9, 9, 9]}, check_alpha_prop=False, check_shader=False)
    check("verify passes on a correct write", good, problems)
    bad, problems2, _ = verify_written(Nif(_synthetic_nif(with_colors=False)), n6.serialize(),
                                       {0: [9, 9, 9, 8]}, check_alpha_prop=False, check_shader=False)
    check("verify fails on a wrong alpha expectation", not bad, problems2)

    # 10. end to end through apply_file: no colors in, colors + alpha property + shader flags out
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="nifvertex_")
    src = os.path.join(tmpdir, "src.nif")
    dst = os.path.join(tmpdir, "out.nif")
    with open(src, "wb") as fh:
        fh.write(_synthetic_nif(with_colors=False, with_alpha=False, with_shader=True))
    res = apply_file(src, dst, lambda sh: [0, 255, 255, 0])
    check("apply_file reports ok", res["ok"], res["problems"])
    check("apply_file recorded the added color component", res["records"][0]["added_colors"] is True)
    check("apply_file created the alpha property", "created" in res["records"][0]["alpha"], res["records"][0])
    check("apply_file applied the shader flags", "Decal" in res["records"][0]["shader"], res["records"][0]["shader"])
    out_sh = Nif(open(dst, "rb").read()).shapes()[0]
    check("apply_file: alphas on disk", out_sh.alphas() == [0, 255, 255, 0], out_sh.alphas())

    # 11. the all-black guard: decided before anything is written, and all four policies behave
    def _apply_black(policy, prompter=None):
        p_src = os.path.join(tmpdir, "black_src.nif")
        p_dst = os.path.join(tmpdir, "black_%s.nif" % policy)
        with open(p_src, "wb") as fh:
            fh.write(_synthetic_nif(with_colors=True, with_alpha=True))
        return apply_file(p_src, p_dst, lambda sh: [0, 0, 0, 0], 4333, True, True, True, policy, prompter), p_dst

    try:
        _apply_black("keep")
        rejected = False
    except Exception:
        rejected = True
    check("all-black: the old 'keep all black' policy is rejected, not honoured", rejected)
    r_flip, p_flip = _apply_black("flip")
    check("all-black flip: writes 255 everywhere",
          r_flip["ok"] and Nif(open(p_flip, "rb").read()).shapes()[0].alphas() == [255] * 4, r_flip["problems"])
    check("all-black flip: says so in the notes", any("all-black mask -> flip" in n for n in r_flip["notes"]),
          r_flip["notes"])
    r_skip, p_skip = _apply_black("skip")
    check("all-black skip: no file written", not r_skip["ok"] and not os.path.exists(p_skip), r_skip)
    asked = []
    r_ask, p_ask = _apply_black("ask", lambda info: (asked.append(info), "flip")[1])
    check("all-black ask: the prompter is consulted", len(asked) == 1 and asked[0]["verts"] == 4, asked)
    check("all-black ask: its answer is obeyed",
          r_ask["ok"] and Nif(open(p_ask, "rb").read()).shapes()[0].alphas() == [255] * 4, r_ask["problems"])
    r_ask2, _ = _apply_black("ask", lambda info: "skip")
    check("all-black ask: a skip answer writes nothing", not r_ask2["ok"], r_ask2)
    check("all-black: a non-black mask never asks",
          resolve_all_black("ask", dict(file="x", shape="y", verts=1), lambda i: asked.append(i) or "skip") == "skip")

    # 12. the dialog itself: real window, real buttons, invoked the way a user would
    info = dict(file="probe.nif", shape="triProbe", verts=4)
    try:
        def _press(name):
            dlg = AllBlackDialog(info, timeout_seconds=5)
            dlg.root.after(30, lambda: dlg.press(name))
            return dlg.run()
        check("dialog: the flip button answers flip", _press("flip"), "flip")
        probe = AllBlackDialog(info, timeout_seconds=5)
        names = sorted(probe.buttons)
        probe._done("skip")
        check("dialog: there is no 'write all black' button", "keep" not in names, names)
        check("dialog: the skip button answers skip", _press("skip"), "skip")
        dlg = AllBlackDialog(info, timeout_seconds=1)
        check("dialog: times out to skip, so nothing is written unanswered", dlg.run(), "skip")
        check("dialog: exactly two buttons exist", sorted(dlg.buttons), ["flip", "skip"])
    except Exception as exc:
        check("dialog: could not be created here (%s)" % exc, False)
    check("apply_file: alpha flags 4333", read_alpha_property(Nif(open(dst, "rb").read()), out_sh)["flags"] == 4333)
    check("apply_file: verification confirmed the alpha property",
          any("alpha property block" in n and "confirmed" in n for n in res["notes"]), res["notes"])
    check("apply_file: verification confirmed the shader bits",
          any("shader 0x" in n and "confirmed" in n for n in res["notes"]), res["notes"])

    # 12. the confirmation is not vacuous: a write that skipped the property edits must fail it
    src12 = os.path.join(tmpdir, "src12.nif")
    dst12 = os.path.join(tmpdir, "out12.nif")
    with open(src12, "wb") as fh:
        fh.write(_synthetic_nif(with_colors=True, with_alpha=False, with_shader=True))
    n12 = Nif(open(src12, "rb").read())
    n12.shapes()[0].set_alphas([0, 255, 0, 255])          # alphas only: no alpha property, no flags
    n12.save(dst12)
    bad12, probs12, _ = verify_written(Nif(open(src12, "rb").read()), open(dst12, "rb").read(),
                                       {0: [0, 255, 0, 255]}, 4333, True, True)
    check("confirm: a missing NiAlphaProperty is caught",
          not bad12 and any("no NiAlphaProperty" in p for p in probs12), probs12)
    check("confirm: missing shader bits are caught",
          any("still missing" in p for p in probs12), probs12)
    check("confirm: a still-set ZBuffer_Write is caught",
          any("ZBuffer_Write still set" in p for p in probs12), probs12)
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)

    # 11. a skinned shape: the color component must land BEFORE the bone data, or weights are destroyed
    nsk = Nif(_synthetic_nif(with_colors=False, skinned=True))
    ssk = nsk.shapes()[0]
    # no tangent in this shape, so 16 vertex + 4 uv + 4 normal + 12 skin = 36, and skin starts at 24
    check("skinned: stride 36 before the write", ssk.stride == 36, ssk.stride)
    check("skinned: skin data starts at 24", ssk.insert_off == 24, ssk.insert_off)
    skin_before = [bytes(nsk.blocks[ssk.index][ssk.vstart + v * ssk.stride + 24:
                                              ssk.vstart + v * ssk.stride + 36]) for v in range(ssk.vert)]
    ssk.set_alphas([0, 128, 255, 64])
    nsk2 = Nif(nsk.serialize())
    s2sk = nsk2.shapes()[0]
    check("skinned: stride 36 -> 40", s2sk.stride == 40, s2sk.stride)
    check("skinned: color offset 24, skin pushed to 28", (s2sk.color_off, s2sk.d["skin"] * 4) == (24, 28),
          (s2sk.color_off, s2sk.d["skin"] * 4))
    skin_after = [bytes(nsk2.blocks[s2sk.index][s2sk.vstart + v * s2sk.stride + 28:
                                              s2sk.vstart + v * s2sk.stride + 40]) for v in range(s2sk.vert)]
    check("skinned: every vertex kept its 12 bone bytes", skin_before == skin_after, (skin_before[:1], skin_after[:1]))
    check("skinned: alphas read back", s2sk.alphas() == [0, 128, 255, 64], s2sk.alphas())
    check("skinned: still validates", s2sk.ok, s2sk.reason)

    print("\nRESULT: %s" % ("all checks passed" if ok else "FAILURES above"))
    return 0 if ok else 1


def nif_blocks_of(raw):
    return Nif(raw).blocks[0]


def cmd_roundtrip(args):
    with open(args.path, "rb") as fh:
        raw = fh.read()
    nif = Nif(raw)
    out = nif.serialize()
    same = out == raw
    print("%s  %d bytes -> %d bytes  %s" % (args.path, len(raw), len(out),
                                            "IDENTICAL" if same else "DIFFERS"))
    if not same:
        for i in range(min(len(raw), len(out))):
            if raw[i] != out[i]:
                print("  first difference at byte %d" % i)
                break
    for sh in nif.shapes():
        print("  " + sh.describe())
    return 0 if same else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="read/write Skyrim SE vertex-color alpha and mask shader flags")
    sub = ap.add_subparsers(dest="cmd")
    a = sub.add_parser("audit")
    a.add_argument("path")
    a.add_argument("--recurse", action="store_true")
    a.add_argument("--tsv", default="")
    r = sub.add_parser("roundtrip")
    r.add_argument("path")
    sub.add_parser("selftest")
    args = ap.parse_args(argv)
    if args.cmd == "audit":
        return cmd_audit(args)
    if args.cmd == "roundtrip":
        return cmd_roundtrip(args)
    return selftest()


if __name__ == "__main__":
    sys.exit(main())
