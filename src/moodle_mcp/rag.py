"""RAG document formatting.

Converts Moodle entities into a uniform Document shape suitable for vector
store ingestion. The key design decisions:

1. **Stable IDs**: every document has a deterministic URI like
   `moodle://{host}/forum_post/{id}`. Same entity → same ID across syncs →
   safe upsert into vector stores keyed by document ID.

2. **One entity = one document**: no splitting. Chunking is the consumer's
   job because it depends on the embedding model's token budget.

3. **Plain text content**: HTML stripped, ready for embedding. No need for
   the consumer to re-clean.

4. **Rich metadata**: every field a typical retrieval filter would want
   (course_id, author, timestamps) is in `metadata`, not buried in content.

5. **Sync envelope**: response carries `latest_modified_at`, the max
   modification timestamp across documents in this batch. Consumers persist
   it and pass it back as `time_modified_since` on the next call.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, TypedDict
from urllib.parse import urlparse

from .formatting import strip_html


class Document(TypedDict):
    """Uniform document shape for RAG ingestion.

    `id` is the stable, deterministic URI used as the vector store primary key.
    `content` is plain text ready for embedding. `metadata` is structured
    filter data; nothing essential should be ONLY in content.
    """
    id: str
    type: str
    title: str
    content: str
    metadata: dict[str, Any]


def _host(base_url: str) -> str:
    """Extract a stable host slug from MOODLE_URL for use in document URIs."""
    parsed = urlparse(base_url)
    return parsed.netloc or "moodle"


def _iso(ts: int | float | None) -> str | None:
    """Unix timestamp → ISO 8601 UTC string, or None."""
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (ValueError, OSError):
        return None


def make_doc_id(host: str, doc_type: str, entity_id: int | str) -> str:
    """Build a stable URI for a Moodle entity.

    Examples:
        moodle://moodle.unibo.it/course/42
        moodle://moodle.unibo.it/forum_post/12345
        moodle://moodle.unibo.it/assignment/100
    """
    return f"moodle://{host}/{doc_type}/{entity_id}"


# ---------------------------------------------------------------------------
# Per-entity converters. Each takes the raw Moodle payload and emits a list
# of Documents. They're pure functions — no I/O — so they're trivially testable.
# ---------------------------------------------------------------------------


def courses_to_docs(courses: list[dict], host: str) -> list[Document]:
    docs: list[Document] = []
    for c in courses:
        cid = c.get("id")
        if not cid:
            continue
        docs.append({
            "id": make_doc_id(host, "course", cid),
            "type": "course",
            "title": c.get("fullname") or c.get("shortname") or f"Course {cid}",
            "content": strip_html(c.get("summary", "")),
            "metadata": {
                "course_id": cid,
                "shortname": c.get("shortname"),
                "category_id": c.get("categoryid"),
                "category_name": c.get("categoryname"),
                "start_date": _iso(c.get("startdate")),
                "end_date": _iso(c.get("enddate")),
                "visible": bool(c.get("visible", True)),
                "format": c.get("format"),
            },
        })
    return docs


def course_contents_to_docs(sections: list[dict], course_id: int, host: str) -> list[Document]:
    """Each section → one document, each module → one document.

    Section docs are useful for "what's in week N" queries; module docs
    are the leaf-level documents (one per activity/resource).
    """
    docs: list[Document] = []
    for s in sections:
        section_id = s.get("id")
        section_num = s.get("section")
        if section_id:
            docs.append({
                "id": make_doc_id(host, "section", section_id),
                "type": "section",
                "title": s.get("name") or f"Section {section_num}",
                "content": strip_html(s.get("summary", "")),
                "metadata": {
                    "course_id": course_id,
                    "section_id": section_id,
                    "section_number": section_num,
                    "visible": bool(s.get("visible", True)),
                },
            })
        for m in s.get("modules", []) or []:
            mid = m.get("id")
            if not mid:
                continue
            docs.append({
                "id": make_doc_id(host, "module", mid),
                "type": "module",
                "title": m.get("name") or f"Module {mid}",
                "content": strip_html(m.get("description", "")),
                "metadata": {
                    "course_id": course_id,
                    "section_id": section_id,
                    "section_number": section_num,
                    "module_id": mid,
                    "module_type": m.get("modname"),
                    "instance_id": m.get("instance"),
                    "url": m.get("url"),
                    "visible": bool(m.get("visible", True)),
                },
            })
    return docs


def assignments_to_docs(courses: list[dict], host: str) -> list[Document]:
    docs: list[Document] = []
    for c in courses:
        course_id = c.get("id")
        course_name = c.get("fullname")
        for a in c.get("assignments", []) or []:
            aid = a.get("id")
            if not aid:
                continue
            docs.append({
                "id": make_doc_id(host, "assignment", aid),
                "type": "assignment",
                "title": a.get("name") or f"Assignment {aid}",
                "content": strip_html(a.get("intro", "")),
                "metadata": {
                    "course_id": course_id,
                    "course_name": course_name,
                    "assignment_id": aid,
                    "course_module_id": a.get("cmid"),
                    "due_date": _iso(a.get("duedate")),
                    "cutoff_date": _iso(a.get("cutoffdate")),
                    "opens_at": _iso(a.get("allowsubmissionsfromdate")),
                    "max_grade": a.get("grade"),
                    "max_attempts": a.get("maxattempts"),
                    "team_submission": bool(a.get("teamsubmission", False)),
                },
            })
    return docs


def submissions_to_docs(assignments: list[dict], host: str) -> list[Document]:
    """One document per submission. Content is the onlinetext (if any);
    attached files are listed in metadata.attachments."""
    docs: list[Document] = []
    for a in assignments:
        assignment_id = a.get("assignmentid")
        for s in a.get("submissions", []) or []:
            sid = s.get("id")
            if not sid:
                continue
            text_parts: list[str] = []
            attachments: list[str] = []
            for plugin in s.get("plugins", []) or []:
                ptype = plugin.get("type")
                if ptype == "onlinetext":
                    for ef in plugin.get("editorfields", []) or []:
                        cleaned = strip_html(ef.get("text", ""))
                        if cleaned:
                            text_parts.append(cleaned)
                elif ptype == "file":
                    for fa in plugin.get("fileareas", []) or []:
                        for f in fa.get("files", []) or []:
                            if f.get("filename"):
                                attachments.append(f["filename"])
            docs.append({
                "id": make_doc_id(host, "submission", sid),
                "type": "submission",
                "title": f"Submission by user {s.get('userid')} for assignment {assignment_id}",
                "content": "\n\n".join(text_parts),
                "metadata": {
                    "assignment_id": assignment_id,
                    "submission_id": sid,
                    "user_id": s.get("userid"),
                    "group_id": s.get("groupid"),
                    "status": s.get("status"),
                    "submitted_at": _iso(s.get("timemodified")),
                    "created_at": _iso(s.get("timecreated")),
                    "attempt_number": s.get("attemptnumber"),
                    "attachments": attachments,
                },
            })
    return docs


def forums_to_docs(forums: list[dict], host: str) -> list[Document]:
    docs: list[Document] = []
    for f in forums:
        fid = f.get("id")
        if not fid:
            continue
        docs.append({
            "id": make_doc_id(host, "forum", fid),
            "type": "forum",
            "title": f.get("name") or f"Forum {fid}",
            "content": strip_html(f.get("intro", "")),
            "metadata": {
                "forum_id": fid,
                "course_id": f.get("course"),
                "forum_type": f.get("type"),
                "course_module_id": f.get("cmid"),
                "num_discussions": f.get("numdiscussions"),
            },
        })
    return docs


def discussions_to_docs(discussions: list[dict], forum_id: int, host: str) -> list[Document]:
    docs: list[Document] = []
    for d in discussions:
        # The discussion thread starter — Moodle exposes `discussion` (thread id)
        # and `id` (first post id). We document the thread-starter post.
        post_id = d.get("id")
        thread_id = d.get("discussion")
        if not post_id:
            continue
        docs.append({
            "id": make_doc_id(host, "forum_post", post_id),
            "type": "forum_post",
            "title": d.get("name") or d.get("subject") or f"Post {post_id}",
            "content": strip_html(d.get("message", "")),
            "metadata": {
                "post_id": post_id,
                "discussion_id": thread_id,
                "forum_id": forum_id,
                "course_id": d.get("course"),
                "is_thread_starter": True,
                "author_id": d.get("userid"),
                "author_name": d.get("userfullname"),
                "created_at": _iso(d.get("created")),
                "modified_at": _iso(d.get("timemodified")),
                "num_replies": d.get("numreplies"),
                "pinned": bool(d.get("pinned", False)),
            },
        })
    return docs


def posts_to_docs(posts: list[dict], discussion_id: int, host: str) -> list[Document]:
    docs: list[Document] = []
    for p in posts:
        pid = p.get("id")
        if not pid:
            continue
        author = p.get("author") or {}
        docs.append({
            "id": make_doc_id(host, "forum_post", pid),
            "type": "forum_post",
            "title": p.get("subject") or f"Post {pid}",
            "content": strip_html(p.get("message", "")),
            "metadata": {
                "post_id": pid,
                "discussion_id": discussion_id,
                "parent_post_id": p.get("parentid"),
                "is_thread_starter": p.get("parentid") in (0, None),
                "author_id": author.get("id") or p.get("userid"),
                "author_name": author.get("fullname"),
                "created_at": _iso(p.get("timecreated")),
                "modified_at": _iso(p.get("timemodified")),
            },
        })
    return docs


def categories_to_docs(categories: list[dict], host: str) -> list[Document]:
    docs: list[Document] = []
    for c in categories:
        cid = c.get("id")
        if not cid:
            continue
        docs.append({
            "id": make_doc_id(host, "category", cid),
            "type": "category",
            "title": c.get("name") or f"Category {cid}",
            "content": strip_html(c.get("description", "")),
            "metadata": {
                "category_id": cid,
                "parent_id": c.get("parent"),
                "course_count": c.get("coursecount"),
                "depth": c.get("depth"),
                "path": c.get("path"),
                "idnumber": c.get("idnumber"),
                "visible": bool(c.get("visible", True)),
            },
        })
    return docs


def files_to_docs(files: list[dict], host: str) -> list[Document]:
    """One document per file. Content is empty — file bytes are fetched separately
    via `moodle_fetch_file_bytes`. Use fileurl from metadata as the doc identifier
    fallback when no numeric id is available."""
    docs: list[Document] = []
    for f in files:
        if f.get("isdir"):
            continue
        url = f.get("fileurl") or ""
        filename = f.get("filename") or ""
        if not url and not filename:
            continue
        # Files don't have a single numeric id; use a stable hash of the fileurl
        # so the same file across syncs gets the same doc id.
        ident = url or filename
        docs.append({
            "id": make_doc_id(host, "file", ident),
            "type": "file",
            "title": filename or url,
            "content": "",
            "metadata": {
                "filename": filename,
                "filepath": f.get("filepath"),
                "filesize": f.get("filesize"),
                "mimetype": f.get("mimetype"),
                "fileurl": url,
                "component": f.get("component"),
                "filearea": f.get("filearea"),
                "context_id": f.get("contextid"),
                "item_id": f.get("itemid"),
                "modified_at": _iso(f.get("timemodified")),
                "created_at": _iso(f.get("timecreated")),
            },
        })
    return docs


def calendar_events_to_docs(events: list[dict], host: str) -> list[Document]:
    docs: list[Document] = []
    for e in events:
        eid = e.get("id")
        if not eid:
            continue
        docs.append({
            "id": make_doc_id(host, "calendar_event", eid),
            "type": "calendar_event",
            "title": e.get("name") or f"Event {eid}",
            "content": strip_html(e.get("description", "")),
            "metadata": {
                "event_id": eid,
                "event_type": e.get("eventtype"),
                "course_id": e.get("courseid"),
                "group_id": e.get("groupid"),
                "user_id": e.get("userid"),
                "module_name": e.get("modulename"),
                "instance_id": e.get("instance"),
                "starts_at": _iso(e.get("timestart")),
                "duration_seconds": e.get("timeduration"),
                "created_at": _iso(e.get("timemodified")),
            },
        })
    return docs


# ---------------------------------------------------------------------------
# Sync envelope
# ---------------------------------------------------------------------------


def build_rag_response(documents: list[Document]) -> str:
    """Wrap a list of Documents in the standard RAG response envelope.

    The `sync.latest_modified_at` field is the max modification timestamp
    across the batch — consumers persist it and pass it back as
    `time_modified_since` to fetch only new/updated entities next time.

    Returns JSON string (Moodle MCP tools always return strings).
    """
    latest: str | None = None
    for d in documents:
        ts = d["metadata"].get("modified_at") or d["metadata"].get("created_at")
        if ts and (latest is None or ts > latest):
            latest = ts

    payload = {
        "documents": documents,
        "count": len(documents),
        "sync": {"latest_modified_at": latest},
    }
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)
