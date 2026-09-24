# Command-line reference

The `hoplink` command downloads media from Reddit by driving a real browser. This page documents every option.
For the Python API see [lib.md](lib.md) and for the project overview see the [README](../README.md).

```bash
hoplink [SOURCES...] [OPTIONS]
```

`hoplink --help` prints the same options in condensed form, and `hoplink --version` prints the version.

## Contents

- [Sources](#sources)
- [Choosing what to extract](#choosing-what-to-extract)
- [External hosts](#external-hosts)
- [Output](#output)
- [Post filters](#post-filters)
- [Browser](#browser)
- [Pacing and retries](#pacing-and-retries)
- [Console output and logging](#console-output-and-logging)
- [Config files](#config-files)
- [Exit codes](#exit-codes)

## Sources

Sources are positional arguments: pass as many as you like. Each is written to its own subdirectory under`--out`.

| Form                            | Example                          | Becomes                                   |
|---------------------------------|----------------------------------|-------------------------------------------|
| bare name                       | `pics`                           | subreddit                                 |
| `r/name`, `/r/name/`            | `r/EarthPorn`                    | subreddit                                 |
| full URL                        | `https://www.reddit.com/r/pics/` | subreddit                                 |
| `a+b+c`                         | `EarthPorn+wallpapers`           | multireddit, browsed as one combined feed |
| `u/name`, `user/name`, user URL | `u/someuser`                     | that user's submitted posts               |

Subreddit names may contain `A-Z a-z 0-9 _`; usernames may also contain `-`. Anything else is rejected.

```bash
hoplink r/EarthPorn
hoplink EarthPorn pics wallpapers
hoplink "EarthPorn+wallpapers+skyporn"
hoplink u/someuser
```

Sources may be omitted on the command line when a `--config` file supplies them. Giving sources on the command
line replaces the file's list entirely.

### Listing options

| Option          | Default | Description                                                                                                                                                                  |
|-----------------|---------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `--limit N`     | `100`   | Maximum posts to harvest per source. Counted at the listing, *before* filters (with filters active you will usually keep far fewer).                                         |
| `--sort ORDER`  | `new`   | `new`, `hot`, `top`, `rising`, `controversial`. `rising` is subreddit- and multireddit-only; `controversial` is user-page-only. An unsupported combination is a usage error. |
| `--time WINDOW` | `all`   | Window for `--sort top` and `--sort controversial`: `hour`, `day`, `week`, `month`, `year`, `all`. Ignored by the other sorts.                                               |

```bash
hoplink r/EarthPorn --limit 50 --sort top --time month
```

## Choosing what to extract

| Option           | Default             | Description                                                    |
|------------------|---------------------|----------------------------------------------------------------|
| `--types LIST`   | `image,gallery`     | Which kinds of post to handle (comma-separated).               |
| `--formats LIST` | `jpg,jpeg,png,webp` | Image extensions to accept. Leading dots and case are ignored. |

`--types` accepts:

| Type        | Covers                                                                                                                                                 |
|-------------|--------------------------------------------------------------------------------------------------------------------------------------------------------|
| `image`     | single-image posts (`i.redd.it`)                                                                                                                       |
| `gallery`   | multi-image gallery posts                                                                                                                              |
| `video`     | `v.redd.it` videos and silent "gif" posts, muxed to MP4. RedGIFs clips (hosted or linked), resolved through the RedGIFs API to their audio-bearing MP4 |
| `text`      | self posts, saved as a Markdown document (YAML front matter plus the body)                                                                             |
| `link`      | external link posts pointing at a direct image                                                                                                         |
| `poll`      | poll posts, saved as a Markdown document of the post's listing metadata                                                                                |
| `crosspost` | posts re-sharing another post, resolved to the shared image, gallery, or video                                                                         |
| `audio`     | audio tracks from an external host                                                                                                                     |
| `document`  | documents (PDFs and the like) from an external host                                                                                                    |
| `all`       | everything above                                                                                                                                       |

`audio` and `document` exist for external hosts to serve. No built-in resolver produces them yet.

A post whose type is not listed is skipped before its page is ever opened, so narrowing `--types` saves time.
`--formats` does not apply to `--types video`, whose container Reddit decides. RedGIFs clips fall under `video`:
Reddit's rehosted copy is usually silent, so the original is fetched from RedGIFs keeping the audio.

```bash
hoplink r/pics --types image,gallery,video
hoplink r/wallpapers --formats png,webp
```

## External hosts

| Option                         | Default | Description                                                                                                             |
|--------------------------------|---------|-------------------------------------------------------------------------------------------------------------------------|
| `--scrape-all HOSTS`           | none    | Comma-separated hosts whose posts pull the uploader's entire profile rather than just the linked item (e.g. `redgifs`). |
| `--blacklist HOST:NAMES`       | none    | Uploaders never to download, per host (case-insensitive). Repeatable, once per host.                                    |
| `--host-option HOST:KEY=VALUE` | none    | A per-host setting, e.g. `--host-option imgur:client_id=abc123`. Repeatable.                                            |
| `--follow-links`               | off     | Also resolve the links found in the body of text posts, through the same per-host resolvers.                            |

Media on an external host is resolved by a per-host *resolver* matched on the URL's domain (subdomains included). The
same resolver serves that host wherever its URL appears: as a link post's target, behind a crosspost, or inside the 
body of a text post. The set of registered resolvers is what `--scrape-all`, `--blacklist`, `--host-option` can name.

### Scraping whole profiles

To avoid re-downloading a profile every time that uploader reappears, each profile is scraped **at most once per run**: 
the first post by an uploader triggers the full scrape, and later posts by the same uploader are skipped with a reason. 
The flag also implies the media types the named hosts serve (`video` for RedGIFs), so it works without adding 
`--types video`by hand. Across runs, the profile's own manifest skips clips already saved, so a repeat run fetches only
what is new.

A scraped profile is stored as a **collection**: its own subfolder of the source directory, named for the uploader, 
with its own `manifest.json` and files named `0001_someuploader.mp4`. The source's own manifest acts at post level 
(one record naming the profile, rather than one per clip).

```
downloads/gifs/
├── 0001_A_neat_loop.mp4
├── manifest.json          <- posts, plus one record per collection
└── someuploader/
    ├── 0001_someuploader.mp4
    ├── 0002_someuploader.mp4
    └── manifest.json      <- this profile's own files and numbering
```

```bash
hoplink r/gifs --scrape-all redgifs --limit 200
```

### Blacklisting uploaders

`--blacklist` names uploaders you never want, in either mode. Once a clip's metadata names one of them, the post
is skipped before any download, which also stops `--scrape-all` from paging that uploader's profile. Matching is
case-insensitive. Anonymous uploads (which carry no username) are not blocked.

```bash
hoplink r/gifs --scrape-all redgifs --blacklist redgifs:spammer,adbot --limit 200
```

### Links inside text posts

Some posts keep their media behind a link in the body of a text post rather than on the post card. `--follow-links`
reads those links and resolves the ones whose host is registered, so the post yields that media alongside its Markdown 
document. The resolver registry also acts as the allowlist: a link to an unregistered host is not fetched.

Following widens what a text post can answer for, so asking for only the linked host's media types still selects text
posts (but drops the text you did not ask for). The run below keeps the clips a text post linked, without the Markdown:

```bash
hoplink r/gifs --types video --follow-links --limit 100
```

Reading a text post's body always costs one page visit, with or without this flag. `--follow-links` adds one resolver
lookup, and its network requests, per recognized link.

## Output

| Option          | Default     | Description                                                                     |
|-----------------|-------------|---------------------------------------------------------------------------------|
| `--out DIR`     | `downloads` | Base output directory; each source gets a subdirectory.                         |
| `--dry-run`     | off         | Resolve and report media, download nothing.                                     |
| `--dedupe`      | off         | Skip saving a file whose SHA-256 matches one already stored in the same folder. |
| `--report PATH` | none        | Write a machine-readable run report.                                            |

Files are named `<index>_<slug>.<ext>` — `0001_Sunset_Over_Lofoten.jpg`. The index records arrival order. The slug
comes from the post's title, sanitized (Windows' reserved characters and device names removed, length capped at 120 
characters, and cut back to a word boundary). A post whose title slugs to nothing keeps the bare `0001.jpg`. A post that 
resolves to several files inserts a part number, so a gallery's slides are `0001_01_...`, `0001_02_...`.

Each source directory holds a `manifest.json` recording where every file came from. Re-running a command skips media
already recorded in the manifest and files already on disk, and continues the numbering rather than restarting at 1, so 
an interrupted run resumes cheaply. A collection (see `--scrape-all` above) resumes against its own manifest, inside 
its own subfolder.

`--dedupe` catches reposts and crossposts that carry a fresh URL. Only the write is skipped. It still costs the 
download, as the bytes must be fetched to be hashed. Hashes are recorded in the manifest as `sha256` and compared
within one folder, so a collection de-duplicates against its own contents, not against the rest of the source.

`--report` picks its format from the extension: `.csv` writes one summary row per source; any other extension writes a 
JSON document with per-source totals, per-item detail (filename, path, size, hash, originating post), timings, and 
run-wide totals. The report is written even when some sources failed.

```bash
hoplink r/pics --out ./downloads --dedupe --report run.json
hoplink r/pics --dry-run              # preview without downloading
```

## Post filters

`--types` picks which *kind* of post to take; these filters narrow that further. A post must satisfy **every** filter
given. Rejection happens between harvest and resolution, so a dropped post costs no page visit and no download.

| Option                 | Keeps                                                                        |
|------------------------|------------------------------------------------------------------------------|
| `--min-score N`        | posts scoring at least N                                                     |
| `--min-comments N`     | posts with at least N comments                                               |
| `--title-include TEXT` | posts whose title contains TEXT                                              |
| `--title-exclude TEXT` | posts whose title does *not* contain TEXT                                    |
| `--title-regex`        | treats both `--title-*` values as regular expressions rather than substrings |
| `--author NAMES`       | posts by these authors (comma-separated)                                     |
| `--block-author NAMES` | posts *not* by these authors (wins over `--author`)                          |
| `--flair TEXTS`        | posts carrying one of these link flairs                                      |
| `--min-gallery N`      | galleries holding at least N images                                          |
| `--after DATE`         | posts created at or after DATE (inclusive, ISO-8601)                         |
| `--before DATE`        | posts created before DATE (exclusive, ISO-8601)                              |
| `--skip-stickied`      | posts that are not stickied or pinned                                        |

Title and flair matching is case-insensitive. Flair matching is exact = a substring does not count. Author names
are written bare, without the `u/` prefix. `--after` must be strictly earlier than `--before`.

```bash
# original-content wallpapers from trusted posters, no mod posts
hoplink r/wallpapers --title-include "^\[OC\]" --title-regex \
    --author alice,bob --title-exclude meta --min-score 500

# substantial galleries only, from this year
hoplink r/pics --types gallery --min-gallery 5 --after 2026-01-01
```

Special behaviours:

- **`--min-gallery` is judged after the gallery is opened.** A listing card doesn't report how many images a gallery
  holds (Reddit virtualizes the carousel). The filter still saves downloads, but not page visits.
- **A post whose value Reddit did not render is dropped, not waved through.** If a post's score is hidden, and you
  asked for `--min-score 500`, it does not qualify. The reason is reported (`score unknown`), so `-v` or an `on_skip`
  callback will surface it.
- **A single `--flair` on a subreddit is filtered by Reddit. It becomes a `?f=flair_name:"..."` parameter on the 
  listing URL, so the harvester scrolls a listing that already holds only matching posts, and `--limit` surfaces that 
  many *matching* posts rather than that many posts scanned. The filter stays local when it cannot be pushed upstream: 
  Reddit's listing accepts exactly one flair (`--flair a,b` is filtered here), and combined listings (`r/a+b`) and user 
  profiles are served on markup that ignores the parameter. When Reddit does the filtering, a flair that matches nothing
  yields an empty run rather than a "no posts rendered" error.
- **`--after` stops a `--sort new` listing early.** A `new` listing runs newest-first, so once the harvester has seen
  10 consecutive posts older than DATE it stops scrolling instead of paging on to `--limit`. Pinned posts at the top of
  a listing, whatever their age, are too few to trigger it. With any other sort `--after` only filters.

## Browser

| Option                | Default                | Description                                                                 |
|-----------------------|------------------------|-----------------------------------------------------------------------------|
| `--show`              | off (headless)         | Show the browser window.                                                    |
| `--profile DIR`       | none                   | Persistent browser profile directory, letting a login survive between runs. |
| `--login`             | off                    | Sign in to Reddit and exit. Scrapes nothing.                                |
| `--user-agent STRING` | current desktop Chrome | User-Agent sent to Reddit.                                                  |

### Signing in

Logged-out browsing cannot see NSFW or some private content. `--login` is an errand of its own: it opens a visible
window on Reddit's login page, waits for you to finish signing in, stores the session in `--profile`, and exits. No
source is opened and nothing is downloaded, so it takes no sources and needs none.

```bash
hoplink --login --profile ./.reddit_profile         # sign in once
hoplink r/somensfwsub --profile ./.reddit_profile   # every later run reuses the session, headless
```

The window is always shown, whatever `--show` says: a sign-in nobody can see cannot be completed. `--profile` is
required, since an incognito session is thrown away the moment the browser closes.

Run it again on the same profile to check that session:

```
$ hoplink --login --profile ./.reddit_profile
Already signed in as u/someuser. Session stored in ./.reddit_profile
```

The exit code follows: `0` when the profile ends up signed in, `1` when it does not (the window was closed, or five
minutes passed without a sign-in), so a setup script can act on it.

The profile directory is created on first use. It holds a logged-in session, so keep it secret.

The default User-Agent impersonates current desktop Chrome, which is what Reddit serves its normal layout to. A
custom string risks the stripped-down or blocked page.

## Pacing and retries

Defaults are relaxed to avoid throttling:

| Option                     | Default | Description                                                                                                          |
|----------------------------|---------|----------------------------------------------------------------------------------------------------------------------|
| `--concurrency N`          | `1`     | Sources scraped in parallel, each on its own page of the shared browser. Memory and request rate both scale with it. |
| `--scroll-pause SECONDS`   | `2.0`   | Wait after each feed scroll for new cards to load.                                                                   |
| `--delay SECONDS`          | `0.5`   | Wait after each download before the same downloader takes the next. Also the floor on retry backoff.                 |
| `--download-concurrency N` | `1`     | Files fetched at once *within one post*. See below.                                                                  |
| `--no-direct-download`     | off     | Route media through the browser instead of fetching it directly. Much slower for large files; see below.             |
| `--max-stale-scrolls N`    | `10`    | Give up scrolling after this many consecutive scrolls that surface no new posts.                                     |
| `--max-retries N`          | `2`     | Extra attempts for a download that fails transiently. `0` disables retrying.                                         |

`--download-concurrency` parallelizes the files *of a single post* for example an entire RedGIFs profile pulled in by 
`--scrape-all redgifs`. Posts are still handled one after another, so a listing of single-image posts sees no speedup 
from it. Filenames, hash de-duplication, the manifest, and the event stream are unaffected: downloads run in parallel
but are recorded in candidate order, exactly as with the default of `1`.

Workers pull the next candidate as soon as they are free, so one slow file occupies only its own slot and does not
stall the others behind it. What is bounded is how far they may run ahead of the writes (twice the window) to keep
memory in check when a post resolves to hundreds of videos.

The two concurrency flags multiply. `--concurrency 3 --download-concurrency 4` puts up to twelve requests in flight.
Raising `--download-concurrency` is safest on jobs whose media lives elsewhere (RedGIFs, Imgur) rather than on 
`i.redd.it`. Memory scales with it too: up to `2 * N` response bodies are held at once.

### How media is fetched

Listings need a browser: Reddit serves its modern UI to real clients and blocks its API. Media bytes do not. Routing 
them through Playwright is expensive (every response is buffered inside the browser process and then copied back out 
through the driver, at a cost that grows with the **square** of the file's size).

Media is therefore fetched directly by default, carrying the browser's cookies for the host and the configured
User-Agent, so a logged-in or over-18 session still applies. A host that answers a direct request with a transport
error or a "not through this route" status (`401`, `403`, `405`, `406`, `451`) is retried through the browser and
remembered, so it costs one wasted attempt per host per run. A `404` is reported as-is -> it would be a `404` in
the browser too.

`--no-direct-download` forces everything back through the browser. Given the automatic fallback you should not need it.

Only transient failures are retried (transport errors, 408, 425, 429, and any 5xx). A 404 or an unexpected
`Content-Type` is final. Backoff is exponential and never drops below `--delay`; a config file can tune its base 
through `retry_backoff`.

### Rate limits

A `429` is not a flaky response: the host is naming our request rate as the problem, and a one-second retry does not
address it. So the host is held for a cooldown instead -- `rate_limit_backoff` (15s by default), doubling with each
consecutive refusal up to `rate_limit_max_backoff`, or whatever the host's `Retry-After` header asked for when that is
longer. A request that answers normally resets the doubling. A rate-limited request may be retried up to
`rate_limit_retries` times (or `--max-retries`, if that is more).

The cooldown is imposed on the host, not just on the request that was refused: every request to it waits, API calls
and downloads alike, whichever handler or job makes it. Refusals of requests that were already in flight when the
cooldown began count as the same one, so a burst of parallel downloads does not escalate it.

```
16:48:52 INFO  api.redgifs.com is rate-limiting us (HTTP 429); holding every request to it for 15.0s
```

## Console output and logging

Two independent channels: progress goes to **stdout**, the library's diagnostics go to **stderr**.

| Option              | Description                                                               |
|---------------------|---------------------------------------------------------------------------|
| `-q`, `--quiet`     | Print only the per-source summary, dropping per-file progress lines.      |
| `-v`, `--verbose`   | `-v` for info (retries, and the timing breakdown below), `-vv` for debug. |
| `--log-level LEVEL` | `debug`, `info`, `warning`, `error`, `critical`. Overrides `--verbose`.   |

```bash
hoplink r/pics -v                    # info: retries and the timing breakdown
hoplink r/pics -vv                   # debug: adds per-post and per-fetch detail
hoplink r/pics --log-level warning
```

### Why a run was slow

At `-v`, each job ends with a breakdown of where its wall clock went:

```
where the time went (wall 412.7s):
  harvest        11.2s    2.7%  x1                   scrolling the listing
  resolve        19.0s    4.6%  x100   avg   0.19s   resolving posts to media (RedGIFs profile paging lands here)
  fetch_wait     66.3s   16.1%  x214   avg   0.31s   fetching responses into the browser
  fetch_body    303.9s   73.6%  x214   avg   1.42s   copying out of Playwright into Python
  pace          107.0s   25.9%  x214   avg   0.50s   configured delay between downloads
  write           4.1s    1.0%  x214   avg   0.02s   writing files to storage
  manifest       12.4s    3.0%  x214   avg   0.06s   rewriting manifest.json
  transferred 2.3 GB at 8.1 MB/s
```

Read it as a set of suspects, each with its own remedy:

| Dominant phase | What it means                                                                                                                                                                                                   | What to try                                                                                                                                                  |
|----------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `fetch_wait`   | Time to first byte on the direct route. On the browser route this is the whole response, since Playwright buffers it all before resolving, so it scales with file size rather than latency.                     | Raise `--download-concurrency`; separate streams overlap well. Compare the reported MB/s against a speed test to see whether the link is actually saturated. |
| `fetch_body`   | The bytes arriving. On the direct route this is roughly the transfer itself. If it dwarfs `fetch_wait` on large files, that host fell back to the browser, whose body copy grows worse than linearly with size. | Check the log for "refused a direct request" to see which host, and why.                                                                                     |
| `pace`         | Self-inflicted.                                                                                                                                                                                                 | Lower `--delay`, or set it to `0` for non-Reddit media hosts.                                                                                                |
| `manifest`     | `manifest.json` is rewritten after every saved file, and it grows with the job.                                                                                                                                 | Raise `manifest_flush_every` in a config file.                                                                                                               |
| `hash`         | `--dedupe` hashing every downloaded body.                                                                                                                                                                       | Drop `--dedupe` if URL-level de-duplication is enough.                                                                                                       |
| `resolve`      | Handlers, not downloads — most often a RedGIFs profile being paged.                                                                                                                                             | Lower `api_pause` in a config file.                                                                                                                          |
| `harvest`      | Scrolling the listing.                                                                                                                                                                                          | Lower `--scroll-pause`, at the usual throttling risk.                                                                                                        |

Phases are measured as wall clock around an `await`, so they overlap rather than partitioning the run. With
`--download-concurrency` above 1 the `fetch_*` and `pace` phases overlap each other and their shares can add past 100%; 
the header says so when it applies. With `--concurrency` above 1, each job's phases additionally absorb time the event 
loop spent on the *other* jobs, which the header does not flag.

`-vv` adds a line per fetch with its size and the same wait/transfer split.
The breakdown is also in `--report` JSON under `timings`, and on `ExtractionResult.timings` for library callers.

## Config files

Any run can be stored in a TOML file and loaded with `--config` (or `-c`) instead of retyping a long command:

```toml
# earthporn.toml
sources = ["r/EarthPorn", "r/wallpapers"]
sort    = "top"
time    = "month"          # alias for time_filter
limit   = 200
types   = "image,gallery"
out     = "downloads"
```

```bash
hoplink --config earthporn.toml
```

Keys mirror the long-form options with the leading dashes removed. Dashes and underscores are interchangeable
(`scroll-pause` = `scroll_pause`), and `--time` may be written as `time` or `time_filter`. `sources` accepts a bare
string or a list. An unknown key is a hard error listing every accepted key, so typos surface immediately.

### Precedence

**Command-line argument > config file > built-in default.** A file gives you a saved baseline you can still
override per run:

```bash
# same job, but only 20 posts and download nothing this time
hoplink --config earthporn.toml --limit 20 --dry-run
```

The on/off switches `dry_run`, `show`, and `quiet` can be turned *on* by a config file, but the command line cannot
turn them back *off* (there are no `--no-*` flags). Comment them out in the file instead.

### Repeatable and per-host options

`--blacklist` and `--host-option` are repeatable flags, so in a file they take a list of the same `HOST:...`
strings the command line uses:

```toml
scrape_all  = "redgifs"
blacklist   = ["redgifs:spammer,adbot"]
host_option = ["imgur:client_id=abc123"]
```

The same settings are also reachable in structured form through `host_options`, which is a table per host. The two
forms are merged key by key, with `blacklist`/`host_option` layered on top of the `host_options` table, so setting one
option for a host never discards the others:

```toml
[host_options.redgifs]
blacklist = ["spammer", "adbot"]

[host_options.imgur]
client_id = "abc123"
```

### Settings with no CLI flag

A config file also reaches settings the command line does not expose:

| Key                      | Default           | Description                                                                                                                                                                         |
|--------------------------|-------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `host_options`           | none              | Per-host settings, as a table per host (see above).                                                                                                                                 |
| `viewport`               | `[1366, 900]`     | Browser window size, `[width, height]`.                                                                                                                                             |
| `locale`                 | `"en-US"`         | Browser locale (`Accept-Language`, date and number formats).                                                                                                                        |
| `scroll_px`              | `18000`           | Pixels scrolled per step. Lower it if posts seem to be skipped.                                                                                                                     |
| `api_pause`              | `0.5`             | Seconds between successive requests to one third-party host, e.g. walking a RedGIFs profile under `--scrape-all`. Unlike `--scroll-pause`, nothing is waiting for a page to render. |
| `retry_backoff`          | `1.0`             | Base seconds for exponential retry backoff.                                                                                                                                         |
| `rate_limit_backoff`     | `15.0`            | Seconds a host is left alone after it answers `429`, doubling per consecutive refusal. A longer `Retry-After` wins.                                                                 |
| `rate_limit_max_backoff` | `300.0`           | Ceiling for that wait, so a run cannot stall indefinitely.                                                                                                                          |
| `rate_limit_retries`     | `4`               | Extra attempts for a rate-limited request, when more than `--max-retries`.                                                                                                          |
| `manifest_flush_every`   | `1`               | Rewrite `manifest.json` after every N saved files.                                                                                                                                  |
| `nav_timeout_ms`         | `60000`           | Page-navigation timeout.                                                                                                                                                            |
| `post_wait_timeout_ms`   | `30000`           | How long to wait for the first post cards to render.                                                                                                                                |
| `gallery_wait_ms`        | `2000`            | Flat settle time after opening a gallery. `0` disables it.                                                                                                                          |
| `request_timeout_ms`     | `60000`           | Per-download request timeout.                                                                                                                                                       |
| `default_media_types`    | `"image,gallery"` | Selection used when no `types` is given (mostly for library use).                                                                                                                   |

### Dates in TOML

`after` and `before` accept both TOML spellings (quoted `"2026-01-01"` and bare `2026-01-01`) and a bare date is
read as midnight. A value carrying no timezone is read as UTC. Quoting is safer: it is the one form that behaves 
identically here, on the command line, and in the library.

### Examples

Example config files live in [`examples/`](../examples/), each heavily commented and shaped around a realistic job. 
The option tables on this page and `hoplink --help` are the authoritative list of accepted keys.

## Exit codes

| Code  | Meaning                                                                |
|-------|------------------------------------------------------------------------|
| `0`   | every source finished successfully                                     |
| `1`   | at least one source failed, or the report could not be written         |
| `2`   | bad usage — unknown option, invalid value, or a broken `--config` file |
| `130` | interrupted with Ctrl-C                                                |
