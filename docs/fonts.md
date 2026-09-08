# Recompiling fonts

The `firmware/src/font_*.c` files are pre-compiled LVGL bitmap fonts.

There are two converters:

| | `lv_font_conv` (npm) | `tools/ttf_to_lvgl.py` (Python) |
|---|---|---|
| how the shipped Latin fonts were made | yes | no |
| needs Node/npm | yes | no (stdlib + Pillow) |
| kerning pairs | yes | **no** |
| LVGL 9 output | needs 4 manual patches | already patched |
| sparse cmaps for scattered charsets | yes | yes |

Use whichever is available. `lv_font_conv` remains the reference for the
Latin fonts (it emits kerning, which matters for the brand typefaces).
`ttf_to_lvgl.py` exists because Node is not installed on every machine this
firmware gets worked on, and because the CJK font needs a sparse subset that
is easier to describe with a flag than with a 2,350-entry `-r` argument.

---

## `lv_font_conv` (npm) — how the shipped Latin fonts were made

```bash
npm install -g lv_font_conv
```

Generate each one (one at a time — `lv_font_conv` doesn't like loop-driven
invocations) with `--no-compress` (required for LVGL 9):

```bash
# Tiempos Text (titles, 56px)
lv_font_conv --font assets/TiemposText-400-Regular.otf -r 0x20-0x7E \
  --size 56 --format lvgl --bpp 4 --no-compress \
  -o firmware/src/font_tiempos_56.c --lv-include "lvgl.h"

# Styrene B (large numbers 48, panel labels 28, small text 24, minimal 20)
for size in 48 28 24 20; do
  lv_font_conv --font assets/StyreneB-Regular.otf -r 0x20-0x7E \
    --size $size --format lvgl --bpp 4 --no-compress \
    -o firmware/src/font_styrene_${size}.c --lv-include "lvgl.h"
done

# DejaVu Sans Mono (32px, with spinner Unicode chars)
lv_font_conv --font assets/DejaVuSansMono.ttf \
  -r 0x20-0x7E,0xB7,0x2026,0x2722,0x2733,0x2736,0x273B,0x273D \
  --size 32 --format lvgl --bpp 4 --no-compress \
  -o firmware/src/font_mono_32.c --lv-include "lvgl.h"
```

**Important:** `lv_font_conv` v1.5.3 outputs LVGL 8 format. Each generated
file must be patched for LVGL 9 compatibility:

1. Remove `#if LVGL_VERSION_MAJOR >= 8` guards around `font_dsc` and the font struct
2. Remove the `.cache` field from `font_dsc`
3. Add `.release_glyph = NULL`, `.kerning = 0`, `.static_bitmap = 0` to the font struct
4. Add `.fallback = NULL`, `.user_data = NULL` to the font struct

Without these patches, fonts compile but render as invisible.

---

## `tools/ttf_to_lvgl.py` (Python) — no Node required

A stdlib + Pillow rasteriser that writes the same `lv_font_fmt_txt`
structures. **Its output already has the four LVGL 9 patches above applied**,
so there is nothing to fix up by hand.

```bash
pip install Pillow          # the only dependency
python tools/ttf_to_lvgl.py --help
```

### CLI

```
--font PATH             source TTF/OTF
--size N                em size in px
-o, --output PATH       output .c
--bpp {1,2,4,8}         default 4; 1 = no antialiasing, a quarter the size
--name NAME             C symbol (default: output basename)
--lv-include HDR        default lvgl.h

codepoint selection (combine freely, they union):
-r, --range SPEC        0x20-0x7E,0x2026   (repeatable)
--symbols "TEXT"        literal characters
--charset-file PATH     UTF-8 file; every distinct character in it
--ksx1001               the 2350 KS X 1001 precomposed Hangul syllables

metrics:
--line-height N         override (see "Fallback fonts" below)
--base-line N           override
--ofs-y-adjust N        shift every glyph up (+) / down (-) by N px.
                        FALLBACK fonts only: base_line is derived from
                        min(ofs_y), so on a primary font the derived base_line
                        absorbs the shift exactly and nothing moves. Pin
                        --base-line too if that is not what you want.
--advance {design,rendered}
                        adv_w from unhinted hmtx metrics (default, matches
                        lv_font_conv) or from Pillow's hinted getlength()
```

Codepoints the source font does not actually contain are skipped and counted,
rather than silently emitted as `.notdef` boxes.

### Recipes

```bash
# Regenerate an existing Latin font (structure-compatible with the shipped
# file; no kerning — see "Known differences")
python tools/ttf_to_lvgl.py --font assets/StyreneB-Regular.otf --size 28 \
  --bpp 4 -r 0x20-0x7E -o firmware/src/font_styrene_28.c

# Korean: KS X 1001 Hangul at 28 px, 1 bpp, metrics pinned to font_styrene_28
python tools/ttf_to_lvgl.py --font assets/NanumGothic-Regular.ttf --size 28 \
  --bpp 1 --ksx1001 -r 0x00B7,0x2013,0x2014,0x2018,0x2019,0x201C,0x201D,0x2026,0x20A9 \
  --line-height 30 --base-line 6 \
  --name font_nanum_kr_28 -o firmware/src/font_nanum_kr_28.c
```

Every generated file records its own full command line in the `Opts:` header,
and that line is meant to *run* — `--line-height`, `--base-line`, `--name`,
`--symbols` and `--charset-file` are all recorded, because omitting them
produces a file with different metrics (or, for `--symbols`, a command that
exits with "no codepoints selected"). Regenerating from a file's own header
reproduces it byte for byte apart from the `-o` path.

### Fallback fonts (how Korean is wired up)

LVGL 9's `lv_font_t` has a `.fallback` pointer that is resolved recursively
for any codepoint the primary font is missing. The Korean support uses that:
`font_styrene_28` keeps ASCII in the brand typeface, and only Hangul
codepoints resolve to `font_nanum_kr_28`. The Hangul font therefore contains
no space, no digits and no Latin — those all live in Styrene. It carries the
2,350 syllables plus **nine punctuation marks** a Korean IME emits and ASCII
has no equivalent for (`·` U+00B7, `–` `—`, the four curly quotes, `…`
U+2026, `₩` U+20A9). Those cost 184 B and close the one remaining way a
Korean sentence could still put placeholder boxes on the panel: the host's
punctuation fold runs *before* its Hangul pass and turns them into ASCII, so
they cannot arrive through this repo's daemon — but a third-party writer of
the RX characteristic is not bound by that.

**Only the message body has a fallback.** Sender names and session labels
render in `font_styrene_20/24/28/48`, none of which does, so Korean in those
fields is a row of empty boxes — in the largest font on the screen, for a
Korean project directory name. The host therefore folds every label to ASCII
(`clawdmeter_sessions.panel_label`, applied inside `fit_payload` so both label
producers go through it) and only the body keeps its Hangul. Doing it in the
firmware instead would mean Hangul faces at 20, 24 and 48 px as well:
**measured, 109,932 + 150,729 + 524,763 = 785,424 B** of extra flash, four
times the font that is already there, to render a machine name.

Two things matter when generating a fallback font:

* **`ofs_y` is baseline-relative**, so glyphs from the two fonts land on the
  same baseline automatically. Nothing to configure.
* **`line_height` / `base_line` come from the primary font**, so the
  fallback's own values are mostly cosmetic — but pin them anyway with
  `--line-height` / `--base-line` to match the font it hangs off, so anything
  that measures the fallback directly gets the same answer. Left to itself
  the Korean font derives 28/4 from its own ink box, against Styrene 28's
  30/6; the 2 px of extra ascent that pinning buys is what keeps the tallest
  syllable (ink 24 px above the baseline) inside the line.

#### Where the pointer is actually set

`lv_font_t` is `const` in every generated file, and on ESP32 that means
`.rodata` — memory-mapped flash. A `const_cast` store into `font_styrene_28`
does not "work anyway"; it faults. So the wiring is a **runtime copy**, in
`firmware/src/ui.cpp`:

```c
LV_FONT_DECLARE(font_nanum_kr_28);

static const lv_font_t* msg_body_font(void) {
    static lv_font_t kr;                 // font_styrene_28 + Hangul fallback
    if (!kr.get_glyph_dsc) {
        kr = font_styrene_28;
        kr.fallback = &font_nanum_kr_28;
    }
    return &kr;
}
```

One `lv_font_t` of `.bss` (~40 B), composed on first use so it cannot depend
on `ui_init()` having run. The alternative — writing
`.fallback = &font_nanum_kr_28` into `font_styrene_28.c` — was rejected on two
counts: the generated files are meant to be regenerable (the edit would
silently vanish on the next `lv_font_conv` run), and `font_styrene_28` is
shared with the usage screen and the splash, which have no Korean to render
and no reason to carry a 197 KB dependency into the link.

Only `MSG_BODY_FONT` uses the composed font today, i.e. the body of a
`SESSION_MESSAGE` card. That is the one field that carries text a human wrote.

#### One size, on both card kinds

The message body was `font_styrene_28` on the ONE-CHAT card and
`font_styrene_24` on a list card; it is now 28 on both, because 28 is the size
the Hangul face exists at. This is a metrics constraint, not taste: at 28 px
NanumGothic's ink reaches **24 px above the baseline**, and a `font_styrene_24`
line box only has **20 px** of ascent (`line_height 25 − base_line 5`), so a
wrapped two-line Korean body would climb into the line above it. Generating a
second Hangul size at 24 px costs a measured **150,729 B** — three quarters of
the font again, for a 4 px difference.

It costs the list card nothing: 88 px of content, less a 21 px sender row and
the 4 px gap, leaves 63 px — still two 30 px lines. (The comment that used to
sit on that macro claimed 28 px would cost the second line; the arithmetic
says otherwise, and the sim screenshots agree.)

### What it costs

Measured on the shipping tree, `--size 28 --bpp 1 --ksx1001` plus the nine
punctuation codepoints.

Source: `firmware/src/font_nanum_kr_28.c` is 1,318,311 B of C. Linked, from
`riscv32-esp-elf-size -A` on the object and the C6 map file:

| section | bytes |
|---|---|
| `.rodata.glyph_bitmap` | 174,644 |
| `.rodata.glyph_dsc` (2,360 × 8) | 18,880 |
| `.rodata.unicode_list_0` (2,359 × 2) | 4,718 |
| `.rodata.cmaps` + `font_dsc` + `lv_font_t` | 84 |
| **flash total** | **198,326** |
| `.data` / `.bss` | **0** — glyphs are read in place from flash |

Plus 36 B of `.bss` in `ui.cpp` for the composed fallback font
(`.bss._ZZL13msg_body_fontvE2kr`, `0x24` in the map).

On `waveshare_amoled_216_c6` (the tightest target — C6, no PSRAM, 6,553,600 B
app partition) the build lands at **37.5% flash (2,458,258 B) / 51.8% RAM
(169,796 B)**. Flash is the font; RAM is `SESSION_MSG_MAX` going 48 → 104
bytes, which is (104 − 48) × `SESSION_MAX_ROWS` = **336 B exactly** — the map
shows `.bss._ZL8sessions` at `0x3c4` = 964 B, up from 628.

The whole-block alternative (11,172 syllables, 839,979 B of bitmap /
**929,387 B** of flash — measured, not estimated) would have put flash past
48%.

### UTF-8 and byte-wise truncation

Everything downstream of the wire used to cut strings by **bytes**, which was
free of consequence while the wire was ASCII 32..126. A Korean syllable is
three bytes, so an unaligned cut leaves a dangling continuation byte and LVGL
draws the remainder as placeholder boxes — an elide that makes a message *less*
readable rather than shorter. Four places had to learn about it:

* `main.cpp` `parse_sessions()` — the `snprintf` into `SessionRow::msg` plus
  the fixed-offset `"..."` marker at `sizeof(msg) - 4`.
* `main.cpp` `parse_sessions()` — the `snprintf` into `SessionRow::label`.
  The host caps labels at 32 *characters*, so an 11-syllable name is 33 bytes
  into a 32-byte buffer. This one fails **silently**: LVGL's decoder walks off
  the end of the partial character and drops it without drawing anything, so
  the name is shortened with no ellipsis and no sign it was cut.
  `utf8_drop_partial_tail()` removes the partial character instead.
* `ui.cpp` `label_set_ellipsized()` — the middle-elide's head *and* its
  4-byte tail.
* `ui.cpp` `label_set_clamped()` — the message body's tail elide.

The `ui.cpp` pair walk back off continuation bytes (`utf8_floor()`);
`main.cpp` additionally checks whether the character the tail *starts* is
complete, because there a well-formed string is the normal case and backing
off a continuation byte unconditionally would eat a good character.

The host side is the same rule in Python: `clawdmeter_inbox._cut_bytes()`
truncates the UTF-8 encoding and decodes with `errors="ignore"`.

### Korean on the wire (what the host does)

A font nobody sends anything to is dead weight, so the host half ships in the
same change. The fold now lives in `clawdmeter_sessions.py` — next to
`elide_label` and `fit_payload`, because *every* label goes through
`fit_payload` on the way to the wire and every label needs folding, not just
the inbox's sender field.

* `fold_to_ascii(text, translit=True, keep_hangul=False)` gained a pass, ahead
  of `romanize_hangul()`, that passes a syllable through unchanged when it is
  one of the 2,350 the font actually has (`cs.KSX1001_HANGUL`, derived exactly
  the way the font was: `chr(cp).encode("euc_kr")`, keep lead bytes
  `0xB0..0xC8`). Everything else keeps folding — the font has no Jamo
  (U+1100..U+11FF), no compatibility Jamo (U+3130..U+318F), no Han, no emoji,
  and none of the 8,822 CP949-extension syllables.
* `to_panel_text()`'s "did anything survive" test was `[A-Za-z0-9]`, which
  declared a pure-Korean body unreadable — the pass that made it readable,
  immediately overruled. It accepts Hangul when `keep_hangul` is on.
* `keep_hangul` defaults **off at the function level**, so a caller has to ask
  for it. Exactly one does: the message BODY, in `message_row()`.
  **`panel_sender()` and `panel_label()` keep romanising** — their fonts have
  no fallback (see "Fallback fonts" above).
* `inbox_hangul = off` in the daemon config puts the body back to romanised.
  That is the setting for a host paired with firmware older than this font,
  which would draw the Hangul as empty boxes.

The invariant the tests hold is: *every string on the wire is drawable in the
font the field it lands in actually uses* —
`daemon/tests/test_inbox.py::test_everything_that_reaches_the_panel_is_in_a_font_that_field_has`.

#### Byte budget, honestly

Korean is 3 bytes per character in UTF-8, and four caps sit in series. The
firmware ones no longer bind; the *wire* one does, and it is a real constraint
rather than an oversight.

| cap | where | in Korean syllables |
|---|---|---|
| `SESSION_MSG_MAX` = 104 B | `firmware/src/data.h` | **34** (33 + `...`) |
| 2 body lines × 422 px ÷ 26.31 px advance | list card | 32 |
| 3 body lines | ONE-CHAT card | 48 |
| `MSG_TEXT_MAX` = 96 **bytes** | `clawdmeter_inbox.py` | 32 |
| `text_max_for_budget(budget)` = `budget // 4` **bytes** | ditto | 15 at the default |
| `DEFAULT_BUDGET_BYTES` = 180 B | `clawdmeter_sessions.py` | see below |

`SESSION_MSG_MAX` used to be 48 B, which is **15** syllables — *less than one*
of the two lines the card can draw, so every Korean message arrived
pre-truncated to half a card while the same card gave ASCII two full lines.
104 buys the full two lines (32 syllables = 96 B, + `...` + NUL) and costs
`(104 − 48) × SESSION_MAX_ROWS` = **336 B of internal SRAM**, taking the one
`SessionList` instance from 628 B to 964 B — confirmed in the C6 map
(`.bss._ZL8sessions` = `0x3c4`).

On the wire, measured against the real row shape
(`["79","CLAWDMETER",11,-1,42,0,0,0,0,0,0,-1,-1,"…"]`, `ensure_ascii=False`):

| body | payload |
|---|---|
| 15 Korean syllables | 103 B |
| 21 Korean syllables | 121 B |
| 32 Korean syllables | 154 B |
| 40 ASCII characters | 98 B |

So a 32-syllable message eats 154 of the default 180-byte budget and evicts
essentially every session card. `text_max_for_budget()` now counts **bytes**
(`budget // 4` *characters* was the right share for ASCII and 3× too generous
for Korean), which keeps the fit honest at any budget — but it also means the
budget is what limits Korean now: **15 syllables at 180, 21 at 260**.

`DEFAULT_BUDGET_BYTES` deliberately stays at 180. It is not sized for Korean;
it is sized for the 185-byte MTU floor an unlucky host BLE stack may hand you
(`daemon/SESSIONS.md`), and raising the default would trade a silent
truncation on those stacks for four more syllables on good ones. Korean users
on a normal stack should set `sessions_budget_bytes = 260`, which
`SESSIONS.md` already recommends for message rows.

### Korean typeface / licence

`assets/NanumGothic-Regular.ttf` is **NanumGothic 3.020** by Sandoll
Communications for Naver (NHN), under the **SIL Open Font License 1.1**, which
permits redistribution and permits deriving a bitmap font from it. It was
chosen over the Malgun Gothic that ships with Windows precisely because Malgun
is proprietary Microsoft/Sandoll and cannot be redistributed or converted.

**Where the grant actually comes from — read this before trusting the file.**
Nothing inside the TTF says "OFL". Its `name` table reads:

| nameID | value |
|---|---|
| 0 (copyright) | `Copyright © 2011 NHN Corporation. All rights reserved. Font designed by Sandoll Communications Inc.` |
| 13 (licence) | `NHN Corporation` |
| 14 (licence URL) | `http://www.nhncorp.com` |

That is the same in every build of this font, including Naver's own — the
grant travels with the *distribution*, not with the binary. So the vendored
copy is taken from the distribution that carries it: Google Fonts'
`ofl/nanumgothic/`, whose `METADATA.pb` says `license: "OFL"` and whose
`OFL.txt` names the Nanum family and its Reserved Font Names explicitly. Both
files came from that directory in the same fetch:

| file | bytes | SHA-256 |
|---|---|---|
| `assets/NanumGothic-Regular.ttf` | 2,054,744 | `76f45ef4a6bcff344c837c95a7dcc26e017e38b5846d5ae0cdcb5b86be2e2d31` |
| `assets/OFL.txt` | 4,534 | `eeacf16032901d0ed0456876ec77b8f0fda6b3fecec7d972f8543eb602e6c30f` |

`assets/OFL.txt` is there because OFL 1.1 §2 requires the licence text to
travel with any redistributed copy — without it this repo would be publishing
a 4 MB binary whose own metadata says "All rights reserved" and a doc claiming
otherwise. (The earlier vendored copy was
`C:\Windows\Fonts\NanumGothic-Regular.ttf`, SHA-256 `eafdda9e…`, a Windows
system font with no `nameID 13` at all and no distribution around it. It has
been replaced. The upstream one is also half the size and covers the same
11,172 syllables.)

One nuance recorded rather than resolved: OFL §3 reserves the name "NanumGothic"
for unmodified versions, and the generated bitmap is a Modified Version. The C
symbol `font_nanum_kr_28` is an internal identifier and is never presented to a
user as a font name, so it is a description of provenance rather than a name
claim — but if that ever ships in a user-visible font menu, rename it.

### Coverage choice: 2,350 syllables, not 11,172

`--ksx1001` selects the KS X 1001 wansung set — the syllables that live in
EUC-KR rows 0xB0..0xC8. The lead-byte test is what does the work: CPython's
`euc_kr` codec is really CP949 and encodes **all 11,172** syllables without
error, so "does it encode?" is not a filter. Round-trip each syllable and keep
the ones whose first byte is 0xB0..0xC8 and you get exactly 2,350 (verified
against the generated `unicode_list_0[]`: same 2,350 codepoints, sorted, no
duplicates). That is 2,350 of the 11,172 precomposed Hangul
syllables in U+AC00..U+D7A3, and it covers essentially all real Korean text;
the other 8,822 only exist in the CP949 extension area. Measured at
28 px / 1 bpp: 2,350 glyphs = 174,644 B of bitmap / 198,326 B of flash,
against 11,172 glyphs = 839,979 B of bitmap / 929,387 B of flash for the whole
block.

The cost of the choice is a *silent* one, so it is worth naming: a syllable
outside KS X 1001 draws as an empty box, with nothing to say why. That is why
the host's pass-through set is derived by the same `euc_kr` lead-byte test
rather than by a range check — `cs.KSX1001_HANGUL` and `--ksx1001` have to
agree exactly, or the daemon starts sending codepoints the font does not
have.

### cmap layout

The tool picks cmap subtables by exact dynamic programming over the
contiguous runs of the requested charset, weighing one more
`lv_font_fmt_txt_cmap_t` (~24 B) against 2 bytes per codepoint for a
`unicode_list`:

* a dense run becomes `LV_FONT_FMT_TXT_CMAP_FORMAT0_TINY` (no side table);
* scattered runs get merged into `LV_FONT_FMT_TXT_CMAP_SPARSE_TINY` with a
  `uint16` `unicode_list` of **relative** codepoints (`cp - range_start`),
  which is what LVGL binary-searches.

For ASCII that yields the same single `format0_tiny` table `lv_font_conv`
emits. For the KS X 1001 subset — 1,613 contiguous runs — it yields one
`sparse_tiny` table (4.7 KB of list) instead of 1,613 subtables (39 KB).
Subtables are emitted sorted and non-overlapping by construction. A subtable
also never spans more than 65,535 codepoints, because `range_length` is a
`uint16` and an overflow there does not fail — it truncates, and LVGL then
resolves codepoints past the truncation into unrelated glyphs. That is
enforced twice: `contiguous_runs()` splits any run longer than the limit
*before* the DP runs (so no candidate group is unrepresentable), and
`plan_cmaps()` re-checks `range_length`, `glyph_id_start` and `list_length`
against `uint16` before returning, raising rather than emitting a wrong
font.

### Bitmap packing (verified, not assumed)

`LV_FONT_FMT_TXT_PLAIN` with `lv_font_fmt_txt_dsc_t.stride == 0` is a
**continuous MSB-first bitstream with no per-row padding** — only the glyph
as a whole is padded up to a byte. It is *not* row-padded, which is the
natural assumption. Check it against a shipped font rather than trusting
either claim: `font_styrene_28`'s `$` is a 13x27 box at 4 bpp and occupies
176 bytes (`ceil(13*27*4/8)`), where a row-padded layout would need
`ceil(13*4/8)*27 = 189`.

### Known differences from `lv_font_conv`

Regenerating `font_styrene_28.c` (StyreneB-Regular.otf, size 28, bpp 4,
`-r 0x20-0x7E`) and diffing against the committed file:

| | result |
|---|---|
| glyph count, cmap shape, `lv_font_t` field set/order | identical |
| `lv_font_fmt_txt_dsc_t` fields | identical except `kern_dsc` / `kern_scale` |
| `adv_w` | **95/95 exact** |
| `box_w` / `ofs_x` | 96% exact, 99% within 1 px |
| `box_h` / `ofs_y` | 75%/85% exact, **100% within 1 px** |
| `underline_position` / `underline_thickness` | **11/11 exact** across every shipped font |
| `line_height` | 29 vs 30 |
| bitmap array | 12,172 B vs 12,051 B (+1%) |

The remaining differences all come from one cause and one omission:

* **Rasteriser.** Pillow renders through FreeType *with hinting*;
  `lv_font_conv` renders unhinted through opentype.js. Stems snap to the
  pixel grid, so a few boxes move by a pixel (`X` is 20 px wide hinted vs 22
  unhinted, with `ofs_x` compensating). At 1 bpp — the mode the Korean font
  uses — hinting is a clear win, so this is not worth "fixing". Advance
  widths are read straight out of `hmtx` and scaled, which is why they match
  exactly despite the different rasteriser.
* **No kerning.** The tool emits `kern_dsc = NULL`. Kerning does not apply
  across a font boundary in LVGL anyway, so a fallback font never needed it;
  if you regenerate a *Latin* font with this tool you will lose the 785 kern
  pairs the shipped Styrene 28 carries. Use `lv_font_conv` for those.

`line_height` and `base_line` are derived the way `lv_font_conv` derives
them, which is worth writing down because it is not what the font's own
metrics say: they come from the **rendered ink box of the selected glyphs**,
not from `hhea`/`OS/2`:

```
base_line   = -min(ofs_y)
line_height =  max(ofs_y + box_h) - min(ofs_y)
```

That rule reproduces the `line_height`/`base_line` pair in all 11 shipped
`firmware/src/font_*.c` files exactly. (StyreneB's `hhea` would give
36/7 at size 28, not the 30/6 the shipped file carries.)
`font_nanum_kr_28` is the one file that overrides it, with `--line-height 30
--base-line 6`, because it is a fallback and has to report the line box of the
font it hangs off. Its own ink would say **28/4** — measured by regenerating
without the two flags.

`underline_position` and `underline_thickness`, by contrast, are NOT derived —
they come straight out of the source font's `post` table, scaled to the pixel
size (`round(post.underlinePosition * size / upem)`). That rule reproduces
both fields on all 11 shipped fonts; a size heuristic reproduces 5 and is off
by up to 2 px on both (`font_tiempos_56` is `(-5, 2)`, a heuristic says
`(-3, 4)`).

### Verifying a generated font

The documented failure mode here is a font that compiles and draws
*nothing*. Always look at it:

```bash
$env:Path = "C:/Users/jelly/AppData/Local/Programs/mingw64/bin;" + $env:Path
pio run -d firmware -e sim
$env:SDL_VIDEODRIVER="dummy"; $env:SIM_AUTOSHOT_MS="5000"
$env:SIM_AUTOSHOT_PATH="shot.bmp"
cd firmware; .\.pio\build\sim\program.exe
python -c "from PIL import Image; Image.open('shot.bmp').save('shot.png')"
```

...and open `shot.png`. See CLAUDE.md § "QA your own UI changes" for the
hardware `screenshot` command and the boot-screen swap trick.
