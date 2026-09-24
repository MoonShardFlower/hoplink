.. hoplink documentation master file.

hoplink documentation
=====================

Composable, browser-driven Reddit media extraction that hops outbound links to their real host.

Reddit blocks plain HTTP access to its JSON/OAuth API from most IPs.
``hoplink`` sidesteps that by driving a real browser (Playwright + Chromium) to render listings the way your
browser does, then downloading the underlying full-resolution media from Reddit's CDN.

It handles single images, multi-image galleries, videos (packaged MP4s, with a DASH fallback), external image links,
and self/text posts (recorded as metadata). Subreddits, user profiles, and multireddits are all supported,
including Reddit's legacy layout.

Installation
------------

.. code-block:: bash

   pip install hoplink
   python -m playwright install chromium

Command line
------------

.. code-block:: bash

   # Top 50 images + galleries from r/EarthPorn this month
   hoplink r/EarthPorn --limit 50 --sort top --time month

   # Store a whole run in a TOML file and load it
   hoplink --config myjob.toml

   # Skip reposts by content, and write a machine-readable report
   hoplink r/pics --dedupe --report run.json

Command-line arguments override a ``--config`` file, which overrides the
built-in defaults. Ready-to-run example configs live in the project's
``examples/`` directory.

Python API
----------

Synchronous:

.. code-block:: python

   from hoplink import HoplinkExtractor, Subreddit, MediaType

   with HoplinkExtractor(headless=True) as hle:
       result = hle.extract(
           Subreddit("EarthPorn", limit=50, sort="top", time_filter="month"),
           media_types=MediaType.IMAGE | MediaType.GALLERY,
           output_dir="./downloads",
       )
       print(result.summary())

Async:

.. code-block:: python

   import asyncio
   from hoplink import AsyncHoplinkExtractor

   async def main():
       async with AsyncHoplinkExtractor(headless=True) as hle:
           async for result in hle.iter_batch(
               ["r/EarthPorn", "r/pics", "u/someuser"],
               media_types="image,gallery",
               concurrency=2,
           ):
               print(result.summary())

   asyncio.run(main())

NSFW and private sources
------------------------

Logged-out browsing can't see NSFW or some private content. A session lives in a persistent browser profile, and
:func:`hoplink.login` puts one there: it opens Reddit's login page in a visible window and waits for the sign-in,
scraping nothing. Point ``profile_dir`` at that same directory afterwards and every run reuses the session::

    from hoplink import ExtractorConfig, login

    login(ExtractorConfig(profile_dir="./.reddit_profile"))

From the command line that is ``hoplink --login --profile ./.reddit_profile``.

API reference
-------------

.. toctree::
   :maxdepth: 2
   :caption: Contents:

   modules
