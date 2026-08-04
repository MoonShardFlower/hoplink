# hoplink

[![CI/CD](https://github.com/MoonShardFlower/hoplink/actions/workflows/ci-cd.yml/badge.svg)](https://github.com/MoonShardFlower/hoplink/actions/workflows/ci-cd.yml)
[![codecov](https://codecov.io/gh/MoonShardFlower/hoplink/branch/main/graph/badge.svg)](https://codecov.io/gh/MoonShardFlower/hoplink)
[![Docs](https://readthedocs.org/projects/hoplink/badge/?version=latest)](https://hoplink.readthedocs.io/en/latest/)
[![Python](https://img.shields.io/badge/python-3.12%20|%203.13%20|%203.14-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Composable, browser-driven Reddit media extraction that hops outbound links to their real host — a Python library
with a command-line front end.

Reddit blocks plain HTTP access to its JSON and OAuth endpoints from many networks. `hoplink` renders listings
in a real browser (Playwright and Chromium), reads post data out of the rendered page, and downloads the media those 
posts reference.

## Contents

- [Why a browser](#why-a-browser)
- [How a run works](#how-a-run-works)
- [Post types and media](#post-types-and-media)
- [External hosts](#external-hosts)
- [Sources and filters](#sources-and-filters)
- [Output layout](#output-layout)
- [Resuming and de-duplication](#resuming-and-de-duplication)
- [Pacing and retries](#pacing-and-retries)
- [Library API](#library-api)
- [Progress reporting and logging](#progress-reporting-and-logging)
- [Install](#install)
- [Quick start](#quick-start)
- [Documentation](#documentation)
- [NSFW and private sources](#nsfw-and-private-sources)
- [License](#license)

## Why a browser

The two conventional ways to read Reddit programmatically are its JSON API and OAuth (through a client such as
PRAW). The JSON API is blocked for large parts of the IP space, and OAuth requires an app registration Reddit is
unlikely to approve for a scraper. Driving a browser is slower than either, but it offers two things they do not:

- **No account required.** Only NSFW and private sources need a login, supplied through a persistent browser profile.
- **Media hosted off Reddit.** A per-host resolver can follow a link out to its real source (e.g. RedGIFs) and even 
  scrape the linked uploader's entire profile if requested.

Disadvantages:

- **Speed.** Chromium has to launch and every listing has to render. The default pacing is slow to avoid throttling.
- **Coupling to Reddit's markup.** Post data is read from rendered HTML, which Reddit might change without notice.
- **Space.** `playwright install chromium` downloads several hundred megabytes.

## How a run works

1. **Render.** Chromium opens the listing URL, executes its JavaScript, and clicks through cookie and over-18
   interstitials.
2. **Harvest.** Each rendered post card is read for its id, permalink, title, author, subreddit, score, comment count,
   flair, creation time, stickied status, and any media hints it carries. Both of Reddit's layouts are supported:
   the modern one (`shreddit-post`) infinite-scrolls, the legacy one (served for multireddits, among others) paginates 
   through its "next" link.
3. **Filter.** `PostFilter` predicates are applied here, between harvest and resolution. A post rejected at this
   point costs no page visit and no download.
4. **Resolve.** A handler turns each surviving post into media URLs. An image card already carries its original;
   a gallery needs its post page opened and its carousel parsed; a video needs its packaged-media JSON read.
5. **Download.** Media bytes are fetched directly, carrying the cookies and User-Agent of the browser session. A host
   that refuses a direct request is retried through the browser and remembered for the rest of the run.

## Post types and media

| Post type     | Result                                                                                                                                                                                                                                                                                                     |
|---------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **Image**     | The full-resolution `i.redd.it` original the card points at.                                                                                                                                                                                                                                               |
| **Gallery**   | Every slide. A listing card renders only the first few slides regardless of the gallery's size, so the post page is opened and the carousel is read.                                                                                                                                                       |
| **Video**     | A pre-muxed MP4 (video and audio) at the highest offered resolution. A post with no packaged rendition falls back to its DASH manifest, from which the tallest stream is taken (DASH keeps audio in a separate file, so those downloads are silent). Reddit's soundless "gif" videos are handled here too. |
| **RedGIFs**   | Clips hosted on or linked to RedGIFs, resolved through the RedGIFs API to the muxed MP4, which keeps the audio Reddit's rehosted copy usually drops. Selected under the `video` media type.                                                                                                                |
| **Link**      | External link posts, kept only when the target is itself a direct image file with an accepted extension.                                                                                                                                                                                                   |
| **Text**      | Self posts, saved as a Markdown document: a YAML front-matter block (title, author, date, subreddit, permalink, score, comments, flair, id, type) followed by the post's body, read from the post page.                                                                                                    |
| **Poll**      | Poll posts, saved as a Markdown document of the listing metadata alone (the question is the title). No page visit.                                                                                                                                                                                         |
| **Crosspost** | Posts re-sharing another post. The crosspost's own page renders the shared media, which is read and rebuilt as an image, gallery, or video.                                                                                                                                                                |

Any combination is selectable with `--types` on the command line or `media_types` in the library (default: images and
galleries). `--formats` narrows which image extensions are accepted (default `jpg,jpeg,png,webp`) and does not apply to 
video, whose container Reddit decides.

Two further media types, `audio` and `document`, exist for external hosts to produce. No built-in resolver emits
them yet.

## External hosts

Media on an external host is resolved by a `LinkResolver` matched on the URL's domain, not on the post type. The same
resolver serves that host wherever its URL appears: as a link post's target, behind a crosspost, or (with 
`--follow-links`) inside the body of a self post. The registry doubles as the allowlist for which link may be followed.

With `--scrape-all redgifs`, a RedGIFs post is treated as a pointer to its uploader: the uploader's entire RedGIFs
profile is paged in and downloaded. Each profile is scraped at most once per run, and `--blacklist redgifs:NAMES`
names uploaders to skip entirely, before any download.

## Sources and filters

Subreddits, user profiles, and multireddits (`a+b+c`, browsed as one combined feed) are all supported, each
sortable by `new`, `hot`, `top`, `rising`, or `controversial`, with a time window for the sorts that accept one.

A run can be narrowed by score, comment count, title (substring or regular expression), author allow and deny lists,
link flair, gallery size, creation-date window, and stickied status. Every rejection carries a reason
(`score 412 below min 500`), reported through `--verbose` or the `on_skip` callback. A post whose value Reddit did not 
render is rejected (e.g., a hidden score does not satisfy `--min-score 500`).

A single flair filter on a subreddit is pushed upstream into the listing URL, so Reddit does the filtering and
`--limit` counts matching posts rather than posts scanned.

## Output layout

Files land in a per-source directory under `--out`, named `<index>_<slug>.<ext>` for example
`0001_Sunset_Over_Lofoten.jpg`. The index records arrival order and the slug is derived from the post's title and 
sanitized (Windows' reserved characters and device names removed, length capped at 60 characters cut back to a word 
boundary). A post whose title slugs to nothing keeps the bare `0001.jpg`. A post that resolves to several files inserts 
a part number: `0001_01_...`, `0001_02_...`.

Alongside the files sits a `manifest.json` mapping each one back to its post: id, permalink, title, author,
creation time, media type, and the media URL it came from.

A handler that answers a single post with a whole *collection* of files (`--scrape-all redgifs`) stores that collection 
in its own subfolder instead, named for the collection, with its own `manifest.json`, its own numbering, and files 
named after the collection (`0001_someuploader.mp4`). The source's manifest records the collection once, at post level, 
rather than carrying one record per file. The mechanism is generic: any handler that sets `collection` on its 
candidates gets the same layout.

```
downloads/gifs/
├── 0001_A_neat_loop.mp4
├── manifest.json          <- posts, plus one record per collection
└── someuploader/
    ├── 0001_someuploader.mp4
    ├── 0002_someuploader.mp4
    └── manifest.json      <- this profile's own files and numbering
```

## Resuming and de-duplication

The manifests make runs resumable. A second run skips media already recorded in the manifest and files already present 
on disk, and continues the numbering instead of restarting at 1. Even with a lost manifest, numbering resumes past the 
highest-numbered file in the folder, so nothing is overwritten. Each collection resumes against its own manifest, so a 
profile that has grown since the last run costs only the new files. Writes are atomic (temporary file plus rename), so 
an interrupted download does not leave a half-written file behind.

Two further output modes:

- `--dedupe` hashes each downloaded body and skips writing one whose SHA-256 matches a file already stored in the
  same folder, which catches reposts and crossposts carrying a fresh URL. Only the write is skipped, the bytes still 
  have to be fetched to be hashed.
- `--report run.json` (or `.csv`) writes a machine-readable summary: per-source counts, per-file detail, and
  run-wide totals. `--dry-run` resolves everything and downloads nothing.

## Pacing and retries

Defaults are relaxed to avoid throttling. Scroll pauses, inter-download delays, scroll distance, per-host API pacing, 
and four separate timeouts are all tunable.

Downloads are retried with exponential backoff on transient failures (= transport errors, 408, 425, 429, and any 5xx). 
A 404 or an unexpected `Content-Type` is treated as final and fails on the first attempt.

## Library API

`AsyncHoplinkExtractor` is the async-native engine and holds all behavior. `HoplinkExtractor` is a synchronous
facade over it that runs the engine on a private event-loop thread. Both take the same constructor arguments and expose
the same methods. The CLI is a thin wrapper over the same API.

The extractor is extensible in three places:

- Register a `MediaHandler` to teach it a new kind of post.
- Register a `LinkResolver` to teach it a new external host.
- Swap the `StorageBackend` to write somewhere other than the local filesystem (`MemoryStorage` ships for tests).

Playwright is imported lazily, so models, filters, handlers, and storage can be imported and tested without a browser
installed.

## Progress reporting and logging

The library never prints. `Events` callbacks are the reporting channel (job start, each scroll, each resolved or
saved file, each skip with its reason, job end) and can be wired to `print`, tqdm, a queue, or a webhook.
Diagnostics go through the standard `logging` module and are off by default. The CLI wires up both: progress to
stdout, diagnostics to stderr.

## Install

```bash
pip install -e .
python -m playwright install chromium
```

## Quick start

```bash
# Top 50 images and galleries from r/EarthPorn this month
hoplink r/EarthPorn --limit 50 --sort top --time month

# Well-received, recent, non-pinned posts only, skipping reposts by content
hoplink r/pics --min-score 1000 --after 2026-01-01 --skip-stickied --dedupe

# Store a whole run in a TOML file and replay it
hoplink --config myjob.toml
```

```python
from hoplink import HoplinkExtractor, Subreddit, MediaType

with HoplinkExtractor(headless=True) as hle:
    result = hle.extract(
        Subreddit("EarthPorn", limit=50, sort="top", time_filter="month"),
        media_types=MediaType.IMAGE | MediaType.GALLERY,
        output_dir="./downloads",
    )
    print(result.summary())
```

## Documentation

| Where                                                           | What                                                                              |
|-----------------------------------------------------------------|-----------------------------------------------------------------------------------|
| [docs/cli.md](docs/cli.md)                                      | Every command-line option, the post filters, and the TOML config-file format      |
| [docs/lib.md](docs/lib.md)                                      | The Python API: both facades, configuration, events, storage, handlers, resolvers |
| [examples/](examples/)                                          | Ready-to-run config files                                                         |
| [readthedocs](https://hoplink.readthedocs.io/en/latest/) | Generated API reference                                                           |

`hoplink --help` covers the common cases; [docs/cli.md](docs/cli.md) fills in the rest.

## NSFW and private sources

Logged-out browsing cannot see NSFW or some private content. Point a persistent browser profile at the run and sign in 
once. the session is reused afterwards. See [docs/cli.md](docs/cli.md#browser) or [docs/lib.md](docs/lib.md#nsfw-and-private-sources).

## License

MIT. See [LICENSE](LICENSE).
