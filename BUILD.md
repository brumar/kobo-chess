# Building & deploying the Kobo puzzle site

## What this is
A fully static puzzle site for the Kobo browser. Boards are pre-rendered to grayscale
PNGs at build time; pages are plain HTML with no JavaScript and no cookies. Navigation,
"show solution", and "seen" marking all work as ordinary links.

## Build
```
uv run build.py              # full build -> ./site/  (reads config.toml)
uv run build.py --limit 5    # quick test: 5 puzzles per band
```
Deterministic: same `config.toml` + same dataset => byte-identical `site/`.
Takes ~30s and produces ~1500 puzzles / ~46 MB.

## Layout produced
```
site/index.html                         # home: pick a rating band
site/p/<pool_id>/b0500/index.html       # band 500-600: grid of 100 puzzle links
site/p/<pool_id>/b0500/p001.html        # puzzle page (board + "show solution")
site/p/<pool_id>/b0500/s001.html        # solution page (key move + SAN line + rating)
site/p/<pool_id>/b0500/img/*.png        # board images
site/p/<pool_id>/manifest.json          # config echo + chosen puzzle ids per band
```

## Rotate the pool (manual)
Edit `config.toml`: bump **both** `pool_id` and `seed`, then `uv run build.py`.
New puzzles land under a new `site/p/<pool_id>/` path, so the URLs are fresh. That
resets the browser-history "seen" greying naturally and dodges Kobo cache staleness.
Old pools stay until you delete their folder.

## Tuning knobs (config.toml)
- `rating_min/max`, `band_width`, `per_band` — band layout and size.
- `popularity_min`, `nbplays_min`, `rating_dev_max` — quality gates (auto-relax per band
  if a band can't fill; the 500-600 band currently relaxes one tier).
- `max_solver_moves` — caps solution length.
- `board_px` — board image size; tune on the actual device.

## Deploy to GitHub Pages (HTTPS confirmed working on the target Kobo)
```
cd site
git init -b gh-pages
touch .nojekyll                 # stop Jekyll from touching the files
git add -A && git commit -m "pool 2026-06a"
git remote add origin <your-repo-url>
git push -f origin gh-pages
```
Then enable Pages on the `gh-pages` branch. Entry URL: `https://<user>.github.io/<repo>/`.
(`site/` is gitignored in the main repo; deploy it as its own Pages branch/repo.)

## Verify reproducibility before shipping
```
uv run build.py --out /tmp/site_a
uv run build.py --out /tmp/site_b
diff -r /tmp/site_a /tmp/site_b      # expect no output
```

## Known item to check on the real Kobo
The "opened puzzles turn grey" feature uses CSS `:visited` (browser history as memory).
Modern desktop Chrome suppresses `:visited` for privacy, so it cannot be previewed there,
but the old Kobo engine should honor it. If a given Kobo wipes history or ignores
`:visited`, the "Puzzle N of 100" counter + the device bookmark are the fallback.
