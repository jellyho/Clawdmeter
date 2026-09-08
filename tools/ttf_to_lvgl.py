#!/usr/bin/env python3
"""ttf_to_lvgl.py - rasterise a TTF/OTF into an LVGL 9 bitmap font (.c).

A stdlib + Pillow replacement for `lv_font_conv` (npm).  It emits the same
`lv_font_fmt_txt` structures the shipped `firmware/src/font_*.c` files use,
already patched for LVGL 9 (no `#if LVGL_VERSION_MAJOR` guards, no `.cache`
field, and `.release_glyph` / `.kerning` / `.static_bitmap` / `.fallback` /
`.user_data` present) so the output compiles and *renders* as-is.

See docs/fonts.md for the recipes and for what this does NOT do (kerning).

Examples
--------
  # ASCII, 4 bpp, same parameters as the shipped font_styrene_28.c
  python tools/ttf_to_lvgl.py --font assets/StyreneB-Regular.otf --size 28 \
      --bpp 4 -r 0x20-0x7E -o firmware/src/font_styrene_28.c

  # KS X 1001 Hangul, 1 bpp, line metrics pinned to font_styrene_28's so it
  # can be used as that font's `.fallback`
  python tools/ttf_to_lvgl.py --font assets/NanumGothic-Regular.ttf --size 28 \
      --bpp 1 --ksx1001 --line-height 30 --base-line 6 \
      --name font_nanum_kr_28 -o firmware/src/font_nanum_kr_28.c
"""

from __future__ import annotations

import argparse
import bisect
import os
import re
import shlex
import struct
import sys

try:
    from PIL import Image, ImageFont
except ImportError:  # pragma: no cover
    sys.exit("error: Pillow is required (pip install Pillow)")


# Rough in-RAM/flash size of one `lv_font_fmt_txt_cmap_t` on a 32-bit target.
# Only used to weigh "one more subtable" against "two bytes per codepoint"
# when choosing between FORMAT0_TINY and SPARSE_TINY; the exact value is not
# critical, any number in 16..32 picks the same layout for real charsets.
CMAP_ENTRY_BYTES = 24

# `range_length` is a uint16 and sparse `unicode_list` entries are uint16
# relative codepoints, so a single cmap subtable can span at most 65535.
MAX_CMAP_SPAN = 65535


# --------------------------------------------------------------------------
# Minimal SFNT reader
#
# Pillow gives us hinted, pixel-snapped advance widths (`getlength()` returns
# whole pixels for most fonts).  lv_font_conv stores the *unhinted design*
# advance scaled to the pixel size, in 8.4 fixed point, which is what produces
# the fractional `adv_w` values in the shipped fonts.  Reading `hmtx` directly
# reproduces that, and the same parse gives us the font's real cmap coverage
# so we can skip codepoints the font does not actually have (rendering those
# would silently emit .notdef boxes).
# --------------------------------------------------------------------------
class SfntFont:
    def __init__(self, path: str):
        with open(path, "rb") as fh:
            self.data = fh.read()
        d = self.data
        if d[:4] == b"ttcf":
            # TrueType collection: use the first face.
            first = struct.unpack(">I", d[12:16])[0]
        else:
            first = 0
        num_tables = struct.unpack(">H", d[first + 4:first + 6])[0]
        self.tables: dict[str, tuple[int, int]] = {}
        for i in range(num_tables):
            rec = first + 12 + 16 * i
            tag, _csum, off, length = struct.unpack(">4sIII", d[rec:rec + 16])
            self.tables[tag.decode("latin-1")] = (off, length)

        self.units_per_em = 1000
        if "head" in self.tables:
            off = self.tables["head"][0]
            self.units_per_em = struct.unpack(">H", d[off + 18:off + 20])[0] or 1000

        self.num_glyphs = 0
        if "maxp" in self.tables:
            off = self.tables["maxp"][0]
            self.num_glyphs = struct.unpack(">H", d[off + 4:off + 6])[0]

        # `post` carries the font designer's underline placement, in design
        # units.  lv_font_conv reads it; scaling it to the pixel size
        # reproduces the `underline_position` / `underline_thickness` pair in
        # all 11 shipped firmware/src/font_*.c files exactly, where a
        # size-derived heuristic reproduces only 5 of them.
        self.underline_position: int | None = None
        self.underline_thickness: int | None = None
        if "post" in self.tables:
            off = self.tables["post"][0]
            if off + 12 <= len(d):
                pos, thick = struct.unpack(">hh", d[off + 8:off + 12])
                self.underline_position = pos
                self.underline_thickness = thick

        self._advances: list[int] = []
        if "hhea" in self.tables and "hmtx" in self.tables:
            hhea = self.tables["hhea"][0]
            num_h = struct.unpack(">H", d[hhea + 34:hhea + 36])[0]
            hmtx = self.tables["hmtx"][0]
            for i in range(num_h):
                p = hmtx + 4 * i
                if p + 2 > len(d):
                    break
                self._advances.append(struct.unpack(">H", d[p:p + 2])[0])

        self.cmap: dict[int, int] = {}
        if "cmap" in self.tables:
            try:
                self._parse_cmap(*self.tables["cmap"])
            except Exception as exc:  # pragma: no cover - corrupt font
                print("warning: cmap parse failed (%s); coverage checks off"
                      % exc, file=sys.stderr)
                self.cmap = {}

    # -- cmap ------------------------------------------------------------
    def _parse_cmap(self, base: int, _length: int) -> None:
        d = self.data
        n = struct.unpack(">H", d[base + 2:base + 4])[0]
        subtables = []
        for i in range(n):
            rec = base + 4 + 8 * i
            plat, enc, off = struct.unpack(">HHI", d[rec:rec + 8])
            subtables.append((plat, enc, base + off))

        def rank(entry):
            plat, enc, _off = entry
            order = [(3, 10), (0, 6), (0, 4), (3, 1), (0, 3), (0, 2), (0, 1),
                     (0, 0), (3, 0), (1, 0)]
            return order.index((plat, enc)) if (plat, enc) in order else 99

        for plat, enc, off in sorted(subtables, key=rank):
            fmt = struct.unpack(">H", d[off:off + 2])[0]
            table = self._parse_cmap_subtable(off, fmt)
            if table:
                self.cmap = table
                return

    def _parse_cmap_subtable(self, off: int, fmt: int) -> dict[int, int]:
        d = self.data
        out: dict[int, int] = {}
        if fmt == 0:
            for cp in range(256):
                gid = d[off + 6 + cp]
                if gid:
                    out[cp] = gid
        elif fmt == 4:
            seg_x2 = struct.unpack(">H", d[off + 6:off + 8])[0]
            seg = seg_x2 // 2
            ends = off + 14
            starts = ends + seg_x2 + 2
            deltas = starts + seg_x2
            ranges = deltas + seg_x2
            for i in range(seg):
                end = struct.unpack(">H", d[ends + 2 * i:ends + 2 * i + 2])[0]
                start = struct.unpack(">H", d[starts + 2 * i:starts + 2 * i + 2])[0]
                delta = struct.unpack(">h", d[deltas + 2 * i:deltas + 2 * i + 2])[0]
                ro = struct.unpack(">H", d[ranges + 2 * i:ranges + 2 * i + 2])[0]
                if start > end:
                    continue
                for cp in range(start, min(end, 0xFFFF) + 1):
                    if ro == 0:
                        gid = (cp + delta) & 0xFFFF
                    else:
                        p = ranges + 2 * i + ro + 2 * (cp - start)
                        if p + 2 > len(d):
                            continue
                        gid = struct.unpack(">H", d[p:p + 2])[0]
                        if gid:
                            gid = (gid + delta) & 0xFFFF
                    if gid:
                        out[cp] = gid
        elif fmt == 6:
            first, count = struct.unpack(">HH", d[off + 6:off + 10])
            for i in range(count):
                gid = struct.unpack(">H", d[off + 10 + 2 * i:off + 12 + 2 * i])[0]
                if gid:
                    out[first + i] = gid
        elif fmt == 12:
            n_groups = struct.unpack(">I", d[off + 12:off + 16])[0]
            for i in range(n_groups):
                p = off + 16 + 12 * i
                s, e, g = struct.unpack(">III", d[p:p + 12])
                if e < s or e > 0x10FFFF or e - s > 0x20000:
                    e = min(e, s + 0x20000)
                for cp in range(s, e + 1):
                    out[cp] = g + (cp - s)
        return out

    # -- metrics ---------------------------------------------------------
    def has_glyph(self, cp: int) -> bool:
        if not self.cmap:
            return True  # unknown coverage: trust the caller
        return cp in self.cmap

    def design_advance(self, cp: int) -> int | None:
        """Advance width of `cp` in font design units, or None if unknown."""
        if not self._advances:
            return None
        gid = self.cmap.get(cp) if self.cmap else None
        if gid is None:
            return None
        if gid < len(self._advances):
            return self._advances[gid]
        return self._advances[-1]


# --------------------------------------------------------------------------
# Codepoint selection
# --------------------------------------------------------------------------
def parse_ranges(spec: str) -> list[int]:
    """`0x20-0x7E,0x2026,65` -> list of codepoints."""
    cps: list[int] = []
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        m = re.fullmatch(r"(0[xX][0-9a-fA-F]+|\d+)(?:-(0[xX][0-9a-fA-F]+|\d+))?", part)
        if not m:
            raise argparse.ArgumentTypeError("bad range %r" % part)
        lo = int(m.group(1), 0)
        hi = int(m.group(2), 0) if m.group(2) else lo
        if hi < lo:
            raise argparse.ArgumentTypeError("reversed range %r" % part)
        cps.extend(range(lo, hi + 1))
    return cps


def ksx1001_hangul() -> list[int]:
    """The 2350 precomposed Hangul syllables of KS X 1001.

    Those are exactly the syllables that live in the EUC-KR *wansung* rows
    0xB0..0xC8; the other 8822 syllables of U+AC00..U+D7A3 only exist in the
    CP949 extension area and are vanishingly rare in real text.
    """
    out = []
    for cp in range(0xAC00, 0xD7A4):
        try:
            enc = chr(cp).encode("euc_kr")
        except UnicodeEncodeError:
            continue
        if len(enc) == 2 and 0xB0 <= enc[0] <= 0xC8:
            out.append(cp)
    return out


# --------------------------------------------------------------------------
# Glyph rasterisation
# --------------------------------------------------------------------------
class Glyph:
    __slots__ = ("cp", "adv_w", "box_w", "box_h", "ofs_x", "ofs_y", "bits",
                 "bitmap_index")

    def __init__(self, cp):
        self.cp = cp
        self.adv_w = 0
        self.box_w = 0
        self.box_h = 0
        self.ofs_x = 0
        self.ofs_y = 0
        self.bits = b""
        self.bitmap_index = 0


def quantise(value: int, bpp: int) -> int:
    if bpp == 8:
        return value
    levels = (1 << bpp) - 1
    return (value * levels + 127) // 255


def pack_bits(pixels: list[int], bpp: int) -> bytes:
    """MSB-first, continuous across rows (no per-row padding), whole glyph
    padded up to a byte boundary.  This is `LV_FONT_FMT_TXT_PLAIN` with
    `lv_font_fmt_txt_dsc_t.stride == 0` - verified against the shipped fonts:
    font_styrene_28's '$' is box 13x27 at 4 bpp and occupies 176 bytes
    (ceil(13*27*4/8)), not the 189 a row-padded layout would need."""
    out = bytearray()
    acc = 0
    nbits = 0
    for value in pixels:
        acc = (acc << bpp) | (value & ((1 << bpp) - 1))
        nbits += bpp
        while nbits >= 8:
            nbits -= 8
            out.append((acc >> nbits) & 0xFF)
    if nbits:
        out.append((acc << (8 - nbits)) & 0xFF)
    return bytes(out)


def render_glyph(font, sfnt, cp, size, bpp, mode, ascent, advance_mode) -> Glyph:
    g = Glyph(cp)

    # advance width, 8.4 fixed point
    adv_px = None
    if advance_mode == "design" and sfnt is not None:
        units = sfnt.design_advance(cp)
        if units is not None:
            adv_px = units * size / sfnt.units_per_em
    if adv_px is None:
        adv_px = font.getlength(chr(cp))
    g.adv_w = int(round(adv_px * 16))

    mask, (off_x, off_y) = font.getmask2(chr(cp), mode=mode)
    if mask.size[0] == 0 or mask.size[1] == 0:
        return g
    img = Image.frombytes("L", mask.size, bytes(mask))
    bbox = img.getbbox()
    if bbox is None:            # whitespace
        return g
    left, top, right, bottom = bbox
    img = img.crop(bbox)
    g.box_w, g.box_h = img.size
    g.ofs_x = off_x + left
    # ofs_y is the distance from the baseline up to the *bottom* of the box.
    # Pillow's mask offset is measured from the ascender line, and the
    # baseline sits `ascent` px below it.
    g.ofs_y = ascent - (off_y + bottom)

    pixels = [quantise(v, bpp) for v in img.tobytes()]
    g.bits = pack_bits(pixels, bpp)
    return g


# --------------------------------------------------------------------------
# cmap layout
# --------------------------------------------------------------------------
def contiguous_runs(cps: list[int]) -> list[tuple[int, int]]:
    """Maximal runs of consecutive codepoints, each no longer than one cmap
    subtable can span.

    The length cap is not cosmetic: `range_length` is a uint16, so a single
    run of >65535 consecutive codepoints has no legal one-subtable
    representation.  Splitting it HERE keeps the DP in plan_cmaps total —
    every candidate group of one run is representable, so the cost function
    never has to return INF for a group it cannot avoid choosing.
    """
    runs = []

    def push(start, end):
        while end - start + 1 > MAX_CMAP_SPAN:
            runs.append((start, start + MAX_CMAP_SPAN - 1))
            start += MAX_CMAP_SPAN
        runs.append((start, end))

    start = prev = cps[0]
    for cp in cps[1:]:
        if cp == prev + 1:
            prev = cp
        else:
            push(start, prev)
            start = prev = cp
    push(start, prev)
    return runs


def plan_cmaps(cps: list[int]) -> list[dict]:
    """Split the (sorted, unique) codepoints into cmap subtables, choosing
    FORMAT0_TINY for dense runs and SPARSE_TINY where merging scattered runs
    into one table beats paying for a subtable each.  Exact DP over runs."""
    runs = contiguous_runs(cps)
    n = len(runs)
    counts = [e - s + 1 for s, e in runs]
    prefix = [0]
    for c in counts:
        prefix.append(prefix[-1] + c)

    INF = float("inf")

    def cost(i, j):
        span = runs[j][1] - runs[i][0] + 1
        if span > MAX_CMAP_SPAN:
            return INF
        if i == j:
            return CMAP_ENTRY_BYTES            # FORMAT0_TINY, no side table
        return CMAP_ENTRY_BYTES + 2 * (prefix[j + 1] - prefix[i])

    dp = [INF] * (n + 1)
    back = [0] * (n + 1)
    dp[0] = 0
    for j in range(n):
        for i in range(j, -1, -1):
            c = cost(i, j)
            if c == INF:
                break
            if dp[i] + c < dp[j + 1]:
                dp[j + 1] = dp[i] + c
                back[j + 1] = i

    groups = []
    j = n
    while j > 0:
        i = back[j]
        groups.append((i, j - 1))
        j = i
    groups.reverse()

    tables = []
    gid = 1                                    # glyph 0 is reserved
    for i, j in groups:
        first = runs[i][0]
        last = runs[j][1]
        lo = bisect.bisect_left(cps, first)
        hi = bisect.bisect_right(cps, last)
        members = cps[lo:hi]
        entry = {
            "range_start": first,
            "range_length": last - first + 1,
            "glyph_id_start": gid,
            "count": len(members),
        }
        if i == j:
            entry["type"] = "LV_FONT_FMT_TXT_CMAP_FORMAT0_TINY"
            entry["unicode_list"] = None
        else:
            entry["type"] = "LV_FONT_FMT_TXT_CMAP_SPARSE_TINY"
            entry["unicode_list"] = [cp - first for cp in members]
        tables.append(entry)
        gid += len(members)

    # Belt and braces.  `range_length`, `glyph_id_start` and `list_length` are
    # all uint16 in `lv_font_fmt_txt_cmap_t`, and an overflow there does not
    # fail loudly — it truncates, and LVGL then resolves codepoints past the
    # truncation into unrelated glyph_dsc entries.  The run splitting above
    # makes the first unreachable and no real font gets near the others, but a
    # silent wrong font is exactly what this tool must never emit.
    for t in tables:
        if not 1 <= t["range_length"] <= 0xFFFF:
            raise ValueError("cmap range_length %d at U+%04X exceeds uint16"
                             % (t["range_length"], t["range_start"]))
        if not 0 <= t["glyph_id_start"] <= 0xFFFF:
            raise ValueError("cmap glyph_id_start %d exceeds uint16 (a font "
                             "cannot hold more than 65535 glyphs)"
                             % t["glyph_id_start"])
        if t["unicode_list"] is not None and len(t["unicode_list"]) > 0xFFFF:
            raise ValueError("cmap list_length %d exceeds uint16"
                             % len(t["unicode_list"]))
    return tables


# --------------------------------------------------------------------------
# C emission
# --------------------------------------------------------------------------
def fmt_bytes(data: bytes, per_line: int, indent: str = "    ") -> str:
    lines = []
    for i in range(0, len(data), per_line):
        chunk = data[i:i + per_line]
        lines.append(indent + ", ".join("0x%x" % b for b in chunk) + ",")
    return "\n".join(lines)


def emit(out_path, name, args, glyphs, cmaps, bitmap, line_height, base_line,
         underline_position, underline_thickness, opts_line):
    w = []
    a = w.append
    a("/" + "*" * 78)
    a(" * Size: %d px" % args.size)
    a(" * Bpp: %d" % args.bpp)
    a(" * Opts: %s" % opts_line)
    a(" * Generated by tools/ttf_to_lvgl.py (LVGL 9 output, no patching needed)")
    a(" " + "*" * 78 + "/")
    a("")
    a('#include "%s"' % args.lv_include)
    a("")
    a("")
    a("")
    a("/*-----------------")
    a(" *    BITMAPS")
    a(" *----------------*/")
    a("")
    a("/*Store the image of the glyphs*/")
    a("static LV_ATTRIBUTE_LARGE_CONST const uint8_t glyph_bitmap[] = {")
    if not bitmap:
        a("    0x00")
    else:
        for g in glyphs:
            label = "U+%04X" % g.cp
            if 0x20 < g.cp < 0x7F:
                ch = chr(g.cp)
                if ch == '"':
                    ch = '\\"'
                elif ch == "\\":
                    ch = "\\\\"
                label += ' "%s"' % ch
            a("    /* %s */" % label)
            if g.bits:
                a(fmt_bytes(g.bits, args.bytes_per_line))
            a("")
        # strip the trailing comma of the very last data line
        while w and w[-1] == "":
            w.pop()
        w[-1] = w[-1].rstrip(",")
    a("};")
    a("")
    a("")
    a("/*---------------------")
    a(" *  GLYPH DESCRIPTION")
    a(" *--------------------*/")
    a("")
    a("static const lv_font_fmt_txt_glyph_dsc_t glyph_dsc[] = {")
    a("    {.bitmap_index = 0, .adv_w = 0, .box_w = 0, .box_h = 0, "
      ".ofs_x = 0, .ofs_y = 0} /* id = 0 reserved */,")
    for i, g in enumerate(glyphs):
        a("    {.bitmap_index = %d, .adv_w = %d, .box_w = %d, .box_h = %d, "
          ".ofs_x = %d, .ofs_y = %d}%s"
          % (g.bitmap_index, g.adv_w, g.box_w, g.box_h, g.ofs_x, g.ofs_y,
             "," if i < len(glyphs) - 1 else ""))
    a("};")
    a("")
    a("/*---------------------")
    a(" *  CHARACTER MAPPING")
    a(" *--------------------*/")
    a("")
    for idx, cm in enumerate(cmaps):
        if cm["unicode_list"] is None:
            continue
        a("static const uint16_t unicode_list_%d[] = {" % idx)
        vals = cm["unicode_list"]
        per = 12
        for i in range(0, len(vals), per):
            chunk = vals[i:i + per]
            comma = "," if i + per < len(vals) else ""
            a("    " + ", ".join("0x%x" % v for v in chunk) + comma)
        a("};")
        a("")
    a("/*Collect the unicode lists and glyph_id offsets*/")
    a("static const lv_font_fmt_txt_cmap_t cmaps[] =")
    a("{")
    for idx, cm in enumerate(cmaps):
        ulist = ("unicode_list_%d" % idx) if cm["unicode_list"] is not None else "NULL"
        llen = len(cm["unicode_list"]) if cm["unicode_list"] is not None else 0
        a("    {")
        a("        .range_start = %d, .range_length = %d, .glyph_id_start = %d,"
          % (cm["range_start"], cm["range_length"], cm["glyph_id_start"]))
        a("        .unicode_list = %s, .glyph_id_ofs_list = NULL, "
          ".list_length = %d, .type = %s" % (ulist, llen, cm["type"]))
        a("    }%s" % ("," if idx < len(cmaps) - 1 else ""))
    a("};")
    a("")
    a("")
    a("")
    a("/*--------------------")
    a(" *  ALL CUSTOM DATA")
    a(" *--------------------*/")
    a("")
    a("")
    a("static const lv_font_fmt_txt_dsc_t font_dsc = {")
    a("    .glyph_bitmap = glyph_bitmap,")
    a("    .glyph_dsc = glyph_dsc,")
    a("    .cmaps = cmaps,")
    a("    .kern_dsc = NULL,")
    a("    .kern_scale = 0,")
    a("    .cmap_num = %d," % len(cmaps))
    a("    .bpp = %d," % args.bpp)
    a("    .kern_classes = 0,")
    a("    .bitmap_format = 0,")
    a("};")
    a("")
    a("")
    a("")
    a("/*-----------------")
    a(" *  PUBLIC FONT")
    a(" *----------------*/")
    a("")
    a("/*Initialize a public general font descriptor*/")
    a("const lv_font_t %s = {" % name)
    a("    .get_glyph_dsc = lv_font_get_glyph_dsc_fmt_txt,    "
      "/*Function pointer to get glyph's data*/")
    a("    .get_glyph_bitmap = lv_font_get_bitmap_fmt_txt,    "
      "/*Function pointer to get glyph's bitmap*/")
    a("    .line_height = %d,          /*The maximum line height required by the font*/"
      % line_height)
    a("    .base_line = %d,             /*Baseline measured from the bottom of the line*/"
      % base_line)
    a("    .subpx = LV_FONT_SUBPX_NONE,")
    a("    .release_glyph = NULL,")
    a("    .kerning = 0,")
    a("    .static_bitmap = 0,")
    a("    .underline_position = %d," % underline_position)
    a("    .underline_thickness = %d," % underline_thickness)
    a("    .dsc = &font_dsc,          /*The custom font data. "
      "Will be accessed by `get_glyph_bitmap/dsc` */")
    a("    .fallback = NULL,")
    a("    .user_data = NULL,")
    a("};")
    a("")

    text = "\n".join(w)
    with open(out_path, "w", encoding="ascii", newline="\n") as fh:
        fh.write(text)
    return len(text)


# --------------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(
        description="Rasterise a TTF/OTF into an LVGL 9 bitmap font (.c).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Examples", 1)[-1])
    p.add_argument("--font", required=True, help="source TTF/OTF")
    p.add_argument("--size", type=int, required=True, help="em size in px")
    p.add_argument("-o", "--output", required=True, help="output .c path")
    p.add_argument("--bpp", type=int, default=4, choices=(1, 2, 4, 8),
                   help="bits per pixel (default 4; 1 = no antialiasing)")
    p.add_argument("-r", "--range", action="append", default=[],
                   help="codepoint ranges, e.g. 0x20-0x7E,0x2026 (repeatable)")
    p.add_argument("--symbols", default="",
                   help="literal characters to include")
    p.add_argument("--charset-file",
                   help="UTF-8 file; every distinct character in it is included")
    p.add_argument("--ksx1001", action="store_true",
                   help="add the 2350 KS X 1001 precomposed Hangul syllables")
    p.add_argument("--name",
                   help="C symbol name (default: output file basename)")
    p.add_argument("--lv-include", default="lvgl.h",
                   help="header to #include (default lvgl.h)")
    p.add_argument("--line-height", type=int,
                   help="override line_height (use to match a font this one "
                        "is the .fallback of)")
    p.add_argument("--base-line", type=int, help="override base_line")
    p.add_argument("--ofs-y-adjust", type=int, default=0,
                   help="shift every glyph up (+) or down (-) by N px. Only "
                        "meaningful for a FALLBACK font: base_line is derived "
                        "from min(ofs_y), so on a primary font the derived "
                        "base_line absorbs the shift and nothing moves "
                        "(pin --base-line too if that is not what you want)")
    p.add_argument("--advance", choices=("design", "rendered"), default="design",
                   help="adv_w source: unhinted design metrics from hmtx "
                        "(default, matches lv_font_conv) or Pillow's hinted "
                        "getlength()")
    p.add_argument("--bytes-per-line", type=int, default=12,
                   help="bitmap bytes per source line (default 12)")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    # ---- codepoint set -------------------------------------------------
    cps: set[int] = set()
    for spec in args.range:
        cps.update(parse_ranges(spec))
    for ch in args.symbols:
        cps.add(ord(ch))
    if args.charset_file:
        with open(args.charset_file, encoding="utf-8") as fh:
            for ch in fh.read():
                if ch not in "\r\n\t":
                    cps.add(ord(ch))
    if args.ksx1001:
        cps.update(ksx1001_hangul())
    if not cps:
        p.error("no codepoints selected: pass -r / --symbols / --charset-file / --ksx1001")

    # ---- font ----------------------------------------------------------
    try:
        sfnt = SfntFont(args.font)
    except Exception as exc:
        print("warning: could not parse SFNT tables (%s); falling back to "
              "Pillow metrics" % exc, file=sys.stderr)
        sfnt = None

    font = ImageFont.truetype(args.font, args.size,
                              layout_engine=ImageFont.Layout.BASIC)
    ascent, _descent = font.getmetrics()
    mode = "1" if args.bpp == 1 else "L"

    wanted = sorted(cps)
    if sfnt is not None:
        have = [cp for cp in wanted if sfnt.has_glyph(cp)]
        missing = [cp for cp in wanted if not sfnt.has_glyph(cp)]
    else:
        have, missing = wanted, []
    if not have:
        p.error("the font covers none of the requested codepoints")

    glyphs = []
    bitmap = bytearray()
    for cp in have:
        g = render_glyph(font, sfnt, cp, args.size, args.bpp, mode, ascent,
                         args.advance)
        g.ofs_y += args.ofs_y_adjust
        g.bitmap_index = len(bitmap)
        bitmap.extend(g.bits)
        glyphs.append(g)

    # `lv_font_fmt_txt_glyph_dsc_t` packs these into small bitfields unless the
    # LVGL build sets LV_FONT_FMT_TXT_LARGE (this firmware does not), so a font
    # that overflows them would silently render garbage.
    problems = []
    for g in glyphs:
        if g.box_w > 255 or g.box_h > 255:
            problems.append("U+%04X box %dx%d exceeds 255" % (g.cp, g.box_w, g.box_h))
        if not -128 <= g.ofs_x <= 127 or not -128 <= g.ofs_y <= 127:
            problems.append("U+%04X ofs (%d,%d) outside int8"
                            % (g.cp, g.ofs_x, g.ofs_y))
        if g.adv_w > 0xFFF:
            problems.append("U+%04X adv_w %d exceeds the 12-bit field"
                            % (g.cp, g.adv_w))
    if len(bitmap) > (1 << 20) - 1:
        problems.append("glyph_bitmap is %d B; bitmap_index is a 20-bit field "
                        "(1 MB max)" % len(bitmap))
    if problems:
        for msg in problems[:10]:
            print("error: " + msg, file=sys.stderr)
        if len(problems) > 10:
            print("error: ... and %d more" % (len(problems) - 10), file=sys.stderr)
        sys.exit("aborting: output would overflow lv_font_fmt_txt fields "
                 "(use a smaller --size, or build LVGL with "
                 "LV_FONT_FMT_TXT_LARGE=1)")

    inked = [g for g in glyphs if g.box_h]
    if inked:
        top = max(g.ofs_y + g.box_h for g in inked)
        bottom = min(g.ofs_y for g in inked)
    else:
        top, bottom = args.size, 0
    # Matches lv_font_conv: both are derived from the rendered ink box of the
    # selected glyphs, not from the font's hhea/OS2 metrics.  Verified against
    # all 11 shipped firmware/src/font_*.c files.
    line_height = args.line_height if args.line_height is not None else top - bottom
    base_line = args.base_line if args.base_line is not None else -bottom

    # lv_font_conv takes both straight from the source font's `post` table,
    # scaled to the pixel size.  Verified: that rule reproduces the pair in
    # 11/11 shipped firmware/src/font_*.c files, where the size-derived
    # heuristic below reproduces only 5 (it is off by up to 2 px on BOTH
    # fields — font_tiempos_56 is (-5,2), the heuristic says (-3,4)).  The
    # heuristic is the fallback for a font with no readable `post`.
    if sfnt is not None and sfnt.underline_position is not None:
        scale = args.size / sfnt.units_per_em
        underline_position = round(sfnt.underline_position * scale)
        underline_thickness = round(sfnt.underline_thickness * scale)
    else:
        underline_position = -max(1, round(args.size / 20.0))
        underline_thickness = max(1, round(args.size / 16.0))

    cmaps = plan_cmaps(have)

    name = args.name or os.path.splitext(os.path.basename(args.output))[0]
    # The header this stamps into the .c is the first thing a reader hits, and
    # every shipped lv_font_conv font records a command line that actually
    # re-runs.  So build it from the PARSED args: a partial record is worse
    # than none — dropping --line-height/--base-line silently changes the
    # metrics on a re-run, and dropping --symbols/--charset-file leaves a
    # command that exits with "no codepoints selected".
    opt = ["--font", args.font, "--size", str(args.size), "--bpp", str(args.bpp)]
    for r in args.range:
        opt += ["-r", r]
    if args.symbols:
        opt += ["--symbols", args.symbols]
    if args.charset_file:
        opt += ["--charset-file", args.charset_file]
    if args.ksx1001:
        opt += ["--ksx1001"]
    if args.line_height is not None:
        opt += ["--line-height", str(args.line_height)]
    if args.base_line is not None:
        opt += ["--base-line", str(args.base_line)]
    if args.ofs_y_adjust:
        opt += ["--ofs-y-adjust", str(args.ofs_y_adjust)]
    if args.advance != "design":
        opt += ["--advance", args.advance]
    if args.bytes_per_line != 12:
        opt += ["--bytes-per-line", str(args.bytes_per_line)]
    if args.lv_include != "lvgl.h":
        opt += ["--lv-include", args.lv_include]
    if args.name:
        opt += ["--name", args.name]
    opt += ["-o", args.output]

    # POSIX quoting, and only where a shell would otherwise eat the token —
    # `--symbols "a b"` has to survive being pasted back into a terminal.
    # Plain tokens (every flag, every path without spaces) come out unchanged.
    opts_line = " ".join(shlex.quote(t) for t in opt)

    out_dir = os.path.dirname(os.path.abspath(args.output))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    src_bytes = emit(args.output, name, args, glyphs, cmaps, bitmap,
                     line_height, base_line, underline_position,
                     underline_thickness, opts_line)

    if not args.quiet:
        flash = (len(bitmap) + 8 * (len(glyphs) + 1)
                 + sum(2 * len(c["unicode_list"]) for c in cmaps
                       if c["unicode_list"]) + CMAP_ENTRY_BYTES * len(cmaps))
        kinds = ", ".join(c["type"].replace("LV_FONT_FMT_TXT_CMAP_", "").lower()
                          for c in cmaps[:4])
        if len(cmaps) > 4:
            kinds += ", ..."
        print("%s: %d glyphs, %d cmap subtable(s) (%s)"
              % (name, len(glyphs), len(cmaps), kinds))
        print("  line_height=%d base_line=%d  bitmap=%d B  est. flash=%d B  "
              "source=%d B" % (line_height, base_line, len(bitmap), flash,
                               src_bytes))
        if missing:
            print("  skipped %d codepoint(s) absent from the font "
                  "(first: U+%04X)" % (len(missing), missing[0]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
