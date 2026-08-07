# wenku8 Toolkit

Tools for [wenku8](https://www.wenku8.cc/) (轻小说文库 / light-novel library): two browser userscripts, a Python script that bulk-downloads light-novel txt files, and a Python script that turns locally-downloaded txt files into EPUBs with embedded illustrations.

## Contents

- `js插件/` — two independent Greasyfork userscripts (different authors; their code is intentionally not shared)
  - `轻小说文库+-2.31.2.js` — "轻小说文库+" by PY-DNG. All-encompassing site enhancement: reading, downloading, bookshelf, recommendations, reviews, accounts, page personalization, and more.
  - `轻小说文库下载-2.2.6.js` — "轻小说文库下载" by HaoaW. Builds per-volume / full-book ePub documents, drag-and-drop illustration placement, and online reading for some novels.
- `download_novel.py` — **bulk wenku8 light-novel txt downloader**: simulated browser request headers, 8 threads + rate limiter (40 req/min) by default, auto-retry on failure (3 by default), auto-skip 404s, Cloudflare engine fallback.
- `config.ini` — auto-generated on first run of `download_novel.py`; tune threads, retries, etc.
- `build_epub.py` — **local txt → EPUB with illustrations** (the main tool here).
- `requirements.txt` — Python dependencies for `build_epub.py`.
- `2163.txt` — a sample downloaded txt (aid `2163`).
- `2163_images/` — image cache created by `build_epub.py` (reused on re-runs).
- `AGENTS.md` — developer/agent conventions for this repo.

## Installing the userscripts

Open the script page on Greasyfork and install with Tampermonkey / Violentmonkey:

- 轻小说文库+: <https://greasyfork.org/scripts/539514>
- 轻小说文库下载: <https://greasyfork.org/scripts/407369>

Both run on `wenku8.com` / `wenku8.net` / `wenku8.cc` pages. They are independent scripts from different authors — do not expect shared features or shared code between them.

## download_novel.py

Standalone script that bulk-downloads light-novel txt files from `dl2.wenku8.com`:

- **URL rule**: `https://dl2.wenku8.com/txtutf8/{id // 1000}/{id}.txt` — book 1 → `0/1.txt`, book 2676 → `2/2676.txt`.
- **Simulated browser request headers** (Chrome UA + Referer) to pass basic anti-crawl.
- **Multi-threaded**: 8 threads + global rate limiter (40 req/min by default); prints the machine's CPU core count at startup.
- **Auto-retry on failure**: 3 attempts by default (exponential backoff); books that exhaust retries go to the failure list.
- **Rate-limit protection**: on HTTP 429 it honors `Retry-After` and triggers a global cooldown to avoid a multi-thread retry storm; retries recover automatically.
- **Auto-skip 404s**: links that genuinely don't exist are skipped without retrying.
- **Cloudflare fallback**: on a detected CF challenge the engine auto-upgrades through `curl_cffi` → `requests` → `cloudscraper` (the `dl2` txt endpoint is verified to accept non-browser clients).
- **Resume**: already-downloaded files are skipped by default; `--overwrite` forces re-download.
- **Atomic writes**: files are written to a temp name then renamed, avoiding partial files.
- Output goes to `txt/` next to the script.

### config.ini

Auto-generated on first run (next to the script):

| Key | Default | Description |
|---|---|---|
| `threads` | `8` | Download threads |
| `engine` | `auto` | `auto` / `curl_cffi` / `requests` / `cloudscraper` |
| `output_dir` | `txt` | Output folder (relative to the script) |
| `timeout` | `120` | Per-request timeout (seconds) |
| `retries` | `3` | Retry count on download failure |
| `req_per_min` | `40` | Global rate limit: max requests per minute |
| `request_delay` | `0` | Extra delay between requests (seconds); raise when rate-limited |
| `cookie` | *(empty)* | Paste browser-obtained cookies (e.g. `cf_clearance`) if Cloudflare blocks you |

### Usage

```bash
python download_novel.py -d 1             # download a single book
python download_novel.py -d 1-1000        # download a range
python download_novel.py -d 5,68          # download specific books
python download_novel.py -d 1-10,20,30-35 # mixed: ranges + specific IDs
python download_novel.py -d 1-5000 -c my.ini --overwrite
```

Downloaded `txt/<aid>.txt` files can be handed straight to `build_epub.py` for batch EPUB generation:

```bash
python build_epub.py --txt I:\wenku8\txt
```

## build_epub.py

Converts a wenku8 txt you already downloaded locally into a **Sigil-compliant EPUB3**:

- Parses the txt (auto-detects encoding: UTF-8 → GB18030 → Big5) and splits it into volumes/chapters by matching the API's `卷名 章节名` header lines.
- Fetches the volume/chapter tree from the wenku8 app API (via the `wenku8-relay.mewx.org` relay, with the encrypted Android request format).
- Identifies illustration chapters (title heuristic `/插图|插畫|img|写真/`, or `--scan-all` to scan every chapter) and extracts `<!--image-->` image URLs from their content.
- Downloads all illustrations with **simulated browser request headers** to pass the site's anti-hotlink/Cloudflare protection.
- Embeds images at the illustration-chapter positions, with the **cover defaulting to the first image of the first volume's illustration chapter** (falling back to the API thumbnail when there are no illustrations).
- Applies typography via a bundled stylesheet: centered volume/chapter headings and 2-character first-line indents for paragraphs.
- Runs a built-in self-check (XML well-formedness, manifest/spine/nav references, anchors, image magic bytes) before finishing, and can optionally run the official **epubcheck** too.

### Install

```bash
pip install -r requirements.txt
```

### Usage

Single file:

```bash
python build_epub.py --txt I:\wenku8\2163.txt
```

Batch directory (processes every `<aid>.txt` in it):

```bash
python build_epub.py --txt I:\wenku8 --out epub_out
```

The output is named `<title> - <aid>.epub`. With the default `--cover auto`, the cover is the first image of the first volume's illustration chapter; if the first volume has none, the API thumbnail (`action=book&do=cover`) is used instead.

### Options

| Option | Default | Description |
|---|---|---|
| `--txt` | *(required)* | txt file or directory (directory = batch over every `\d+.txt`) |
| `--out` | current dir | EPUB output directory |
| `--aid` | from filename | Force the wenku8 book id (default parses `<digits>.txt`) |
| `--engine` | `auto` | HTTP engine: `auto` / `curl_cffi` / `requests` / `cloudscraper` (see anti-crawl below) |
| `--workers` | `3` | Parallel download concurrency |
| `--delay` | `0.0` | Extra delay between requests (seconds) |
| `--req-per-min` | `60` | Rate limit: max requests per minute |
| `--cache` | `<txt dir>/<aid>_images` | Image cache directory |
| `--cookie-jar` | – | Persist session cookies to a JSON file |
| `--no-resume` | off | Disable resume (reuses cached images by default) |
| `--scan-all` | off | Scan every chapter for images, not just illustration-named chapters |
| `--cover` | `auto` | `auto` = first volume's illustration-chapter first image (fallback API thumbnail); `api` = always API thumbnail |
| `--no-cover` | off | Do not set a cover |
| `--epubcheck` | – | Path to `epubcheck.jar`; run the official validator after building |

### Anti-crawl & politeness

- **Engine fallback chain**: `curl_cffi` (impersonates Chrome's TLS/HTTP2 fingerprint) → `requests` → `cloudscraper` (solves JS challenges). A detected Cloudflare challenge auto-upgrades to the next engine.
- **Encrypted API body** (`appver`/`request`/`timetoken`) with an Android user-agent, per the app API requirement.
- **Rate limiting**: token-bucket per-minute cap, retries with exponential backoff, honors `Retry-After`, keeps a single session's cookies.
- **Disk cache + resume**: image URLs are immutable, so re-runs fetch nothing new (verified: the second run of the sample made 0 duplicate requests).

### EPUB compliance

The generated EPUB3 passes **epubcheck 5.3.0 with 0 errors / 0 warnings** (validated against the sample). It includes `nav.xhtml` + `toc.ncx`, a proper `cover-image` manifest entry, a reachable cover page (no "Guide"/landmarks section), escaped text, and ASCII-safe internal paths.

To run the official check yourself, point `--epubcheck` at an `epubcheck.jar`. If direct GitHub downloads are blocked in your network, fetch it from npm instead:

```bash
npm pack epubcheck-static   # extracts to epubcheck-static-*.tgz; jar is under package/vendor/epubcheck.jar
java -jar <path/to/epubcheck.jar> <book>.epub
```

### Notes

- The book id (`aid`) must appear in the txt filename (e.g. `2163.txt`), otherwise pass `--aid`.
- Illustration chapters are usually empty placeholder chapters at the end of each volume — that is where the images are embedded.
- Under Cloudflare rate limiting, keep the default `--workers`/`--req-per-min` (or raise `--delay`); don't hammer the endpoints.

## GUI (wenku8_gui.py)

A Dear PyGui desktop GUI that wraps both `download_novel.py` and `build_epub.py` in one window:

```bash
pip install dearpygui
python wenku8_gui.py
```

- Two tabs: **Downloader** (range spec, threads/engine/rate limit/timeout, output dir, cookie, `--overwrite`) and **EPUB Builder** (txt file/folder, out dir, workers, rate limit, cover, epubcheck, resume/scan flags).
- English/中文 switchable UI (default English) and dark/light themes, both in the `View` menu. Fonts auto-load from system CJK fonts.
- Real-time progress bars (per-book + per-image) and a color-coded live log. Settings are remembered in `gui_settings.json` next to the script.
- The scripts run **inside** the GUI (no subprocess); only one task runs at a time.

Build a standalone exe (Python tools only need PyInstaller):

```bash
pyinstaller --noconfirm --clean --onefile --windowed --name wenku8-gui `
  --collect-all curl_cffi --collect-all cloudscraper --collect-all ebooklib `
  --distpath release --workpath build --specpath build wenku8_gui.py
```

The result is a self-contained `release/wenku8-gui.exe`; the two scripts and all deps are embedded. Re-run PyInstaller after editing `build_epub.py`/`download_novel.py` to pick up changes.

## License

The userscripts carry their own licenses (see their `// @license` headers: 轻小说文库+ is GPL-3.0-or-later). This README and `build_epub.py` are provided as-is for personal use.
