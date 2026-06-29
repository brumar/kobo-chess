# kobo_chess

## Goal

Build a **Kobo-friendly static website** that serves a **curated subset** of Lichess
chess puzzles for solving on a Kobo e-reader's built-in browser.

- The site shows a rotating **pool** of puzzles. The pool is a small subset selected
  from the full 6M-puzzle dataset, and **it will change over time** (re-selected /
  regenerated periodically). Treat the pool as data-driven and the build as
  reproducible: same config + same dataset snapshot → same site.
- The reader is a Kobo e-ink device, not a desktop. Its browser is the hard constraint
  that drives every design decision (see "Kobo constraints" below).

Nothing is built yet beyond the downloaded dataset. Do not start implementing the site
or the pipeline unless asked.

## Repository layout

```
data/                     # Lichess puzzle dataset (parquet, ~826 MB, gitignore this)
  train-0000{0,1,2}-of-00003.parquet
README.md                 # HuggingFace dataset card (schema + field docs)
.gitattributes            # from HuggingFace
.cache/                   # HF downloader bookkeeping; safe to delete
CLAUDE.md                 # this file
```

Not a git repo yet. If/when it becomes one, **do not commit `data/`** (826 MB) or
`.cache/`. Add them to `.gitignore` first.

## The dataset

- Source: https://huggingface.co/datasets/Lichess/chess-puzzles (CC0-1.0, public domain).
- **6,014,381 puzzles** across 3 parquet files (~288 MB each). Updated monthly upstream;
  this snapshot is the Jun 11 2026 update.
- Re-download / refresh with:
  ```
  uv tool run --from "huggingface_hub[cli,hf_xet]" hf download \
    Lichess/chess-puzzles --repo-type dataset --local-dir .
  ```
  (Note: this `uvx` does not auto-route to `tool run`; use `uv tool run --from`. The
  `cli` extra warning from huggingface-hub 1.x is harmless — the `hf` command ships in
  the base package.)

### Schema (one row per puzzle)

| Field             | Type        | Notes |
|-------------------|-------------|-------|
| `PuzzleId`        | string      | Live at `https://lichess.org/training/{PuzzleId}` |
| `GameId`          | string      | Source game at `https://lichess.org/{GameId}` |
| `FEN`             | string      | Position **before the opponent's first move** (see below) |
| `Moves`           | string      | Space-separated **UCI** moves, e.g. `e8f7 e2e6 f7f8 e6f7` |
| `Rating`          | uint16      | Glicko-2 puzzle rating (difficulty) |
| `RatingDeviation` | uint16      | Glicko-2 deviation |
| `Popularity`      | int8        | -100..100, `100*(up-down)/(up+down)` |
| `NbPlays`         | uint32      | Times played |
| `Themes`          | list<str>   | e.g. `mate`, `mateIn2`, `fork`, `endgame` |
| `OpeningTags`     | list<str>   | Only set for puzzles before move 20 |

### Puzzle-rendering conventions (easy to get wrong)

- `FEN` is the position **before the opponent moves**. The first move in `Moves` is the
  **opponent's** setup move. After applying it, it is the **solver's** turn.
- The solver plays moves at **even indices** (0-based): `Moves[1]`, `Moves[3]`, ...
  Opponent plays the odd-index responses in between.
- Therefore the **solver's color is the opposite** of the side-to-move field in the FEN.
  Orient the displayed board from the solver's perspective.
- All solver moves are "only moves" (any other move clearly worsens the position), except
  mate-in-1 where any mating move wins.
- Theme reference (descriptions of every theme):
  https://github.com/ornicar/lila/blob/master/translation/source/puzzleTheme.xml

### Querying the data cheaply

826 MB of parquet — do not naively load it all into memory. Use predicate pushdown /
streaming. Recommended via `uv`:
```
uv tool run --from polars python3 -c "import polars as pl; print(pl.scan_parquet('data/*.parquet').filter(...).head().collect())"
```
Reading only parquet footers (for counts/schema) is instant with `pyarrow`.

## Kobo constraints (the whole point)

The Kobo "beta" web browser is an old WebKit/NetFront engine on e-ink hardware. Design
for the worst case:

- **Assume JavaScript is unreliable or off.** The site must fully work as plain HTML +
  links. No SPA, no client-side routing, no runtime chess libraries
  (chessboard.js / chess.js will not render). Any JS is progressive enhancement only.
- **Pre-render boards at build time** as images (PNG is safest; SVG support is spotty on
  older devices). Do not compute board state in the browser.
- **Reveal the solution via a separate page/link**, not a JS show/hide toggle.
- **E-ink:** grayscale (≈16 levels), slow full-page refresh, ghosting. Use high-contrast
  black-on-white, no animation, no hover states.
- **Small/old CSS support:** keep layout simple; do not rely on flexbox/grid/web fonts.
- **Keep pages tiny** (limited RAM): one puzzle per page, small embedded/linked images,
  large tap targets for navigation.

## Intended pipeline (plan, not yet built)

1. **Select** a pool subset from `data/*.parquet` per a config (e.g. rating range,
   themes, min popularity, count). Config-driven so the pool can change over time.
2. **Generate** static pages: one puzzle page + one solution page each, with a
   pre-rendered board image, plus index/navigation. Deterministic given config + dataset.
3. **Deploy** the static output to a host the Kobo can reach.

Open decisions to confirm before building (do not assume):
- Hosting / deploy target.
- Board image generation tool (board-image renderer choice).
- Pool selection criteria and how/when the pool rotates.
- Static generator vs. hand-rolled templates.

## Conventions

- Use `uv` / `uvx` (`uv tool run`) for all Python tooling and the data pipeline; no `pip`.
- Keep generated site output small and self-contained; never duplicate the 826 MB dataset.
- Disk is tight (host was ~97% full, ~27 GB free) — clean up intermediate artifacts.
