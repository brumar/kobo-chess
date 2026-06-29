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
import argparse, io, json, os, sys, hashlib, tomllib
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import polars as pl
import chess, chess.svg
import resvg_py
from PIL import Image
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
    print("\nGenerating HTML pages ...", flush=True)
    home_bands = []
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
