# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "polars==1.42.0",
#   "chess==1.11.2",
#   "resvg-py==0.3.3",
#   "pillow==12.2.0",
#   "jinja2==3.1.6",
# ]
# ///
"""
kobo_chess site builder.

Pipeline:
  1. SELECT  per-Elo-band puzzle pool from data/*.parquet (deterministic, hash-based).
  2. RENDER  each puzzle + solution as small grayscale PNG boards (parallel).
  3. GENERATE plain static HTML pages (one puzzle per page, solution on its own page).

Output goes to ./site/  (gitignored). Same config + same dataset => same site.

Usage:
  uv run build.py                 # full build from config.toml
  uv run build.py --limit 3       # quick test: 3 puzzles per band
  uv run build.py --config x.toml --out site
"""
from __future__ import annotations
import argparse, html, io, json, os, sys, hashlib, tomllib, zipfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import polars as pl
import chess, chess.svg
import resvg_py
from PIL import Image, ImageDraw, ImageFont
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parent
DATA_GLOB = str(ROOT / "data" / "*.parquet")

# Relaxation tiers: (popularity_min, nbplays_min, rating_dev_max).
# A band uses the strictest tier that yields >= per_band puzzles.
def make_tiers(cfg):
    return [
        (cfg["popularity_min"], cfg["nbplays_min"], cfg["rating_dev_max"]),
        (80, 500, 100),
        (50, 100, 150),
        (-100, 0, 9999),  # anything in the band with a usable line
    ]

GRID_COLS = 5  # puzzles per row on a band index page


# ---------- helpers ----------
def hkey(seed: int, pid: str) -> bytes:
    return hashlib.blake2b(f"{seed}:{pid}".encode(), digest_size=8).digest()

def valid_puzzle(fen: str, moves_str: str) -> bool:
    """Cheap legality check (no rendering) so page numbering never has gaps."""
    try:
        b = chess.Board(fen)
        ms = moves_str.split()
        if len(ms) < 2:
            return False
        for u in ms:
            mv = chess.Move.from_uci(u)
            if mv not in b.legal_moves:
                return False
            b.push(mv)
        return True
    except Exception:
        return False

def san_line(board: chess.Board, ucis: list[str]) -> str:
    """Full solution line in SAN, e.g. '24... Nxe4 25. Bxf7+ Kxf7'."""
    b = board.copy()
    parts = []
    for i, u in enumerate(ucis):
        mv = chess.Move.from_uci(u)
        san = b.san(mv)
        if b.turn == chess.WHITE:
            parts.append(f"{b.fullmove_number}. {san}")
        elif i == 0:
            parts.append(f"{b.fullmove_number}... {san}")
        else:
            parts.append(san)
        b.push(mv)
    return " ".join(parts)

def svg_to_gray_png(svg: str, path: Path, colors: int = 16) -> None:
    raw = resvg_py.svg_to_bytes(svg_string=svg)
    im = Image.open(io.BytesIO(bytes(raw))).convert("RGBA")
    bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
    bg.alpha_composite(im)
    bg.convert("L").quantize(colors=colors).save(path, optimize=True)


# ---------- render worker (runs in a separate process) ----------
def render_one(task):
    pid, fen, moves_str, rating, img_dir, idx, size = task
    try:
        moves = moves_str.split()
        board = chess.Board(fen)
        setup = chess.Move.from_uci(moves[0])
        board.push(setup)                       # opponent's setup move
        solver = board.turn                     # solver is to move now
        pchk = board.king(board.turn) if board.is_check() else None
        psvg = chess.svg.board(board, orientation=solver, lastmove=setup,
                               check=pchk, size=size, coordinates=True)
        svg_to_gray_png(psvg, Path(img_dir) / f"p{idx:03d}.png")

        sol = moves[1:]
        san = san_line(board, sol)
        b2 = board.copy()
        first = chess.Move.from_uci(sol[0])
        b2.push(first)
        schk = b2.king(b2.turn) if b2.is_check() else None
        ssvg = chess.svg.board(b2, orientation=solver, lastmove=first,
                               check=schk, size=size, coordinates=True)
        svg_to_gray_png(ssvg, Path(img_dir) / f"s{idx:03d}.png")

        color = "White to move" if solver == chess.WHITE else "Black to move"
        return {"idx": idx, "ok": True, "pid": pid, "color": color,
                "san": san, "rating": int(rating)}
    except Exception as e:
        return {"idx": idx, "ok": False, "pid": pid, "err": repr(e)}


# ---------- selection ----------
def select_pool(cfg, per_band):
    """Return list of bands; each band is dict(lo,hi,tier,puzzles=[{pid,fen,moves,rating}...])."""
    tiers = make_tiers(cfg)
    seed = cfg["seed"]
    buffer = 25  # over-select, then drop any illegal puzzles, then keep per_band

    print("Scanning dataset for candidates ...", flush=True)
    cand = (
        pl.scan_parquet(DATA_GLOB)
        .with_columns(pl.col("Moves").str.split(" ").list.len().alias("plies"))
        .filter(
            (pl.col("Rating") >= cfg["rating_min"])
            & (pl.col("Rating") < cfg["rating_max"])
            & (pl.col("plies") >= 2)
            & (pl.col("plies") <= 2 * cfg["max_solver_moves"])
        )
        .select(["PuzzleId", "Rating", "Popularity", "NbPlays", "RatingDeviation"])
        .collect(engine="streaming")
    )
    print(f"  {cand.height:,} candidate rows in range", flush=True)

    bands = []
    picked_ids = []  # (pid) we still need FEN/Moves for
    for lo in range(cfg["rating_min"], cfg["rating_max"], cfg["band_width"]):
        hi = lo + cfg["band_width"]
        bdf = cand.filter((pl.col("Rating") >= lo) & (pl.col("Rating") < hi))
        chosen = None
        used_tier = len(tiers) - 1
        for ti, (pmin, nmin, rdmax) in enumerate(tiers):
            t = bdf.filter(
                (pl.col("Popularity") >= pmin)
                & (pl.col("NbPlays") >= nmin)
                & (pl.col("RatingDeviation") <= rdmax)
            )
            if t.height >= per_band or ti == len(tiers) - 1:
                chosen = t
                used_tier = ti
                break
        rows = list(zip(chosen["PuzzleId"].to_list(), chosen["Rating"].to_list()))
        rows.sort(key=lambda pr: hkey(seed, pr[0]))
        rows = rows[: per_band + buffer]
        bands.append({"lo": lo, "hi": hi, "tier": used_tier,
                      "ordered": rows, "puzzles": []})
        picked_ids.extend(pid for pid, _ in rows)
        print(f"  band {lo}-{hi}: tier {used_tier}, {len(rows)} pre-picked "
              f"(from {chosen.height:,})", flush=True)

    # Fetch FEN/Moves only for the pre-picked ids (small second scan).
    print("Fetching positions for picked puzzles ...", flush=True)
    need = set(picked_ids)
    fetched = (
        pl.scan_parquet(DATA_GLOB)
        .filter(pl.col("PuzzleId").is_in(list(need)))
        .select(["PuzzleId", "FEN", "Moves"])
        .collect(engine="streaming")
    )
    fen_by = {pid: (fen, mv) for pid, fen, mv in
              zip(fetched["PuzzleId"].to_list(), fetched["FEN"].to_list(), fetched["Moves"].to_list())}

    # Validate in hash order, keep first per_band legal puzzles per band.
    for band in bands:
        keep = []
        for pid, rating in band["ordered"]:
            fm = fen_by.get(pid)
            if not fm:
                continue
            fen, moves = fm
            if valid_puzzle(fen, moves):
                keep.append({"pid": pid, "fen": fen, "moves": moves, "rating": int(rating)})
            if len(keep) >= per_band:
                break
        band["puzzles"] = keep
        del band["ordered"]
    return bands


# ---------- EPUB (one per band) ----------
# The Kobo browser downloads a tapped .epub link straight into the library, so each
# band page offers its puzzles as an offline book: puzzle on one page, solution on
# the next (a page turn reveals it). EPUB2 + NCX for the old RMSDK renderer.
EPUB_CSS = """body { margin: 0; padding: 0; text-align: center; }
h1 { font-size: 1.2em; margin: 0.4em 0 0.1em; }
p { margin: 0.4em 0; }
img.board { width: 95%; }
img.cover { width: 100%; }
.head { font-size: 0.9em; color: #444; margin: 0.3em 0 0; }
.san { font-size: 1.1em; line-height: 1.6; }
.muted { color: #555; font-size: 0.85em; }
"""

def _xhtml(title: str, body: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN"'
        ' "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">\n<head>\n'
        f"<title>{html.escape(title)}</title>\n"
        '<link rel="stylesheet" type="text/css" href="style.css"/>\n'
        f"</head>\n<body>\n{body}\n</body>\n</html>\n"
    )

def _cover_png(lo: int, hi: int, count: int, pool_id: str) -> bytes:
    """Simple deterministic grayscale cover so the Kobo library stays scannable."""
    W, H = 600, 800
    im = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(im)
    d.rectangle([10, 10, W - 11, H - 11], outline=0, width=4)

    def fit(text, max_w, start):
        size = start
        while size > 12:
            f = ImageFont.load_default(size=size)
            if d.textlength(text, font=f) <= max_w:
                return f
            size -= 4
        return ImageFont.load_default(size=12)

    def center(y, text, font, fill=0):
        d.text(((W - d.textlength(text, font=font)) / 2, y), text, font=font, fill=fill)

    sq = 40  # checkerboard strips top and bottom
    for y0 in (48, H - 48 - sq):
        for c in range(12):
            x0 = 60 + c * sq
            if c % 2 == 0:
                d.rectangle([x0, y0, x0 + sq - 1, y0 + sq - 1], fill=0)
            else:
                d.rectangle([x0, y0, x0 + sq - 1, y0 + sq - 1], outline=0, width=1)

    center(180, "CHESS PUZZLES", fit("CHESS PUZZLES", 460, 56))
    center(300, f"{lo}-{hi}", fit(f"{lo}-{hi}", 480, 140))
    center(470, "rating band", fit("rating band", 300, 34))
    center(560, f"{count} puzzles", fit(f"{count} puzzles", 300, 34))
    center(630, f"pool {pool_id}", fit(f"pool {pool_id}", 300, 26))
    buf = io.BytesIO()
    im.quantize(colors=16).save(buf, format="PNG", optimize=True)
    return buf.getvalue()

def build_band_epub(path: Path, pool_id: str, lo: int, hi: int, items, img_dir: Path) -> int:
    """items: [(idx, render_meta)] in page order. Returns the EPUB size in bytes."""
    n = len(items)
    title = f"Chess Puzzles {lo}-{hi} ({pool_id})"
    uid = f"urn:kobo-chess:{pool_id}:{lo:04d}-{hi:04d}"

    files: list[tuple[str, bytes]] = []  # (arcname, data) in archive order
    manifest = ['<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
                '<item id="css" href="style.css" media-type="text/css"/>',
                '<item id="cover-img" href="cover.png" media-type="image/png"/>',
                '<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>',
                '<item id="about" href="about.xhtml" media-type="application/xhtml+xml"/>']
    spine = ['<itemref idref="cover"/>', '<itemref idref="about"/>']
    nav = [(f"Chess Puzzles {lo}-{hi}", "about.xhtml")]

    files.append(("OEBPS/style.css", EPUB_CSS.encode()))
    files.append(("OEBPS/cover.png", _cover_png(lo, hi, n, pool_id)))
    files.append(("OEBPS/cover.xhtml", _xhtml(
        title, '<div><img class="cover" src="cover.png" alt="cover"/></div>').encode()))
    files.append(("OEBPS/about.xhtml", _xhtml(title, (
        f"<h1>Chess Puzzles {lo}-{hi}</h1>"
        f"<p>{n} puzzles around rating {lo}-{hi}, pool {pool_id}.</p>"
        "<p>Each puzzle shows the position after the opponent's last move"
        " (the highlighted one). Find the best move for the side to move."
        " Turn the page to see the solution.</p>"
        '<p class="muted">Puzzles from the Lichess puzzle database (CC0).'
        " Not affiliated with Lichess.</p>")).encode()))

    for idx, m in items:
        pimg = (img_dir / f"p{idx:03d}.png").read_bytes()
        simg = (img_dir / f"s{idx:03d}.png").read_bytes()
        pbody = (f'<p class="head">Puzzle {idx} of {n}</p>'
                 f"<h1>{html.escape(m['color'])}</h1>"
                 f'<div><img class="board" src="img/p{idx:03d}.png"'
                 f' alt="chess position, {html.escape(m["color"])}"/></div>'
                 "<p>Find the best move.</p>")
        sbody = (f'<p class="head">Solution {idx} of {n}</p>'
                 f'<div><img class="board" src="img/s{idx:03d}.png" alt="solution position"/></div>'
                 f'<p class="san">{html.escape(m["san"])}</p>'
                 f'<p class="muted">Rating {m["rating"]} &#183;'
                 f' lichess.org/training/{html.escape(m["pid"])}</p>')
        files.append((f"OEBPS/p{idx:03d}.xhtml", _xhtml(f"Puzzle {idx}", pbody).encode()))
        files.append((f"OEBPS/s{idx:03d}.xhtml", _xhtml(f"Solution {idx}", sbody).encode()))
        files.append((f"OEBPS/img/p{idx:03d}.png", pimg))
        files.append((f"OEBPS/img/s{idx:03d}.png", simg))
        manifest += [
            f'<item id="p{idx:03d}" href="p{idx:03d}.xhtml" media-type="application/xhtml+xml"/>',
            f'<item id="s{idx:03d}" href="s{idx:03d}.xhtml" media-type="application/xhtml+xml"/>',
            f'<item id="ip{idx:03d}" href="img/p{idx:03d}.png" media-type="image/png"/>',
            f'<item id="is{idx:03d}" href="img/s{idx:03d}.png" media-type="image/png"/>',
        ]
        spine += [f'<itemref idref="p{idx:03d}"/>', f'<itemref idref="s{idx:03d}"/>']
        nav.append((f"Puzzle {idx}", f"p{idx:03d}.xhtml"))

    opf = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid" version="2.0">\n'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:opf="http://www.idpf.org/2007/opf">\n'
        f"<dc:title>{html.escape(title)}</dc:title>\n"
        "<dc:creator>Lichess puzzle database</dc:creator>\n"
        "<dc:language>en</dc:language>\n"
        f'<dc:identifier id="bookid">{uid}</dc:identifier>\n'
        '<meta name="cover" content="cover-img"/>\n'
        "</metadata>\n<manifest>\n" + "\n".join(manifest) + "\n</manifest>\n"
        '<spine toc="ncx">\n' + "\n".join(spine) + "\n</spine>\n"
        '<guide><reference type="cover" title="Cover" href="cover.xhtml"/></guide>\n'
        "</package>\n"
    )
    navpoints = "\n".join(
        f'<navPoint id="n{i}" playOrder="{i}"><navLabel><text>{html.escape(label)}</text>'
        f'</navLabel><content src="{href}"/></navPoint>'
        for i, (label, href) in enumerate(nav, start=1)
    )
    ncx = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        f'<head><meta name="dtb:uid" content="{uid}"/>'
        '<meta name="dtb:depth" content="1"/>'
        '<meta name="dtb:totalPageCount" content="0"/>'
        '<meta name="dtb:maxPageNumber" content="0"/></head>\n'
        f"<docTitle><text>{html.escape(title)}</text></docTitle>\n"
        f"<navMap>\n{navpoints}\n</navMap>\n</ncx>\n"
    )
    container = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        '<rootfiles><rootfile full-path="OEBPS/content.opf"'
        ' media-type="application/oebps-package+xml"/></rootfiles>\n'
        "</container>\n"
    )
    files = [("META-INF/container.xml", container.encode()),
             ("OEBPS/content.opf", opf.encode()),
             ("OEBPS/toc.ncx", ncx.encode())] + files

    # Fixed timestamps + fixed entry order keep the build byte-reproducible.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zi = zipfile.ZipInfo("mimetype", date_time=(1980, 1, 1, 0, 0, 0))
        zi.compress_type = zipfile.ZIP_STORED
        zi.external_attr = 0o100644 << 16
        zf.writestr(zi, "application/epub+zip")
        for name, data in files:
            zi = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o100644 << 16
            zf.writestr(zi, data)
    data = buf.getvalue()
    path.write_bytes(data)
    return len(data)

def human_size(nbytes: int) -> str:
    return f"{nbytes / 1024:.0f} KB" if nbytes < 1024 * 1024 else f"{nbytes / 1048576:.1f} MB"


# ---------- generation ----------
def build(cfg, out_dir: Path, per_band: int, workers: int):
    env = Environment(
        loader=FileSystemLoader(str(ROOT / "templates")),
        autoescape=select_autoescape(["html"]),
        keep_trailing_newline=True,
    )
    pool_id = cfg["pool_id"]
    board_px = cfg["board_px"]
    pool_dir = out_dir / "p" / pool_id

    bands = select_pool(cfg, per_band)

    # ----- render all boards in parallel -----
    tasks = []
    for band in bands:
        bdir = pool_dir / f"b{band['lo']:04d}"
        (bdir / "img").mkdir(parents=True, exist_ok=True)
        for i, pz in enumerate(band["puzzles"], start=1):
            tasks.append((pz["pid"], pz["fen"], pz["moves"], pz["rating"],
                          str(bdir / "img"), i, board_px))
    print(f"\nRendering {len(tasks)*2} board images on {workers} workers ...", flush=True)
    meta = {}  # (lo, idx) -> render metadata
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        # map tasks back to their band lo via index ranges
        offsets = []
        k = 0
        for band in bands:
            offsets.append((k, k + len(band["puzzles"]), band["lo"]))
            k += len(band["puzzles"])
        for j, res in enumerate(ex.map(render_one, tasks)):
            lo = next(lo for a, b, lo in offsets if a <= j < b)
            meta[(lo, res["idx"])] = res
            done += 1
            if done % 100 == 0 or done == len(tasks):
                print(f"  {done}/{len(tasks)} puzzles rendered", flush=True)

    failures = [(lo, r) for (lo, _), r in meta.items() if not r["ok"]]
    if failures:
        print(f"  WARNING: {len(failures)} render failures (should be ~0 after validation)")

    # ----- generate HTML -----
    print("\nGenerating HTML pages and band EPUBs ...", flush=True)
    home_bands = []
    epub_info = {}  # lo -> (filename, bytes)
    total = 0
    for band in bands:
        lo, hi = band["lo"], band["hi"]
        bdir = pool_dir / f"b{lo:04d}"
        ok = [(i, meta[(lo, i)]) for i in range(1, len(band["puzzles"]) + 1)
              if meta[(lo, i)]["ok"]]
        n = len(ok)
        total += n
        home_bands.append({"lo": lo, "hi": hi, "count": n,
                           "href": f"p/{pool_id}/b{lo:04d}/index.html"})

        # band EPUB (offline copy for the e-reader library)
        epub_name = f"chess-puzzles-{pool_id}-{lo:04d}-{hi:04d}.epub"
        epub_bytes = build_band_epub(bdir / epub_name, pool_id, lo, hi, ok, bdir / "img")
        epub_info[lo] = (epub_name, epub_bytes)

        # puzzle + solution pages
        for pos, (idx, m) in enumerate(ok):
            prev_idx = ok[pos - 1][0] if pos > 0 else None
            next_idx = ok[pos + 1][0] if pos < n - 1 else None
            (bdir / f"p{idx:03d}.html").write_text(env.get_template("puzzle.html").render(
                title=f"Puzzle {idx} ({lo}-{hi})", board_px=board_px,
                color=m["color"], img=f"img/p{idx:03d}.png",
                solution_href=f"s{idx:03d}.html", n=idx, total=n, lo=lo, hi=hi,
                prev_href=f"p{prev_idx:03d}.html" if prev_idx else None,
                next_href=f"p{next_idx:03d}.html" if next_idx else None,
                index_href="index.html",
            ))
            (bdir / f"s{idx:03d}.html").write_text(env.get_template("solution.html").render(
                title=f"Solution {idx} ({lo}-{hi})", board_px=board_px,
                img=f"img/s{idx:03d}.png", san=m["san"], n=idx, total=n,
                rating=m["rating"], puzzle_id=m["pid"],
                puzzle_href=f"p{idx:03d}.html", index_href="index.html",
                next_href=f"p{next_idx:03d}.html" if next_idx else None,
            ))

        # band index (grid of puzzle links)
        cells = [{"n": idx, "href": f"p{idx:03d}.html"} for idx, _ in ok]
        rows = [cells[r:r + GRID_COLS] for r in range(0, len(cells), GRID_COLS)]
        (bdir / "index.html").write_text(env.get_template("band.html").render(
            title=f"Rating {lo}-{hi}", board_px=board_px, lo=lo, hi=hi,
            count=n, rows=rows, cols=GRID_COLS, home_href="../../../index.html",
            epub_href=epub_name, epub_size=human_size(epub_bytes),
        ))

    # home page
    (out_dir / "index.html").write_text(env.get_template("home.html").render(
        title="Chess puzzles", board_px=board_px, bands=home_bands,
        per_band=per_band, pool_id=pool_id, total=total,
    ))

    # manifest
    manifest = {
        "pool_id": pool_id, "seed": cfg["seed"], "per_band": per_band,
        "total_puzzles": total,
        "dataset_files": [
            {"name": p.name, "bytes": p.stat().st_size}
            for p in sorted((ROOT / "data").glob("*.parquet"))
        ],
        "bands": [
            {"lo": b["lo"], "hi": b["hi"], "tier": b["tier"],
             "count": len([1 for i in range(1, len(b["puzzles"]) + 1) if meta[(b["lo"], i)]["ok"]]),
             "epub": epub_info[b["lo"]][0], "epub_bytes": epub_info[b["lo"]][1],
             "puzzle_ids": [meta[(b["lo"], i)]["pid"]
                            for i in range(1, len(b["puzzles"]) + 1) if meta[(b["lo"], i)]["ok"]]}
            for b in bands
        ],
        "config": cfg,
    }
    (pool_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"\nDone: {total} puzzles across {len(bands)} bands -> {out_dir}", flush=True)
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.toml"))
    ap.add_argument("--out", default=str(ROOT / "site"))
    ap.add_argument("--limit", type=int, default=None, help="override per_band (quick test)")
    ap.add_argument("--pool-id", default=None, help="override config pool_id (e.g. monthly 2026-07)")
    ap.add_argument("--seed", type=int, default=None, help="override config seed")
    ap.add_argument("--workers", type=int, default=min(8, (os.cpu_count() or 2)))
    args = ap.parse_args()

    with open(args.config, "rb") as f:
        cfg = tomllib.load(f)
    if args.pool_id:
        cfg["pool_id"] = args.pool_id
    if args.seed is not None:
        cfg["seed"] = args.seed
    per_band = args.limit if args.limit is not None else cfg["per_band"]
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    build(cfg, out_dir, per_band, args.workers)


if __name__ == "__main__":
    main()
