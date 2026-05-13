"""Shared formatting and pagination helpers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable


class ResponseFormat(str, Enum):
    MARKDOWN = "markdown"  # compact, for direct LLM context
    JSON = "json"          # raw Moodle payload, for inspection/debug
    RAG = "rag"            # uniform Document[] with stable IDs, for vector store ingestion


def fmt_timestamp(ts: int | float | None) -> str:
    """Moodle returns Unix timestamps (seconds). Format as ISO UTC."""
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, OSError):
        return str(ts)


def strip_html(html: str | None) -> str:
    """Crude tag stripper for Moodle HTML descriptions/posts.

    Good enough for LLM context — preserves text content, drops markup.
    Not a security boundary; never trust this for output to a browser.
    """
    if not html:
        return ""
    import re

    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    # Decode common entities
    replacements = {
        "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
        "&quot;": '"', "&#39;": "'", "&apos;": "'",
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def paginate(items: list[Any], offset: int, limit: int) -> tuple[list[Any], dict[str, Any]]:
    """Client-side pagination wrapper.

    Many Moodle WS functions don't paginate server-side, so we slice here.
    Returns the page plus a metadata dict suitable for inclusion in responses.
    """
    total = len(items)
    page = items[offset : offset + limit]
    has_more = offset + len(page) < total
    return page, {
        "total": total,
        "count": len(page),
        "offset": offset,
        "has_more": has_more,
        "next_offset": offset + len(page) if has_more else None,
    }


def as_response(
    data: Any,
    fmt: ResponseFormat,
    markdown_renderer,
    pagination: dict[str, Any] | None = None,
) -> str:
    """Dispatch on response format.

    `markdown_renderer` is a callable taking (data, pagination) -> str.
    """
    if fmt == ResponseFormat.JSON:
        payload: dict[str, Any] = {"data": data}
        if pagination is not None:
            payload["pagination"] = pagination
        return json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    return markdown_renderer(data, pagination)


def md_pagination_footer(pagination: dict[str, Any] | None) -> str:
    if not pagination:
        return ""
    p = pagination
    base = f"\n\n_Showing {p['count']} of {p['total']} • offset={p['offset']}_"
    if p.get("has_more"):
        base += f" • _next_offset={p['next_offset']}_"
    return base


def truncate(text: str, max_chars: int = 500) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"


def safe_get(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Navigate nested dicts/lists tolerantly."""
    cur: Any = d
    for k in keys:
        if isinstance(cur, dict):
            cur = cur.get(k, default)
        else:
            return default
        if cur is None:
            return default
    return cur


def bullet_list(items: Iterable[str]) -> str:
    return "\n".join(f"- {x}" for x in items if x)
