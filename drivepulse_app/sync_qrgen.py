"""
Minimal QR code generator — byte mode, ECC level M, versions 1-10.
Pure Python, zero dependencies beyond the standard library.
Produces an SVG string; GdkPixbuf loads it via librsvg (always available with GTK4).
"""
from __future__ import annotations

# ── GF(256) ──────────────────────────────────────────────────────────────────

_EXP: list[int] = [0] * 512
_LOG: list[int] = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x = (_x << 1) ^ (0x11D if _x & 0x80 else 0)
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]
del _x, _i


def _gf(a: int, b: int) -> int:
    return 0 if not a or not b else _EXP[_LOG[a] + _LOG[b]]


def _pmul(p: list[int], q: list[int]) -> list[int]:
    r = [0] * (len(p) + len(q) - 1)
    for i, pi in enumerate(p):
        for j, qj in enumerate(q):
            r[i + j] ^= _gf(pi, qj)
    return r


def _gen(n: int) -> list[int]:
    g = [1]
    for i in range(n):
        g = _pmul(g, [1, _EXP[i]])
    return g


def _ecc(data: list[int], gen: list[int]) -> list[int]:
    msg = data + [0] * (len(gen) - 1)
    for i in range(len(data)):
        c = msg[i]
        if c:
            for j, gj in enumerate(gen):
                msg[i + j] ^= _gf(c, gj)
    return msg[len(data):]


# ── Version / block table — ECC level M ──────────────────────────────────────
# (ec_per_block, g1_count, g1_data, g2_count, g2_data)
_VER: dict[int, tuple[int, int, int, int, int]] = {
    1:  (10, 1, 16, 0, 0),
    2:  (16, 1, 28, 0, 0),
    3:  (26, 1, 44, 0, 0),
    4:  (18, 2, 32, 0, 0),
    5:  (24, 2, 43, 0, 0),
    6:  (16, 4, 27, 0, 0),
    7:  (18, 4, 31, 0, 0),
    8:  (22, 2, 38, 2, 39),
    9:  (22, 3, 36, 2, 37),
    10: (26, 4, 43, 1, 43),
}

# Alignment pattern center positions (versions 2-10)
_ALIGN: dict[int, list[int]] = {
    2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
    6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42],
    9: [6, 26, 46], 10: [6, 28, 50],
}

# Precomputed format info words for ECC level M (indicator=00), masks 0-7
# = (data_5bits << 10 | BCH_remainder) XOR 101010000010010
_FMT: dict[int, int] = {
    0: 0x5412, 1: 0x5125, 2: 0x5E7C, 3: 0x5B4B,
    4: 0x45F9, 5: 0x40CE, 6: 0x4ED7, 7: 0x4BA0,
}


def _capacity(v: int) -> int:
    ec, g1c, g1d, g2c, g2d = _VER[v]
    return g1c * g1d + g2c * g2d


def _choose_version(n: int) -> int:
    for v in range(1, 11):
        if _capacity(v) >= n:
            return v
    raise ValueError(f"Data too long for QR version ≤10: {n} bytes")


# ── Data encoding ─────────────────────────────────────────────────────────────

def _encode_data(text: str, version: int) -> list[int]:
    raw = text.encode("utf-8")
    cap = _capacity(version)
    assert len(raw) <= cap
    total_cw = _VER[version][1] * _VER[version][2] + _VER[version][3] * _VER[version][4]
    # byte mode indicator = 0100, char count (8 bits for v1-9, same for v1-9)
    bits: list[int] = []

    def _push(val: int, length: int) -> None:
        for shift in range(length - 1, -1, -1):
            bits.append((val >> shift) & 1)

    _push(0b0100, 4)            # byte mode
    _push(len(raw), 8)          # character count (v1-9: 8 bits)
    for byte in raw:
        _push(byte, 8)
    _push(0, 4)                 # terminator (up to 4 zeros)

    # Pad to byte boundary
    while len(bits) % 8:
        bits.append(0)

    # Convert to codewords and pad to capacity
    codewords: list[int] = []
    for i in range(0, len(bits), 8):
        codewords.append(int("".join(str(b) for b in bits[i:i+8]), 2))

    pad_bytes = [0xEC, 0x11]
    while len(codewords) < cap:
        codewords.append(pad_bytes[len(codewords) % 2])

    return codewords


# ── Reed-Solomon + interleaving ───────────────────────────────────────────────

def _build_codewords(text: str, version: int) -> list[int]:
    data_cw = _encode_data(text, version)
    ec_per, g1c, g1d, g2c, g2d = _VER[version]
    gen = _gen(ec_per)

    blocks_data: list[list[int]] = []
    blocks_ec:   list[list[int]] = []
    pos = 0
    for _ in range(g1c):
        blk = data_cw[pos:pos + g1d]; pos += g1d
        blocks_data.append(blk)
        blocks_ec.append(_ecc(blk, gen))
    for _ in range(g2c):
        blk = data_cw[pos:pos + g2d]; pos += g2d
        blocks_data.append(blk)
        blocks_ec.append(_ecc(blk, gen))

    # Interleave data, then EC
    result: list[int] = []
    max_data = max(len(b) for b in blocks_data)
    for i in range(max_data):
        for b in blocks_data:
            if i < len(b):
                result.append(b[i])
    for i in range(ec_per):
        for b in blocks_ec:
            result.append(b[i])
    return result


# ── Module matrix ─────────────────────────────────────────────────────────────

def _make_matrix(version: int) -> tuple[list[list[int | None]], list[list[bool]]]:
    n = 21 + (version - 1) * 4
    mat:  list[list[int | None]] = [[None] * n for _ in range(n)]
    func: list[list[bool]]       = [[False] * n for _ in range(n)]

    def _set(r: int, c: int, val: int, is_func: bool = True) -> None:
        if 0 <= r < n and 0 <= c < n:
            mat[r][c] = val
            if is_func:
                func[r][c] = True

    def _finder(tr: int, tc: int) -> None:
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = tr + dr, tc + dc
                if not (0 <= r < n and 0 <= c < n):
                    continue
                if dr in (-1, 7) or dc in (-1, 7):
                    v = 0
                elif dr in (0, 6) or dc in (0, 6):
                    v = 1
                elif 2 <= dr <= 4 and 2 <= dc <= 4:
                    v = 1
                else:
                    v = 0
                _set(r, c, v)

    # Finder patterns
    _finder(0, 0)
    _finder(0, n - 7)
    _finder(n - 7, 0)

    # Timing
    for i in range(8, n - 8):
        _set(6, i, 1 if i % 2 == 0 else 0)
        _set(i, 6, 1 if i % 2 == 0 else 0)

    # Alignment patterns (not overlapping finder)
    if version >= 2:
        centers = _ALIGN[version]
        for r in centers:
            for c in centers:
                if func[r][c]:
                    continue
                for dr in range(-2, 3):
                    for dc in range(-2, 3):
                        v = 1 if (dr in (-2, 2) or dc in (-2, 2) or (dr == 0 and dc == 0)) else 0
                        _set(r + dr, c + dc, v)

    # Dark module
    _set(4 * version + 9, 8, 1)

    # Format info placeholders (reserve cells)
    fmt_positions = (
        [(8, c) for c in range(0, 6)] +
        [(8, 7), (8, 8), (7, 8)] +
        [(r, 8) for r in range(5, -1, -1)] +
        [(r, 8) for r in range(n - 7, n)] +
        [(8, c) for c in range(n - 8, n)]
    )
    for r, c in fmt_positions:
        _set(r, c, 0)

    return mat, func


def _place_data(mat: list[list[int | None]], func: list[list[bool]], codewords: list[int]) -> None:
    n = len(mat)
    bits: list[int] = []
    for cw in codewords:
        for shift in range(7, -1, -1):
            bits.append((cw >> shift) & 1)
    # Remainder bits (version-specific; for V1-6 = 0, V7-10 = 0 as well for M)
    # (all versions 1-6 have 0 remainder bits, 7+ have variable; level M versions 7-10 all 0)

    bit_idx = 0
    col = n - 1
    going_up = True
    while col >= 0:
        if col == 6:
            col -= 1
            continue
        for row_step in range(n):
            row = (n - 1 - row_step) if going_up else row_step
            for c_offset in (0, 1):
                c = col - c_offset
                if not func[row][c] and mat[row][c] is None:
                    if bit_idx < len(bits):
                        mat[row][c] = bits[bit_idx]
                        bit_idx += 1
                    else:
                        mat[row][c] = 0
        going_up = not going_up
        col -= 2


def _apply_mask(mat: list[list[int | None]], func: list[list[bool]], mask: int) -> list[list[int]]:
    n = len(mat)
    out = [[mat[r][c] or 0 for c in range(n)] for r in range(n)]
    cond = [
        lambda r, c: (r + c) % 2 == 0,
        lambda r, c: r % 2 == 0,
        lambda r, c: c % 3 == 0,
        lambda r, c: (r + c) % 3 == 0,
        lambda r, c: (r // 2 + c // 3) % 2 == 0,
        lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
        lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
        lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
    ][mask]
    for r in range(n):
        for c in range(n):
            if not func[r][c] and cond(r, c):
                out[r][c] ^= 1
    return out


def _write_format(grid: list[list[int]], n: int, mask: int) -> None:
    fmt = _FMT[mask]
    bits = [(fmt >> i) & 1 for i in range(14, -1, -1)]

    def _fb(idx: int, r: int, c: int) -> None:
        grid[r][c] = bits[idx]

    for i in range(6):
        _fb(i,     8, i)
        _fb(14-i,  i, 8)
    _fb(6, 8, 7); _fb(7, 8, 8); _fb(8, 7, 8)
    for i in range(6):
        _fb(9+i,  8, n-1-i)
        _fb(14-6-i, n-7+i, 8)  # bottom-left, bits 8..13


def _penalty(grid: list[list[int]]) -> int:
    n = len(grid)
    score = 0
    # Rule 1: five or more in a row
    for row in grid:
        run = 1
        for i in range(1, n):
            if row[i] == row[i-1]:
                run += 1
                if run == 5:
                    score += 3
                elif run > 5:
                    score += 1
            else:
                run = 1
    for c in range(n):
        run = 1
        for r in range(1, n):
            if grid[r][c] == grid[r-1][c]:
                run += 1
                if run == 5:
                    score += 3
                elif run > 5:
                    score += 1
            else:
                run = 1
    # Rule 2: 2x2 blocks
    for r in range(n - 1):
        for c in range(n - 1):
            v = grid[r][c]
            if v == grid[r][c+1] == grid[r+1][c] == grid[r+1][c+1]:
                score += 3
    # Rule 4: proportion of dark modules
    dark = sum(grid[r][c] for r in range(n) for c in range(n))
    pct = dark * 100 // (n * n)
    score += min(abs(pct - 50), abs(pct + 5 - 50)) * 2 // 5 * 10
    return score


# ── Public API ────────────────────────────────────────────────────────────────

def make_svg(text: str, module_px: int = 8, border: int = 4) -> str:
    version = _choose_version(len(text.encode("utf-8")))
    codewords = _build_codewords(text, version)
    mat, func = _make_matrix(version)
    _place_data(mat, func, codewords)

    best_grid: list[list[int]] = []
    best_score = 10 ** 9
    for mask in range(8):
        grid = _apply_mask(mat, func, mask)
        _write_format(grid, len(grid), mask)
        s = _penalty(grid)
        if s < best_score:
            best_score = s
            best_grid = grid

    n = len(best_grid)
    size = n * module_px + 2 * border * module_px
    rects = []
    for r in range(n):
        for c in range(n):
            if best_grid[r][c]:
                x = (c + border) * module_px
                y = (r + border) * module_px
                rects.append(f'<rect x="{x}" y="{y}" width="{module_px}" height="{module_px}"/>')

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{size}" height="{size}" viewBox="0 0 {size} {size}">'
        f'<rect width="{size}" height="{size}" fill="white"/>'
        f'<g fill="black">{"".join(rects)}</g>'
        f'</svg>'
    )


def make_pixbuf(text: str) -> "GdkPixbuf.Pixbuf":  # type: ignore[name-defined]
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf
    svg = make_svg(text)
    data = svg.encode("utf-8")
    loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
    loader.write(data)
    loader.close()
    pixbuf = loader.get_pixbuf()
    if pixbuf is None:
        raise RuntimeError("GdkPixbuf failed to render QR SVG (librsvg missing?)")
    return pixbuf
