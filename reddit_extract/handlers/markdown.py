"""
Render a harvested post as a Markdown document with a YAML metadata header.

Used by the handlers that archive a post's *text* rather than a media file (self posts and polls). The output is a
portable ``.md`` file: a YAML front-matter block carrying the post's provenance, followed by the title and body.
"""

from __future__ import annotations

from typing import List, Mapping

from ..models.post import Post


def _yaml_scalar(value: object) -> str:
    """
    Render ``value`` as a safe single-line YAML scalar.

    Numbers and booleans pass through bare; everything else is emitted as a double-quoted string with the two
    characters that matter inside double quotes (``\\`` and ``"``) escaped and newlines/tabs folded to escapes,
    so a title full of colons, hashes, or quotes can never break the header.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    text = text.replace("\n", "\\n").replace("\t", "\\t").replace("\r", "")
    return '"{}"'.format(text)


def _front_matter(fields: Mapping[str, object]) -> str:
    """Build a YAML front-matter block from ``fields``, skipping keys whose value is None."""
    lines: List[str] = ["---"]
    for key, value in fields.items():
        if value is None:
            continue
        lines.append("{}: {}".format(key, _yaml_scalar(value)))
    lines.append("---")
    return "\n".join(lines)


def post_to_markdown(post: Post, body: str = "") -> str:
    """
    Render a post as a Markdown document.

    The document opens with a YAML front-matter block (title, author, creation date, subreddit, permalink,
    score, comment count, flair, id, and type), then repeats the title as a heading and appends ``body``.

    Args:
        post: The harvested post to describe.
        body: The post's text content (a self post's body, a poll's rendered options, ...). May be empty.

    Returns:
        The Markdown document as a single string, newline-terminated.
    """
    header = _front_matter(
        {
            "title": post.title or "",
            "author": post.author or "",
            "date": post.created,
            "subreddit": post.subreddit,
            "url": post.url,
            "score": post.score,
            "comments": post.comment_count,
            "flair": post.flair,
            "id": post.id or None,
            "type": post.type,
        }
    )
    title = (post.title or "").strip()
    parts = [header, ""]
    if title:
        parts += ["# {}".format(title), ""]
    body = body.strip()
    if body:
        parts += [body, ""]
    return "\n".join(parts)
