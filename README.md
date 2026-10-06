# Wiki Book Builder

Turns a chosen set of OrcaSlicer Wiki pages into a printed, hand-bound book: A5 pages imposed two-up on A4 (or A4 on A3), double-sided, in folded signatures you can sew.

## Setup (once)

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
npm ci
```

The build drives your Microsoft Edge through Playwright.

## Use

| Command | Result |
|---|---|
| `.\.venv\Scripts\python build.py --serve` | Opens a live preview as two-page spreads. Edit `book.toml`, `print.css` or any wiki page, then refresh. |
| `.\.venv\Scripts\python build.py` | Writes `out/book.pdf` (reading copy) and `out/book-imposed.pdf` (sheets to print), and prints the signature breakdown. |
| `.\.venv\Scripts\python build.py -c (Get-ChildItem volumes\*.toml)` | Builds the three-volume set of the whole wiki. Output files take the config's name, e.g. `out/2-print-settings-imposed.pdf`. Allow about 5 minutes per volume. |
| `.\.venv\Scripts\python build.py --proof 25` | Takes pages 25-32 of the built `book.pdf` and imposes them on 2 sheets (`out/book-proof.pdf`) for a test print. Add `-c` to proof another config. |
| `.\.venv\Scripts\python test_build.py` | Self-check for imposition order, anchors and video QR codes. |

## Choosing content

Edit `book.toml` (a calibration handbook) or a file in `volumes/` (the whole wiki in three volumes). Each `[[part]]` lists wiki page ids, which are the `.md` filenames without the extension (the same ids wiki links use). Reorder lines to reorder chapters. Comment a line out to drop a chapter. A part with a `title` gets its own divider page.

Layout settings live under `[layout]`: page size, sheets per signature, duplex flip edge, chapter breaks, font size, TOC depth, figure height cap and margins. The defaults are tuned for density: 8pt type, chapters that continue on the same page (the volumes use `chapter_start = "fit"`: a chapter continues on the same page when at least `chapter_min_space` of the page, a third by default, is left below its heading, and otherwise starts on the next page), and figures capped at about a quarter of the page. For a roomier book, try `font_size = "9.5pt"`, `chapter_start = "page"` and `image_max = 0.38`.

### Volumes

Configs in one folder that set `volume = N` form a set. A link from one volume to a page in another prints as "(Vol. N)" instead of a page number, and the cover shows the volume number. Move a page between volumes by moving its line from one file to the other.

## Printing and binding

1. Print `out/book-imposed.pdf` at actual size (100%, fit-to-page off), double-sided, with the flip edge set in `book.toml` (`short` by default).
2. Print a proof first (`--proof`). Hold the first sheet to the light: the first proof page should sit behind the second.
3. The sheets come out in signature order. Take each group of sheets (the build output lists how many per signature), fold the stack in half and crease it.
4. Stack the folded signatures in order and sew through each fold.

Four sheets per signature (16 pages) suits 80 gsm paper. With heavier paper, use three.

## How it works

`build.py` reads the wiki Markdown with the same extensions the website uses and converts GitHub alerts into callouts. Wiki links become in-book cross-references with page numbers, and links to pages outside the book point at orcaslicer.com. The script also drops the in-page link lists, which mean nothing on paper. YouTube links get a small QR code (a thumbnail link becomes a compact card with the QR beside it).

Before pagination, `layout.js` arranges the figures in the browser. Consecutive figures share a row, and a figure that would fill less than half the text width floats beside its text. Sized images under 64px stay inline as icons. Paged.js lays out the single HTML file in Edge, handling mirrored margins, running heads, a TOC with page numbers, and parts and chapters. Edge prints it to PDF, and pypdf imposes the pages onto sheets.

The tool reads the wiki clone and never writes to it.

## Release pipeline

`.github/workflows/release.yml` publishes the volumes as a GitHub Release, one edition per OrcaSlicer minor version (2.4, 2.5, ...). The wiki has no releases of its own, so the app's releases serve as the version signal.

- **When it runs:** Mondays at 05:17 UTC, plus a manual "Run workflow" button. A manual run with an app version (e.g. `v2.4.1`) builds that version straight away and skips the cadence rules.
- **Cadence** (`release_check.py`, covered by `test_build.py`): only clean `vX.Y.Z` tags count, so alphas, betas, rcs and nightlies are ignored. A minor gets its edition once it has settled: 21 days without a new patch, so the post-release fix burst has passed and the wiki has caught up. A long-lived minor gets a refresh when a new patch lands and its edition is 90 days old. OrcaSlicer has shipped a minor every 3 to 15 months since 2.0, so expect a release or two a year.
- **What a build does:** checks out the wiki's current `main`, builds all `volumes/*.toml` on a Windows runner (Edge is preinstalled) and creates the release `wiki-<app version>` with the PDFs attached. The release notes record the wiki commit and page counts.
- **Why poll:** GitHub can't trigger a workflow from releases in a repo you don't control.

The workflow needs this folder pushed as the root of a GitHub repo. Releases need no extra secrets; the built-in token has `contents: write`.
