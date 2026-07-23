"""
Tests for rendering a post as a Markdown document (used by the text and poll handlers).

Browser-free: `post_to_markdown` is a pure function over a harvested Post.
"""

from __future__ import annotations

from typing import Any

from reddit_extract.handlers.markdown import post_to_markdown
from reddit_extract.models.post import Post


def post(**overrides: Any) -> Post:
    """One harvested self post."""
    data: dict[str, Any] = {
        "id": "t3_abc",
        "type": "text",
        "permalink": "/r/tifu/comments/abc/story/",
        "author": "bob",
        "title": "A normal title",
        "subreddit": "r/tifu",
        "created": "2026-07-16T11:45:34.060000+0000",
        "score": "42",
        "comment_count": "7",
    }
    data.update(overrides)
    return Post.from_harvest(data)


def test_the_document_opens_and_closes_the_front_matter():
    doc = post_to_markdown(post(), "the body")
    assert doc.startswith("---\n")
    assert doc.count("---") == 2  # exactly one front-matter block


def test_the_metadata_the_user_asked_for_is_present():
    doc = post_to_markdown(post(), "the body")
    assert 'title: "A normal title"' in doc
    assert 'author: "bob"' in doc
    assert 'date: "2026-07-16T11:45:34.060000+00:00"' in doc


def test_the_permalink_and_subreddit_are_recorded():
    doc = post_to_markdown(post())
    assert 'url: "https://www.reddit.com/r/tifu/comments/abc/story/"' in doc
    assert 'subreddit: "tifu"' in doc


def test_numbers_are_emitted_bare_not_quoted():
    doc = post_to_markdown(post())
    assert "score: 42" in doc
    assert "comments: 7" in doc


def test_the_title_is_repeated_as_a_heading_then_the_body_follows():
    doc = post_to_markdown(post(), "Once upon a time.")
    assert "# A normal title" in doc
    assert doc.rstrip().endswith("Once upon a time.")


def test_a_title_full_of_yaml_metacharacters_cannot_break_the_header():
    # Colons, hashes, and quotes in a title would derail a naive front-matter writer.
    doc = post_to_markdown(post(title='Re: "scary" #1: it: broke'))
    assert 'title: "Re: \\"scary\\" #1: it: broke"' in doc


def test_a_newline_in_a_title_is_folded_to_an_escape():
    doc = post_to_markdown(post(title="line one\nline two"))
    assert 'title: "line one\\nline two"' in doc


def test_a_missing_optional_field_is_omitted_entirely():
    # An unflaired post gets no `flair:` line rather than a null one.
    doc = post_to_markdown(post(flair=None))
    assert "flair:" not in doc


def test_an_empty_body_still_yields_the_metadata_and_heading():
    doc = post_to_markdown(post(), "")
    assert "# A normal title" in doc
    assert 'author: "bob"' in doc


def test_a_backslash_in_the_body_is_left_in_the_body_verbatim():
    # Only the YAML header escapes; the Markdown body is copied as-is.
    doc = post_to_markdown(post(), r"a path C:\Users\bob and done")
    assert r"a path C:\Users\bob and done" in doc
