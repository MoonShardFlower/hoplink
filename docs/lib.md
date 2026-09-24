# Library guide

`hoplink` is a library first with the CLI being a thin wrapper over it. This page covers its usage.
For the command line see [cli.md](cli.md) and for the project overview see the [README](../README.md).

The full API reference is at:
[hoplink.readthedocs.io](https://hoplink.readthedocs.io/en/latest/).

## Contents

- [Two APIs](#two-apis)
- [Sources](#sources)
- [Media types](#media-types)
- [Configuration](#configuration)
- [Filtering posts](#filtering-posts)
- [Results](#results)
- [Progress events](#progress-events)
- [Batches](#batches)
- [Storage backends](#storage-backends)
- [File names, manifests, and collections](#file-names-manifests-and-collections)
- [Custom handlers](#custom-handlers)
- [Link resolvers](#link-resolvers)
- [NSFW and private sources](#nsfw-and-private-sources)
- [Errors](#errors)
- [Logging](#logging)

## Two APIs

`AsyncHoplinkExtractor` is the async-native engine and holds all behavior. `HoplinkExtractor` is a synchronous facade
that runs the engine on a private event-loop thread. Both take the same constructor arguments and expose the same 
methods. Pick whichever suits your program.

### Synchronous

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

### Asynchronous

```python
import asyncio
from hoplink import AsyncHoplinkExtractor

async def main():
    async with AsyncHoplinkExtractor(headless=True) as hle:
        result = await hle.extract("r/EarthPorn", media_types="image,gallery")
        print(result.summary())

asyncio.run(main())
```

The context manager owns the browser. `extract` may be called any number of times inside it, and the browser is
launched once and reused. Calls made outside a `with` block start the browser automatically, in that case you will need
call `close()` yourself though.

Constructor arguments, for both facades:

| Argument      | Purpose                                                                             |
|---------------|-------------------------------------------------------------------------------------|
| `config`      | An `ExtractorConfig`. Individual fields may instead be passed as keyword arguments. |
| `storage`     | A `StorageBackend`. Defaults to `FilesystemStorage(config.output_dir)`.             |
| `handlers`    | Replaces the built-in handler chain entirely.                                       |
| `events`      | Default progress callbacks, merged with any passed per call.                        |
| `post_filter` | Default post predicates, overridable per call.                                      |
| `browser`     | A custom `BrowserManager` (mainly for tests).                                       |

Playwright is imported lazily, so the models, filters, handlers, and storage layers can be imported and tested without 
a browser installed.

## Sources

A source is *what* to scrape.

| Class         | Constructed from               | String form                                |
|---------------|--------------------------------|--------------------------------------------|
| `Subreddit`   | `Subreddit("EarthPorn")`       | `"r/EarthPorn"`, `"EarthPorn"`, a full URL |
| `UserProfile` | `UserProfile("someuser")`      | `"u/someuser"`, `"user/someuser"`          |
| `MultiReddit` | `MultiReddit(("pics", "art"))` | `"pics+art"`                               |

Every method that takes a source accepts either an object or a string. strings are parsed by `parse_source`.
Objects let you set per-source listing options:

```python
from hoplink import MultiReddit, Subreddit, UserProfile, parse_source

Subreddit("EarthPorn", sort="top", time_filter="month", limit=200)
UserProfile("someuser", sort="controversial", limit=50)
MultiReddit(("pics", "art"), sort="hot")

parse_source("r/pics", sort="top", limit=25)   # same thing from a string
```

`sort` is one of `new`, `hot`, `top`, `rising` (subreddits and multireddits) or `new`, `hot`, `top`, `controversial` 
(user profiles). An unsupported combination raises `ValueError`, as does a `limit` below 1 or a name containing illegal 
characters. `time_filter` (`hour` ... `all`) applies to `top` and `controversial`.

Each source exposes `.url` and `.key`. The latter is the filesystem-safe name used as its output subdirectory
(`"EarthPorn"`, `"pics+art"`, `"u_someuser"`).

## Media types

`MediaType` is a combinable flag choosing *which* payloads to keep.

```python
from hoplink import MediaType

MediaType.IMAGE | MediaType.GALLERY     # the default
MediaType.ALL                           # everything
MediaType.coerce("image,gallery")       # from a comma-separated string
MediaType.coerce(["image", "video"])    # from a list
```

Members are `IMAGE`, `GALLERY`, `VIDEO`, `TEXT`, `LINK`, `POLL`, `CROSSPOST`, `AUDIO`, `DOCUMENT`, and `ALL`. Anywhere
a `media_types` argument is accepted, a string or a list works too. It goes through `coerce` for you, and an unknown 
name raises `ValueError`.

`TEXT` and `POLL` posts are saved as Markdown documents (a YAML front-matter block, plus the body for self posts).
`CROSSPOST` posts are followed to the media they re-share and resolved like a normal image, gallery, or video.
`AUDIO` and `DOCUMENT` exist for external hosts (e.g. Soundgasm, GoogleDrive) to produce.

## Configuration

`ExtractorConfig` is a frozen dataclass holding everything about how the extractor drives the browser and
downloads. Pass one, or override individual fields as keyword arguments:

```python
from hoplink import ExtractorConfig, HoplinkExtractor

config = ExtractorConfig(
    formats=("jpg", "png"),
    headless=False,
    output_dir="./downloads",
    scroll_pause=3.0,
    max_retries=5,
    dedupe_by_hash=True,
    viewport=(1920, 1080),
)

with HoplinkExtractor(config) as hle:
    ...

# equivalent for a couple of fields, without building a config object
with HoplinkExtractor(headless=False, output_dir="./downloads") as hle:
    ...
```

| Group           | Fields                                                                                                            |
|-----------------|-------------------------------------------------------------------------------------------------------------------|
| media selection | `formats`, `default_media_types`                                                                                  |
| external hosts  | `scrape_all_hosts`, `host_options`, `follow_text_links`                                                           |
| browser         | `headless`, `profile_dir`, `user_agent`, `viewport`, `locale`                                                     |
| output          | `output_dir`, `manifest_flush_every`, `dedupe_by_hash`                                                            |
| pacing          | `scroll_pause`, `delay`, `api_pause`, `max_stale_scrolls`, `scroll_px`, `download_concurrency`, `direct_download` |
| retries         | `max_retries`, `retry_backoff`                                                                                    |
| rate limits     | `rate_limit_backoff`, `rate_limit_max_backoff`, `rate_limit_retries`                                              |
| timeouts        | `nav_timeout_ms`, `post_wait_timeout_ms`, `gallery_wait_ms`, `request_timeout_ms`                                 |

Values are normalized and validated on construction: `formats` and `scrape_all_hosts` accept a comma-separated string
or an iterable and come back as lower-cased tuples, `host_options` is frozen into a read-only mapping with lower-cased
host names. A negative delay or a zero timeout raises `ValueError`.

Being frozen, a config is safe to share. Derive variants with `replace`:

```python
polite = config.replace(scroll_pause=5.0, delay=2.0)
```

Every field is also settable from a TOML config file. See [cli.md](cli.md#config-files).

## Filtering posts

`PostFilter` holds optional predicates applied to each harvested post *before* its media is resolved, so a rejected
post costs no page visit and no download. A post is kept only when it satisfies every predicate that is set.

```python
from datetime import datetime, timezone
from hoplink import PostFilter, HoplinkExtractor

keepers = PostFilter(
    min_score=1000,
    min_comments=20,
    title_exclude=r"\[?(meta|mod ?post)\]?",
    title_regex=True,
    authors="alice,bob",
    block_authors=["spammer"],
    flairs="OC,Original Content",
    min_gallery=5,
    after=datetime(2026, 1, 1, tzinfo=timezone.utc),
    before="2026-07-01",
    skip_stickied=True,
)

with HoplinkExtractor(post_filter=keepers) as hle:      # default for every call
    result = hle.extract("r/EarthPorn")
    strict = hle.extract("r/pics", post_filter=PostFilter(min_score=5000))  # per call

print(result.posts_filtered, "posts rejected")
```

Name lists accept a comma-separated string or any iterable, and are matched case-insensitively. Dates accept a `date`,
a `datetime`, or an ISO-8601 string; naive values are read as UTC, and `after` must be strictly earlier than `before`. 
Title patterns are compiled at construction, so an invalid regex fails immediately. `block_authors` wins over `authors`.

Useful members:

- `filter.active`: whether any predicate is actually set.
- `filter.accepts(post)`: whether a post passes the listing-time predicates.
- `filter.rejection(post)`: the reason it failed, or `None`. This is the string reported through `Events.on_skip`.
- `filter.rejection_after_resolve(post, media_count)`: the post-resolution check, currently `min_gallery` only.
- `filter.server_side_flair`: the one flair Reddit can filter for, or `None`.

Two behaviors are worth knowing: `min_gallery` is judged after the post page has been read (the listing does not reveal
a gallery's true size), and a post whose value Reddit did not render is *rejected*. (e.g., a hidden score does not 
satisfy `min_score=500`)

### Flair filtering happens upstream where it can

A `PostFilter` holding exactly **one** flair is handed to Reddit: `Source.listing_url(flair=...)` folds it into the 
listing URL as `?f=flair_name:"..."`, so the harvester scrolls a listing that already contains only matching posts, 
and `limit` counts matching posts instead of posts scanned.

```python
result = hle.extract(
    Subreddit("EarthPorn", limit=200),           # 200 *Discussion* posts,
    post_filter=PostFilter(flairs="Discussion"), # not 200 posts of which some are
)
```

Only `Subreddit` overrides `listing_url`. `MultiReddit` and `UserProfile` return their plain URL, because Reddit serves
those listings on markup that drops the parameter and returns everything. More than one flair also stays local: 
Reddit only accepts a single flair. When Reddit does the filtering, an empty listing is an empty result, not a
`NoPostsFoundError`.

## Results

Every extraction returns an `ExtractionResult`:

```python
result = hle.extract("r/EarthPorn")

result.ok                  # False if the job errored
result.error               # the message, when a batch records instead of raising
result.summary()           # one-line human summary
result.duration            # wall-clock seconds, or None if unfinished

result.posts_scanned       # posts harvested from the listing
result.posts_matched       # posts a handler produced media or metadata for
result.posts_filtered      # posts a handler wanted but a PostFilter rejected
result.media_found         # candidates resolved, including known and existing
result.media_saved         # files actually written this run
result.skipped_existing    # already on disk
result.skipped_known       # identity already in the manifest
result.skipped_duplicate   # content hash matched an already-saved file
result.failures            # [(url, reason), ...]

result.items               # MediaItem per saved (or, in a dry run, planned) file
result.posts               # the matched Post objects
result.output_dir
result.manifest_path
result.timings             # where the job's wall clock went
result.to_report_dict()    # JSON-serializable, as written by --report
```

Each `MediaItem` carries the media URL and its provenance: `filename`, `path`, `size`, `sha256` (when de-duplication 
ran), plus `post_id`, `post_url`, `title`, `author`, and `created`. `source_key` is the storage key of the folder the 
file went into, `collection` names that folder's collection (or is `None` for a file stored beside the source's own). 
`key` is the stable identity the file is recognized by on a later run even when its URL is unstable (Possible for 
external hosts).

`result.timings` is a `Timings`: seconds and call counts per pipeline phase (`harvest`, `resolve`, `fetch_wait`,
`fetch_body`, `pace`, `hash`, `write`, `manifest`), plus `downloaded_bytes`. It is what the CLI prints at `-v` —
see [cli.md](cli.md#why-a-run-was-slow) for how to read the phases:

```python
print("\n".join(result.timings.lines(wall=result.duration)))
result.timings.seconds["fetch_body"]   # seconds spent transferring bytes
result.timings.calls["manifest"]       # manifest rewrites this job
```

Phases are measured as wall clock around an `await`, so with concurrent downloads (or concurrent jobs) they overlap.

## Progress events

The library never prints on its own. Wire the `Events` callbacks to `print`, tqdm, a queue, a webhook...
Callbacks may be plain or async functions.

```python
from hoplink import Events, HoplinkExtractor

events = Events(
    on_job_start=lambda source: print("opening", source.url),
    on_scroll=lambda source, total, new: print(f"  {total} posts"),
    on_harvested=lambda source, count: print(f"collected {count}"),
    on_post=lambda source, post, handler: None,
    on_media_found=lambda source, item: print("found", item.filename),
    on_media_saved=lambda source, item: print("saved", item.filename),
    on_skip=lambda source, url, reason: print("skip", url, reason),
    on_job_end=lambda result: print(result.summary()),
)

with HoplinkExtractor(events=events) as hle:
    hle.extract("r/EarthPorn")
```

| Callback         | Fires                                           | Arguments                                   |
|------------------|-------------------------------------------------|---------------------------------------------|
| `on_job_start`   | a job begins                                    | `(source)`                                  |
| `on_scroll`      | after each scroll or pagination round           | `(source, total_collected, new_this_round)` |
| `on_harvested`   | the listing is fully harvested                  | `(source, post_count)`                      |
| `on_post`        | a handler matched a post and produced something | `(source, post, handler)`                   |
| `on_media_found` | a candidate was resolved **during a dry run**   | `(source, item)`                            |
| `on_media_saved` | a file was downloaded and stored                | `(source, item)`                            |
| `on_skip`        | something was skipped or failed                 | `(source, url, reason)`                     |
| `on_job_end`     | a job finished, successfully or not             | `(result)`                                  |

`on_media_found` is dry-run only: in a real run a resolved candidate is downloaded and reports through `on_media_saved` 
or `on_skip`.

Any field may be `None`. Events passed to the constructor are defaults; events passed to a single `extract` call
are merged over them field by field, so you can override one callback for one job.

An exception raised inside a callback is not swallowed: It aborts the current job, which in a batch surfaces as
`result.error` rather than crashing the whole run. With the synchronous facade, callbacks run on the extractor's
internal loop thread.

## Batches

`batch` returns every result once all jobs finish, in input order. `iter_batch` yields each result as it completes.

```python
with HoplinkExtractor() as hle:
    results = hle.batch(
        ["r/EarthPorn", "r/pics", "u/someuser"],
        concurrency=2,
        media_types="image,gallery",
    )

    for result in hle.iter_batch(["r/art", "r/design"], concurrency=2):
        print(result.summary())          # arrives in completion order
```

```python
async with AsyncHoplinkExtractor() as hle:
    results = await hle.batch(["r/EarthPorn", "r/pics"], concurrency=2)

    async for result in hle.iter_batch(["r/art", "r/design"]):
        print(result.summary())
```

Each job runs on its own page of the shared browser. A failed source records its message in `result.error` instead of 
raising; pass `raise_on_error=True` to get the exception instead, in which case the sibling jobs are canceled. Any 
keyword accepted by `extract` (`media_types`, `output_dir`, `dry_run`, `post_filter`, `events`) can be forwarded.

Do not list the same source twice in one concurrent batch: the two jobs would race on a shared manifest.

## Storage backends

By default files go to the filesystem under `config.output_dir`. Swap the backend to write somewhere else:

```python
from hoplink import FilesystemStorage, MemoryStorage, HoplinkExtractor

with HoplinkExtractor(storage=FilesystemStorage("./archive")) as hle:
    hle.extract("r/EarthPorn")

store = MemoryStorage()                  # nothing touches disk
with HoplinkExtractor(storage=store) as hle:
    hle.extract("r/EarthPorn")
print(store.files.keys(), store.manifests.keys())
```

`MemoryStorage` ships for tests and pipelines. To target object storage or a database, subclass `StorageBackend` and
implement `prepare`, `exists`, `write`, `read_manifest`, and `write_manifest`; `list_files` and `location` are optional
overrides. `list_files` is what lets numbering resume past files present in storage but missing from the manifest.

One backend serves many sources: Every method takes the `key` naming a namespace: a source's own (`"EarthPorn"`), or 
one of its collections', which nests with a `/` (`"EarthPorn/someuploader"`). `FilesystemStorage` turns that into a 
subdirectory; a custom backend may treat it as a prefix. Implementations must be safe to call from several asyncio 
tasks as long as each task uses its own key. `FilesystemStorage` writes atomically (temporary file plus rename) and 
retries the rename briefly on Windows, where an antivirus or indexer holding the destination open can cause transient +
`PermissionError`.

Passing `output_dir=` to `extract` alongside an explicit `storage=` raises `ValueError`: configure the backend's
own location instead.

## File names, manifests, and collections

A file is named `<index>_<slug>.<ext>`: an arrival-order index, then a slug of the post's title. A post that resolves 
to several files carries a part number (`0007_02_Sunset_Over_Lofoten.jpg`), and a post whose title slugs to nothing 
keeps the bare `0007.jpg`. `slugify` and `media_filename` are exported if you want to reproduce a name yourself:

```python
from hoplink import media_filename, slugify

slugify("Hello, World!")                        # "Hello_World"
media_filename(7, "jpg", slug="Hello_World")    # "0007_Hello_World.jpg"
```

Slugs are sanitized for Windows (reserved characters and device names are removed) and the length is capped
(`MAX_SLUG_LENGTH`, 120 characters, cut back to a word boundary) so a deep output directory does not let a path exceed
`MAX_PATH`. A collection's name is capped tighter (`MAX_COLLECTION_SLUG_LENGTH`, 60 characters) because it lands in the
path twice: once as the folder, once as the slug of every file inside it.

Each source folder holds a `manifest.json` recording every file's originating post. This makes runs resumable:
a second run skips files already on disk, and continues the numbering rather than restarting at 1.

A handler that answers one post with a *collection* of files (a whole scraped profile) sets `collection` on its
candidates. Those files then get a folder of their own inside the source's, named for the collection, with their own
manifest, their own numbering, and their own identity and hash de-duplication. They are named after the collection 
rather than the post (`0001_someuploader.mp4`). The source's manifest records such a post once, under `collections`, 
instead of carrying an entry per file:

```json
{
  "source": "gifs",
  "generated_at": "2026-07-15T12:00:00+00:00",
  "files": {"0001_A_neat_loop.mp4": {"post_id": "...", "media_url": "...", "media_type": "video"}},
  "collections": {
    "someuploader": {"name": "someUploader", "files": 214, "post_id": "...", "title": "..."}
  }
}
```

`ManifestSet` is the object that holds a job's manifests together (the source's as `.main`, a collection's from
`.collection(name)`) and writes the ones that changed on `.flush()`.

## Custom handlers

A handler answers two questions: *does this post belong to me?* and *which downloadable URLs does it carry?*

```python
from hoplink import MediaCandidate, MediaHandler, MediaType, HoplinkExtractor

class StickerHandler(MediaHandler):
    media_type = MediaType.LINK

    def can_handle(self, post):
        return post.type == "link" and "stickers.example" in (post.content_href or "")

    async def resolve(self, post, ctx):
        return [MediaCandidate(post.content_href, self.media_type, ext="png")]

with HoplinkExtractor() as hle:
    hle.register_handler(StickerHandler())        # prepend=True by default
```

`can_handle` should be a cheap check against already-harvested attributes, with no network access. `resolve` may use
`ctx` to visit pages or fetch resources, and `await ctx.skip(url, reason)` surfaces *why* something was passed over.
Return `[]` when nothing qualifies. An exception escaping `resolve` is recorded in `result.failures` and the job
continues, so one bad post cannot abort a long run.

### The context

`ExtractionContext` is what a handler is given. The supported surface:

| Member                                                                 | Purpose                                                                                                                                                                                                                 |
|------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `ctx.config`, `ctx.formats`, `ctx.wanted`                              | The active config, the accepted image extensions as a frozen set, and the media types this job asked for.                                                                                                               |
| `await ctx.evaluate_on(url, js, arg=None, wait_ms=0)`                  | Navigate a reusable utility page and evaluate JavaScript on it. Separate from the listing page, so the listing's scroll position survives.                                                                              |
| `await ctx.fetch(url, headers=None, retries=None)`                     | GET through the browser context. Successive requests to one host are held `api_pause` apart, and transient failures are retried. A host answering `429` is backed off from far harder, and that wait applies to every request aimed at it. This is the route for API calls, where a token may be bound to the browser's identity. |
| `await ctx.download(url, headers=None)`                                | GET media bytes by whichever route is faster and works.                                                                                                                                                                 |
| `await ctx.skip(url, reason)`                                          | Fire `on_skip`.                                                                                                                                                                                                         |
| `await ctx.sleep(seconds=None)`                                        | Pause, by default for `scroll_pause`.                                                                                                                                                                                   |
| `ctx.state(namespace)`                                                 | A `HostState` shared by every job of this extractor: a `data` dict and the `lock` that guards read-then-write sequences.                                                                                                |
| `ctx.host_option(host, key, default=None)`, `ctx.host_list(host, key)` | Per-host settings from `config.host_options`; the list form normalizes a comma string or an iterable to lower-cased names.                                                                                              |
| `ctx.scrape_all(host)`                                                 | Whether this run wants the host's whole-profile mode.                                                                                                                                                                   |
| `ctx.extension_of(url)`                                                | The lower-cased extension of a URL's path.                                                                                                                                                                              |
| `ctx.page`, `ctx.browser_context`, `await ctx.new_page()`              | Escape hatches for advanced handlers.                                                                                                                                                                                   |

The candidates a handler returns are downloaded by the engine through `ctx.download`, which prefers a direct request
and falls back to the browser per host. Direct requests are prefered as the browser route copies every response body
out through Playwright's driver at a cost that grows with the square of the file's size. Call `ctx.download` yourself
if a handler needs media bytes during `resolve`.

### Candidates

`MediaCandidate` carries what a host may need:

| Field              | Purpose                                                                                                                                                                                                                |
|--------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `url`              | The URL to download.                                                                                                                                                                                                   |
| `media_type`       | Which `MediaType` this file is.                                                                                                                                                                                        |
| `ext`              | The extension to save under. Leave it unset to let the URL's path, and failing that the response's `Content-Type`, settle it — for hosts that serve whatever format they like whatever the URL asked for.              |
| `content_prefixes` | Acceptable `Content-Type` prefixes; a response matching none of them is rejected without a retry. Defaults to `("image/",)`.                                                                                           |
| `body`             | Ready-made bytes. When set, nothing is fetched — this is how a text post's Markdown document is produced.                                                                                                              |
| `collection`       | Store this file in a collection folder of its own (see above).                                                                                                                                                         |
| `headers`          | Extra request headers this file needs, e.g. a `Referer` for a CDN that refuses without one.                                                                                                                            |
| `key`              | A stable identity used instead of `url` to recognize the file on a later run. Set this for signed URLs, whose expiry and signature change on every resolve — de-duplicating on the URL would re-download them forever. |

A collection candidate needs only this:

```python
async def resolve(self, post, ctx):
    author = await self._author_of(post, ctx)
    return [
        MediaCandidate(url, self.media_type, ext="mp4", collection=author)
        for url in await self._page_profile(author, ctx)
    ]
```

The name is slugged into a folder name for you, and a collection met twice in one job keeps a single numbering
sequence.

### The built-in chain

Registered handlers are prepended so they take precedence over the built-ins; pass `prepend=False` to append. The
built-in chain, in selection order, is `ImageHandler`, `GalleryHandler`, `ExternalLinkHandler`, `VideoHandler`,
`CrosspostHandler`, `TextHandler`, `PollHandler`, `LinkImageHandler` — available individually from
`hoplink.handlers`, or as a list from `default_handlers(config)`. Passing `handlers=` to the constructor
replaces the chain entirely.

`ExternalLinkHandler` precedes `VideoHandler` so a RedGIFs post is resolved to its audio-bearing MP4 (via the RedGIFs 
API) rather than a silent `v.redd.it` rehost. `LinkImageHandler` claims *any* link post, so it sets `fallback = True` 
and is consulted only once every other handler has passed.

A handler's `media_type` is a *selection* key, not a promise about what comes back: it may combine flags
(`IMAGE | GALLERY`), an instance may compute its own in `__init__` from how it was configured, and the candidates carry
their own types, which may differ (e.g. a crosspost is claimed as `CROSSPOST` and resolves to images or video). 
A handler that records a matched post but never produces files sets `metadata_only = True`, so an empty candidate list 
still counts as a match.

## Link resolvers

Media on an external host is resolved by a `LinkResolver`, matched by domain rather than by post type. This allows to 
reuse the resolver: link post's target, behind a crosspost, or inside the body of a text post.

```python
from hoplink import (
    AsyncHoplinkExtractor, ExternalLinkHandler, LinkResolver, MediaCandidate, MediaType,
)

class ImgchestResolver(LinkResolver):
    host = "imgchest"                  # key for host_options, state, and scrape_all_hosts
    domains = ("imgchest.com",)        # subdomains included; the boundary is pinned to a dot
    media_type = MediaType.GALLERY     # skipped entirely when the job wants none of it

    async def resolve(self, url, ctx, *, ref):
        result = await ctx.fetch(url)  # paced per host, transient failures retried
        if not result.ok or result.body is None:
            await ctx.skip(ref, "imgchest: {} is unreadable".format(url))
            return []
        return [MediaCandidate(u, self.media_type) for u in files_in(result.body)]

hle = AsyncHoplinkExtractor(handlers=[ExternalLinkHandler([ImgchestResolver()])])
```

Override `claims(url)` for a host that needs to look at the path too — claiming `/a/<id>` album URLs but not the
rest of the site, say. The default matches `domains`, subdomains included, with the boundary pinned to a dot so
`notredgifs.com` does not match `redgifs.com`.

Keep the parsing in plain functions taking bytes and returning data, as the built-in resolvers do (those test without
a browser, an event loop, or a fake context. `default_resolvers()` returns the built-in set, and `ResolverRegistry` is 
the lookup. Because the registry is keyed by domain, registration order carries no meaning. A resolver whose 
`media_type` the job did not ask for is not consulted. The registry also acts as allowlist for `follow_text_links`: 
only registered hosts are fetched out of a post body.

### Whole profiles, blacklists, and per-host settings

With a host named in `scrape_all_hosts`, its resolver treats each post as a pointer to the *uploader*:
`RedGifsResolver` reads the clip's `userName` and pages that uploader's entire RedGIFs profile into candidates, so
one linked clip yields the creator's whole catalogue. Those candidates name the uploader as their collection, so
each profile is stored in a folder of its own with its own manifest.

Each uploader is scraped at most once per extractor. The claim lives in `ctx.state("redgifs")`, taken under its
lock, so two concurrent jobs cannot both page one profile, and it is given back if the paging comes up empty.
(This is per run; the profile's own manifest still de-duplicates across runs, so a later run fetches only clips
added since.) Anonymous uploads, or a profile that reads back empty, fall back to just the linked clip.

`host_options` carries per-host settings, read through `ctx.host_option(host, key)` or `ctx.host_list(host, key)`
for name lists:

```python
ExtractorConfig(
    scrape_all_hosts=("redgifs",),
    host_options={
        "redgifs": {"blacklist": ["spammer", "adbot"]},
        "imgur": {"client_id": "..."},
    },
)
```

A `blacklist` names uploaders whose media is not wanted (a comma string or an iterable, matched case-insensitively). 
Once a clip's metadata names a blacklisted uploader, the post is skipped before anything is downloaded (in both 
single-item and scrape-all mode). Anonymous uploads carry no username and are not blocked.

### Following the links in text posts

`follow_text_links=True` (CLI: `--follow-links`) makes `TextHandler` read the links out of a self post's body and
resolve the ones whose host is registered, so the post yields that media alongside its Markdown document.

Following widens the handler's `media_type` to cover what the resolvers produce, so asking for just those types still
selects text posts. Each candidate is kept only if the job asked for its kind (a run wanting only video gets the linked 
clips without a folder of Markdown files). Reddit's `out.reddit.com/...?url=` wrappers are unwrapped before matching, 
non-`http(s)` links and duplicates are dropped, and a body link to an unregistered host is not fetched.

Reading the body costs one page visit per text post with or without this flag. Following adds the resolvers' requests.

## NSFW and private sources

Logged-out browsing cannot see NSFW or some private content. A session lives in a persistent browser profile, and
`login` puts one there: it opens Reddit's login page in a visible window, waits for the sign-in, and confirms it.
No source is opened and nothing is downloaded.

```python
from hoplink import ExtractorConfig, HoplinkExtractor, login

# once, to sign in (blocks until you finish, or until `timeout` seconds pass; five minutes by default)
result = login(ExtractorConfig(profile_dir="./.reddit_profile"), on_status=print)
if not result.ok:
    raise SystemExit("not signed in: {}".format(result.reason))

# afterwards, headless
with HoplinkExtractor(profile_dir="./.reddit_profile") as hle:
    hle.extract("r/somensfwsub")
```

`login` returns a `LoginResult`:

| Field               | Meaning                                                                                 |
|---------------------|-------------------------------------------------------------------------------------------|
| `ok`                | whether the profile ended up signed in                                                  |
| `username`          | the account name, when the check that confirmed the session carried one                 |
| `reason`            | why an unsuccessful attempt ended, phrased for a user                                   |
| `already_logged_in` | the profile was signed in when the window opened, so nothing had to be typed            |

Calling it on a profile that is already signed in returns immediately with `already_logged_in`, which makes it a
session check as much as a sign-in. `headless` is overridden for the duration: a sign-in nobody can see cannot be
completed. Without a `profile_dir` it raises `ValueError`, since an incognito session is thrown away at close.

`async_login` is the same thing for callers with an event loop of their own, and `login_state(browser, page)` answers
the underlying question ("is this browser signed in?") for one already started `BrowserManager`.

The directory is created on first use. It holds a logged-in session, so keep it secret.

## Errors

All exceptions derive from `HoplinkExtractError`:

| Exception             | Raised when                                                                                                  |
|-----------------------|--------------------------------------------------------------------------------------------------------------|
| `HoplinkExtractError` | base class for everything below                                                                              |
| `BrowserError`        | the browser could not be started, or has died                                                                |
| `NoPostsFoundError`   | a listing rendered no posts: empty, private, banned, or NSFW while logged out. Carries the offending `.url`. |
| `ConfigFileError`     | a TOML config file is missing, malformed, or names an unknown key                                            |

The first three are re-exported at the top level. `ConfigFileError` belongs to the CLI's config loader, so import
it from `hoplink.exceptions` if you need it.

```python
from hoplink import HoplinkExtractError, HoplinkExtractor, NoPostsFoundError

try:
    with HoplinkExtractor() as hle:
        hle.extract("r/somensfwsub")
except NoPostsFoundError as exc:
    print("nothing rendered at", exc.url)
except HoplinkExtractError as exc:
    print("extraction failed:", exc)
```

Invalid arguments raise plain `ValueError`: an unknown media type name, an unsupported sort for a source, a negative 
pacing value, an invalid title regex.

In a batch, a per-source failure is recorded in `result.error` instead of raising (unless `raise_on_error=True`).

## Logging

The library logs to the `hoplink` logger and attaches only a `NullHandler`, so it stays silent until you
configure logging yourself:

```python
import logging

logging.basicConfig(level=logging.INFO)
logging.getLogger("hoplink").setLevel(logging.DEBUG)
```

Logs are the debugging channel (retries, per-post detail, per-fetch sizes, timings). `Events` is the progress channel.
