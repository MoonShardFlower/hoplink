#!/usr/bin/env python3
"""
Reddit image scraper.

Downloads pictures (jpg/jpeg/png/webp by default) from a subreddit's most recent posts.
For every picture it saves, it records the originating Reddit post in ``manifest.json``.

Examples:
    python reddit_image_scraper.py https://www.reddit.com/r/EarthPorn/
    python reddit_image_scraper.py r/pics --limit 1000
    python reddit_image_scraper.py EarthPorn --show          # watch the browser
    python reddit_image_scraper.py r/somensfw --profile .rprofile --show
        (run once, log in manually in the window; the session is reused next time)

First-time setup:
    pip install playwright requests
    python -m playwright install chromium
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ---- constants --------------------------------------------------------------

DEFAULT_FORMATS = ("jpg", "jpeg", "png", "webp")
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")

# JS run in the page: read every rendered post card's key attributes.
JS_HARVEST = """
() => Array.from(document.querySelectorAll('shreddit-post')).map(p => {
  const titleEl = p.querySelector('[slot="title"]');
  return {
    id: p.getAttribute('id'),
    type: p.getAttribute('post-type'),
    permalink: p.getAttribute('permalink'),
    content_href: p.getAttribute('content-href'),
    author: p.getAttribute('author'),
    created: p.getAttribute('created-timestamp'),
    title: titleEl ? titleEl.textContent.trim() : null,
  };
})
"""

# JS run on a gallery's comment page: return the gallery carousel's raw HTML,
# scoped to the main post only (so we don't pick up sidebar/related thumbnails).
JS_GALLERY = """
(pid) => {
  const post = document.getElementById(pid) || document.querySelector('shreddit-post');
  if (!post) return "";
  const car = post.querySelector('gallery-carousel');
  return car ? car.outerHTML : "";
}
"""


# ---- helpers ----------------------------------------------------------------

def parse_subreddit(value):
    """Accept a full URL, ``r/name``, ``/r/name/`` or a bare name."""
    value = value.strip()
    m = re.search(r"reddit\.com/r/([^/?#]+)", value, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"^/?r/([^/?#]+)", value, re.IGNORECASE)
    if m:
        return m.group(1)
    return value.strip("/")


def listing_url(subreddit, sort, time_filter):
    base = "https://www.reddit.com/r/{}/".format(subreddit)
    if sort == "hot":
        return base
    if sort == "top":
        return base + "top/?t=" + time_filter
    return base + sort + "/"          # new / rising


def extension_from_url(url):
    ext = os.path.splitext(urlparse(url).path)[1].lower().lstrip(".")
    return ext


def iso_from_attr(value):
    """shreddit posts expose created-timestamp as epoch-millis or ISO text."""
    if not value:
        return None
    try:
        ms = int(value)
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except (ValueError, TypeError):
        return value


def gallery_image_urls(carousel_html, allowed_formats):
    """
    Reconstruct full-resolution i.redd.it URLs from a gallery carousel.

    Reddit renders gallery slides as preview.redd.it URLs whose final path token is the i.redd.it media id, e.g.,
        https://preview.redd.it/some-slug-v0-3ps5r77zi1ah1.jpg?width=640&...
    -> https://i.redd.it/3ps5r77zi1ah1.jpg
    """
    urls, seen = [], set()
    for m in re.finditer(r"https://preview\.redd\.it/([^\s\"'<>?]+)", carousel_html):
        base, ext = os.path.splitext(m.group(1))
        ext = ext.lower().lstrip(".")
        if ext not in allowed_formats:          # excludes gifs / odd formats
            continue
        token = base.split("-")[-1]             # trailing token = media id
        if not re.fullmatch(r"[A-Za-z0-9]{6,}", token):
            continue
        if token in seen:
            continue
        seen.add(token)
        urls.append("https://i.redd.it/{}.{}".format(token, ext))
    return urls


# ---- browser ----------------------------------------------------------------

def open_context(pw, args):
    """Return (closeable, context). Uses a persistent profile if requested."""
    if args.profile:
        os.makedirs(args.profile, exist_ok=True)
        ctx = pw.chromium.launch_persistent_context(
            args.profile, headless=not args.show, user_agent=args.user_agent,
            viewport={"width": 1366, "height": 900}, locale="en-US")
        return ctx, ctx
    browser = pw.chromium.launch(headless=not args.show)
    ctx = browser.new_context(user_agent=args.user_agent,
                              viewport={"width": 1366, "height": 900}, locale="en-US")
    return browser, ctx


def dismiss_gates(page):
    """Best-effort click-through of cookie / over-18 interstitials."""
    for name in ["Accept all", "Yes, I'm over 18", "I'm over 18",
                 "Continue", "View NSFW content", "Yes"]:
        try:
            btn = page.get_by_role("button", name=re.compile(name, re.I))
            if btn.count():
                btn.first.click(timeout=1500)
                page.wait_for_timeout(400)
        except Exception:
            pass


def harvest_posts(page, url, target, pause, max_stale):
    """
    Scroll the listing and collect up to ``target`` unique posts.

    The feed DOM is virtualized (off-screen cards are recycled), so we accumulate posts on every scroll rather than
    reading them all at the end.
    """
    print("Opening {} ...".format(url))
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    dismiss_gates(page)
    try:
        page.wait_for_selector("shreddit-post", timeout=30000)
    except PWTimeout:
        sys.exit("No posts rendered. The subreddit may be empty, private, or "
                 "(if NSFW) require login -- try --profile with --show to sign in.")

    harvested, order = {}, []
    stale = 0
    while len(harvested) < target and stale < max_stale:
        new_this_round = 0
        for p in page.evaluate(JS_HARVEST):
            pid = p.get("id")
            if pid and pid not in harvested:
                harvested[pid] = p
                order.append(pid)
                new_this_round += 1
        stale = stale + 1 if new_this_round == 0 else 0
        print("  ...{} posts collected{}".format(
            len(harvested), " (no new this scroll)" if new_this_round == 0 else ""))
        page.mouse.wheel(0, 18000)
        page.wait_for_timeout(int(pause * 1000))

    return [harvested[pid] for pid in order[:target]]


def download(ctx, url, dest_path):
    """Download an image through the browser context. Returns (ok, reason)."""
    try:
        resp = ctx.request.get(url, timeout=60000)
        if not resp.ok:
            return False, "HTTP {}".format(resp.status)
        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ctype == "image/gif":
            return False, "is a gif"
        if ctype and not ctype.startswith("image/"):
            return False, "not an image ({})".format(ctype)
        tmp = dest_path + ".part"
        with open(tmp, "wb") as f:
            f.write(resp.body())
        os.replace(tmp, dest_path)
        return True, "ok"
    except Exception as exc:
        return False, "error: {}".format(exc)


# ---- manifest ---------------------------------------------------------------

def load_manifest(path, subreddit):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("images", {})
            return data
        except (ValueError, OSError):
            pass
    return {"subreddit": subreddit, "generated_at": None, "images": {}}


def save_manifest(path, manifest):
    manifest["generated_at"] = datetime.now(timezone.utc).isoformat()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)


def starting_index(manifest):
    mx = 0
    for fname in manifest["images"]:
        m = re.match(r"(\d+)", fname)
        if m:
            mx = max(mx, int(m.group(1)))
    return mx


# ---- main -------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="Download only real pictures from a subreddit's recent posts.")
    p.add_argument("subreddit", help="Subreddit URL, r/name, or bare name.")
    p.add_argument("--limit", type=int, default=1000, help="Max posts to scan.")
    p.add_argument("--sort", choices=["new", "hot", "top", "rising"], default="top",
                   help="Listing to scan (default: new = most recent).")
    p.add_argument("--time", dest="time_filter", default="all",
                   choices=["hour", "day", "week", "month", "year", "all"],
                   help="Time window when --sort top (default: all).")
    p.add_argument("--out", default="downloads", help="Base output directory.")
    p.add_argument("--formats", default=",".join(DEFAULT_FORMATS),
                   help="Comma-separated image extensions to keep.")
    p.add_argument("--scroll-pause", type=float, default=2.0,
                   help="Seconds between scrolls while loading the feed.")
    p.add_argument("--img-delay", type=float, default=0.5,
                   help="Seconds between image downloads.")
    p.add_argument("--max-stale-scrolls", type=int, default=10,
                   help="Stop after this many scrolls with no new posts.")
    p.add_argument("--show", action="store_true",
                   help="Show the browser window (headful).")
    p.add_argument("--profile", default=None,
                   help="Persistent browser profile dir (lets you stay logged in).")
    p.add_argument("--user-agent", default=DEFAULT_UA, help="Custom User-Agent.")
    return p.parse_args()


def main():
    args = parse_args()
    subreddit = parse_subreddit(args.subreddit)
    allowed = tuple(f.strip().lower().lstrip(".")
                    for f in args.formats.split(",") if f.strip())

    out_dir = os.path.join(args.out, subreddit)
    os.makedirs(out_dir, exist_ok=True)
    manifest_path = os.path.join(out_dir, "manifest.json")
    manifest = load_manifest(manifest_path, subreddit)
    known_urls = {info.get("image_url") for info in manifest["images"].values()}

    url = listing_url(subreddit, args.sort, args.time_filter)

    with sync_playwright() as pw:
        closeable, ctx = open_context(pw, args)
        page = ctx.new_page()
        posts = harvest_posts(page, url, args.limit, args.scroll_pause,
                              args.max_stale_scrolls)
        print("\nCollected {} posts. Resolving pictures ...\n".format(len(posts)))

        gallery_page = ctx.new_page()
        post_index = starting_index(manifest)
        saved = skipped_existing = posts_with_pics = 0

        for d in posts:
            ptype = d.get("type")
            href = d.get("content_href") or ""
            images = []

            if ptype == "image":
                if href and extension_from_url(href) in allowed:
                    images = [href]
            elif ptype == "gallery" and d.get("permalink"):
                try:
                    gallery_page.goto("https://www.reddit.com" + d["permalink"],
                                      wait_until="domcontentloaded", timeout=60000)
                    gallery_page.wait_for_timeout(2000)
                    html = gallery_page.evaluate(JS_GALLERY, d["id"])
                    images = gallery_image_urls(html, allowed)
                    time.sleep(args.scroll_pause)
                except Exception as exc:
                    print("  ! gallery {} failed: {}".format(d.get("permalink"), exc))
            # video / gif / link / text -> intentionally skipped

            if not images:
                continue
            posts_with_pics += 1
            post_index += 1
            permalink = "https://www.reddit.com" + (d.get("permalink") or "")
            multi = len(images) > 1
            for i, img_url in enumerate(images, 1):
                if img_url in known_urls:
                    continue
                ext = extension_from_url(img_url) or "jpg"
                fname = ("{:04d}_{:02d}.{}".format(post_index, i, ext) if multi
                         else "{:04d}.{}".format(post_index, ext))
                dest = os.path.join(out_dir, fname)
                if os.path.exists(dest):
                    skipped_existing += 1
                    continue
                ok, reason = download(ctx, img_url, dest)
                if ok:
                    manifest["images"][fname] = {
                        "post_id": d.get("id"),
                        "post_url": permalink,
                        "title": d.get("title"),
                        "author": d.get("author"),
                        "created": iso_from_attr(d.get("created")),
                        "image_url": img_url,
                    }
                    known_urls.add(img_url)
                    saved += 1
                    save_manifest(manifest_path, manifest)
                    print("  saved {:<16} <- {}".format(fname, permalink))
                else:
                    print("  skip  {} ({})".format(img_url, reason))
                time.sleep(args.img_delay)

        save_manifest(manifest_path, manifest)
        try:
            closeable.close()
        except Exception:
            pass

    print("\nDone.")
    print("  posts scanned       : {}".format(len(posts)))
    print("  posts with pictures : {}".format(posts_with_pics))
    print("  images saved (new)  : {}".format(saved))
    if skipped_existing:
        print("  already on disk     : {}".format(skipped_existing))
    print("  output folder       : {}".format(os.path.abspath(out_dir)))
    print("  manifest            : {}".format(os.path.abspath(manifest_path)))


if __name__ == "__main__":
    main()
