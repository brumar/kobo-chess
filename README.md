# kobo-chess

A tiny, **static** chess-puzzle site designed for the **Kobo e-reader's built-in browser**
(an old WebKit engine on slow e-ink hardware). Boards are pre-rendered to grayscale PNGs at
build time; every page is plain HTML with **no JavaScript and no cookies**. Navigation,
"show solution", and "seen" marking all work as ordinary links.

**Live site:** https://brumar.github.io/kobo-chess/

Puzzles are grouped into Elo bands (500–2000, in steps of 100, ~100 puzzles each). You pick the
band nearest your rating, solve, and reveal the solution on a separate page. The pool is
**rotated automatically every month** by a GitHub Actions workflow.

## Credit

Puzzles come from the **[Lichess](https://lichess.org) open puzzle database**
([Lichess/chess-puzzles on Hugging Face](https://huggingface.co/datasets/Lichess/chess-puzzles)),
released under **CC0 1.0 (public domain)**. Each solution page links back to the original puzzle
at `lichess.org/training/<id>`. Huge thanks to Lichess and its community for making this data free.
The dataset card is preserved in [DATASET.md](DATASET.md).

This project is an independent, non-commercial hobby project and is **not affiliated with or
endorsed by Lichess**.

## How it works

- `build.py` — single-file pipeline (`uv run build.py`): selects a deterministic per-band pool
  from the dataset, renders each puzzle + solution board to a grayscale PNG
  (python-chess → resvg → Pillow), and generates static HTML.
- `config.toml` — all knobs (bands, quality gates, seed, board size).
- `templates/` — Kobo-safe HTML with inline CSS (no flexbox/grid/web fonts).
- `.github/workflows/rollout.yml` — monthly rebuild + GitHub Pages deploy.
- See **[BUILD.md](BUILD.md)** for build / rotate / deploy / verify details.

The 826 MB dataset is **not** committed (it is `.gitignore`d); the CI workflow downloads it from
Hugging Face on each run. To build locally, first fetch it:

```
uv tool run --from "huggingface_hub[cli,hf_xet]" hf download \
  Lichess/chess-puzzles --repo-type dataset --local-dir . --include "data/*.parquet"
uv run build.py
```

## License

- **Puzzle data:** CC0 1.0 (Lichess).
- **This code:** MIT (see [LICENSE](LICENSE)).
