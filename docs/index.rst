.. reddit-extract documentation master file.

reddit-extract documentation
============================

Composable, browser-driven Reddit media extraction.

Reddit blocks plain HTTP access to its JSON/OAuth API from most IPs.
``reddit-extract`` sidesteps that by driving a real browser (Playwright + Chromium) to render listings the way your
browser does, then downloading the underlying full-resolution media from Reddit's CDN.

It handles single images, multi-image galleries, videos (packaged MP4s, with a DASH fallback), external image links,
and self/text posts (recorded as metadata). Subreddits, user profiles, and multireddits are all supported,
including Reddit's legacy layout.

Installation
------------

.. code-block:: bash

   pip install reddit-extract
   python -m playwright install chromium

Command line
------------

.. code-block:: bash

   # Top 50 images + galleries from r/EarthPorn this month
   reddit-extract r/EarthPorn --limit 50 --sort top --time month

   # Store a whole run in a TOML file and load it
   reddit-extract --config myjob.toml

   # Skip reposts by content, and write a machine-readable report
   reddit-extract r/pics --dedupe --report run.json

Command-line arguments override a ``--config`` file, which overrides the
built-in defaults. Ready-to-run example configs live in the project's
``examples/`` directory.

Python API
----------

Synchronous:

.. code-block:: python

   from reddit_extract import RedditExtractor, Subreddit, MediaType

   with RedditExtractor(headless=True) as rex:
       result = rex.extract(
           Subreddit("EarthPorn", limit=50, sort="top", time_filter="month"),
           media_types=MediaType.IMAGE | MediaType.GALLERY,
           output_dir="./downloads",
       )
       print(result.summary())

Async:

.. code-block:: python

   import asyncio
   from reddit_extract import AsyncRedditExtractor

   async def main():
       async with AsyncRedditExtractor(headless=True) as rex:
           async for result in rex.iter_batch(
               ["r/EarthPorn", "r/pics", "u/someuser"],
               media_types="image,gallery",
               concurrency=2,
           ):
               print(result.summary())

   asyncio.run(main())

NSFW and private sources
------------------------

Logged-out browsing can't see NSFW or some private content. Point ``profile_dir`` at a persistent browser profile and
run once headful (``headless=False``) to sign in. The session is reused on later runs.

API reference
-------------

.. toctree::
   :maxdepth: 2
   :caption: Contents:

   modules
