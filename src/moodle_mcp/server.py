"""Moodle MCP server.

Exposes read-only tools to extract data from a Moodle instance via its
Web Services REST API. All tools are designed for downstream LLM use:
either dumped directly into model context (markdown format, default) or
ingested into a RAG pipeline (json format).

Configuration is via environment variables:
    MOODLE_URL    Base URL of the Moodle site (e.g. https://moodle.unibo.it)
    MOODLE_TOKEN  Web Services token (see /user/managetoken.php)

The token's external service must have the corresponding `core_*`, `mod_*`,
and `core_calendar_*` functions enabled. See README for the full list.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

from .client import MoodleClient, format_error
from .formatting import (
    ResponseFormat,
    as_response,
    bullet_list,
    fmt_timestamp,
    md_pagination_footer,
    paginate,
    safe_get,
    strip_html,
    truncate,
)
from .rag import (
    _host,
    assignments_to_docs,
    build_rag_response,
    calendar_events_to_docs,
    course_contents_to_docs,
    courses_to_docs,
    discussions_to_docs,
    forums_to_docs,
    posts_to_docs,
    submissions_to_docs,
)

mcp = FastMCP("moodle_mcp")

# Singleton client. Lazy so import-time doesn't require env vars (helps tests).
_client: MoodleClient | None = None


def _get_client() -> MoodleClient:
    global _client
    if _client is None:
        _client = MoodleClient()
    return _client


def _site_host() -> str:
    """Stable hostname slug for use in document URIs."""
    return _host(_get_client().base_url)


# ---------------------------------------------------------------------------
# Common input bases
# ---------------------------------------------------------------------------


class _BaseInput(BaseModel):
    model_config = ConfigDict(
        str_strip_whitespace=True,
        validate_assignment=True,
        extra="forbid",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format. 'markdown' is concise and human/LLM-readable; 'json' returns full structured payload for RAG ingestion.",
    )


class _PaginatedInput(_BaseInput):
    limit: int = Field(
        default=25,
        ge=1,
        le=200,
        description="Max items to return (1-200). Default 25.",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Items to skip for pagination. Use next_offset from previous response.",
    )


# ===========================================================================
# COURSES
# ===========================================================================


class ListCoursesInput(_PaginatedInput):
    search: Optional[str] = Field(
        default=None,
        description="Optional case-insensitive substring filter on course fullname/shortname.",
        max_length=200,
    )


def _render_courses_md(courses: list[dict], pagination: dict | None) -> str:
    if not courses:
        return "_No courses found._"
    lines = ["# Courses\n"]
    for c in courses:
        lines.append(f"## {c.get('fullname', '(no name)')} (id={c.get('id')})")
        meta = []
        if c.get("shortname"):
            meta.append(f"shortname: `{c['shortname']}`")
        if c.get("categoryname"):
            meta.append(f"category: {c['categoryname']}")
        if c.get("startdate"):
            meta.append(f"starts: {fmt_timestamp(c['startdate'])}")
        if c.get("enddate"):
            meta.append(f"ends: {fmt_timestamp(c['enddate'])}")
        if meta:
            lines.append(" • ".join(meta))
        summary = strip_html(c.get("summary", ""))
        if summary:
            lines.append(f"\n{truncate(summary, 400)}")
        lines.append("")
    return "\n".join(lines) + md_pagination_footer(pagination)


@mcp.tool(
    name="moodle_list_courses",
    annotations={
        "title": "List Moodle Courses",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def moodle_list_courses(params: ListCoursesInput) -> str:
    """List all courses on the Moodle site, optionally filtered by a search substring.

    When `search` is provided, calls `core_course_search_courses` (server-side,
    paginated). Otherwise calls `core_course_get_courses` and paginates client-side.
    Use this to discover course IDs before fetching contents, assignments, forums,
    or enrolled users.

    Returns:
        Markdown summary or JSON payload of courses with id, fullname, shortname,
        category, start/end dates, and summary.
    """
    try:
        if params.search:
            page_num = params.offset // params.limit
            data = await _get_client().call(
                "core_course_search_courses",
                {
                    "criterianame": "search",
                    "criteriavalue": params.search,
                    "page": page_num,
                    "perpage": params.limit,
                },
            )
            courses: list[dict] = (data or {}).get("courses", []) if isinstance(data, dict) else []
            total = (data or {}).get("total", len(courses)) if isinstance(data, dict) else len(courses)
            has_more = (params.offset + len(courses)) < total
            pag = {
                "total": total,
                "count": len(courses),
                "offset": params.offset,
                "has_more": has_more,
                "next_offset": params.offset + len(courses) if has_more else None,
            }
            if params.response_format == ResponseFormat.RAG:
                return build_rag_response(courses_to_docs(courses, _site_host()))
            return as_response(courses, params.response_format, _render_courses_md, pag)

        data = await _get_client().call("core_course_get_courses")
        courses = data or []
        page, pag = paginate(courses, params.offset, params.limit)

        if params.response_format == ResponseFormat.RAG:
            return build_rag_response(courses_to_docs(page, _site_host()))
        return as_response(page, params.response_format, _render_courses_md, pag)
    except Exception as e:
        return format_error(e)


class GetCourseContentsInput(_BaseInput):
    course_id: int = Field(
        ...,
        ge=1,
        description="Course ID. Obtain from `moodle_list_courses` or `moodle_get_user_courses`.",
    )
    include_module_descriptions: bool = Field(
        default=True,
        description="If true, include each activity's description text (stripped of HTML). Set false to reduce context.",
    )


def _render_course_contents_md(sections: list[dict], pagination: dict | None) -> str:
    if not sections:
        return "_No sections found._"
    lines = ["# Course contents\n"]
    for s in sections:
        lines.append(f"## Section {s.get('section', '?')}: {s.get('name', '(unnamed)')}")
        summary = strip_html(s.get("summary", ""))
        if summary:
            lines.append(truncate(summary, 300))
        modules = s.get("modules", []) or []
        if not modules:
            lines.append("_(no activities)_")
        for m in modules:
            mod_line = f"- **[{m.get('modname', '?')}]** {m.get('name', '?')} (id={m.get('id')})"
            if m.get("url"):
                mod_line += f" — {m['url']}"
            lines.append(mod_line)
            desc = strip_html(m.get("description", ""))
            if desc:
                lines.append(f"  > {truncate(desc, 250)}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool(
    name="moodle_get_course_contents",
    annotations={
        "title": "Get Course Sections & Activities",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def moodle_get_course_contents(params: GetCourseContentsInput) -> str:
    """Get the full structure of a course: sections, modules (activities/resources), and descriptions.

    Calls `core_course_get_contents`. This is the primary entry point for
    indexing course material into a RAG store — each module typically maps
    to one document chunk. Module types include `assign`, `forum`, `quiz`,
    `resource` (file), `url`, `page`, `book`, etc.

    Returns:
        Markdown outline (default) or full JSON of sections with nested modules.
    """
    try:
        data = await _get_client().call(
            "core_course_get_contents",
            {"courseid": params.course_id},
        )
        sections = data or []

        if not params.include_module_descriptions:
            for s in sections:
                for m in s.get("modules", []) or []:
                    m.pop("description", None)

        if params.response_format == ResponseFormat.RAG:
            return build_rag_response(
                course_contents_to_docs(sections, params.course_id, _site_host())
            )
        return as_response(sections, params.response_format, _render_course_contents_md)
    except Exception as e:
        return format_error(e)


class GetUserCoursesInput(_BaseInput):
    user_id: int = Field(..., ge=1, description="User ID whose enrolled courses to list.")


def _render_user_courses_md(courses: list[dict], pagination: dict | None) -> str:
    if not courses:
        return "_User is not enrolled in any courses._"
    lines = ["# Enrolled courses\n"]
    for c in courses:
        progress = c.get("progress")
        prog_str = f" • progress: {progress}%" if progress is not None else ""
        lines.append(
            f"- **{c.get('fullname')}** (id={c.get('id')}, shortname=`{c.get('shortname')}`){prog_str}"
        )
    return "\n".join(lines)


@mcp.tool(
    name="moodle_get_user_courses",
    annotations={
        "title": "Get Courses for a User",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def moodle_get_user_courses(params: GetUserCoursesInput) -> str:
    """List courses a specific user is enrolled in.

    Calls `core_enrol_get_users_courses`. Useful for building student-centric
    context: given a learner, what courses are they following?

    Returns:
        Markdown list (default) or JSON with each course's id, names, progress.
    """
    if params.response_format == ResponseFormat.RAG:
        return (
            "Error: 'rag' format is not applicable to user records (PII). "
            "Use 'json' to get structured data for resolution, or call "
            "moodle_get_course_contents/moodle_get_forums in 'rag' mode to "
            "build the index, then resolve user IDs at query time."
        )
    try:
        data = await _get_client().call(
            "core_enrol_get_users_courses",
            {"userid": params.user_id},
        )
        return as_response(data or [], params.response_format, _render_user_courses_md)
    except Exception as e:
        return format_error(e)


# ===========================================================================
# USERS
# ===========================================================================


class SearchUsersInput(_PaginatedInput):
    field: str = Field(
        default="email",
        description="Field to search by. One of: id, idnumber, username, email, auth.",
        pattern=r"^(id|idnumber|username|email|auth)$",
    )
    values: list[str] = Field(
        ...,
        description="One or more values to match (exact match on the chosen field).",
        min_length=1,
        max_length=50,
    )


def _render_users_md(users: list[dict], pagination: dict | None) -> str:
    if not users:
        return "_No users found._"
    lines = ["# Users\n"]
    for u in users:
        name = u.get("fullname") or f"{u.get('firstname', '')} {u.get('lastname', '')}".strip() or "(unnamed)"
        lines.append(
            f"- **{name}** (id={u.get('id')}, username=`{u.get('username')}`, email={u.get('email', '—')})"
        )
        if u.get("city") or u.get("country"):
            lines.append(f"  📍 {u.get('city', '')} {u.get('country', '')}".rstrip())
    return "\n".join(lines) + md_pagination_footer(pagination)


@mcp.tool(
    name="moodle_get_users_by_field",
    annotations={
        "title": "Find Users by Field",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def moodle_get_users_by_field(params: SearchUsersInput) -> str:
    """Look up users by exact match on a single field (e.g. email, username, id).

    Calls `core_user_get_users_by_field`. The most reliable way to resolve a
    user from external context (e.g. an email address mentioned by the LLM)
    to a Moodle user record with id.

    Returns:
        Markdown list (default) or JSON with each user's id, names, email, etc.
    """
    if params.response_format == ResponseFormat.RAG:
        return (
            "Error: 'rag' format is not applicable to user records (PII). "
            "Use 'json' to resolve users to IDs, then index content tools "
            "(forums, course contents) in 'rag' mode."
        )
    try:
        data = await _get_client().call(
            "core_user_get_users_by_field",
            {"field": params.field, "values": params.values},
        )
        users: list[dict] = data or []
        page, pag = paginate(users, params.offset, params.limit)
        return as_response(page, params.response_format, _render_users_md, pag)
    except Exception as e:
        return format_error(e)


_USER_CRITERIA_KEYS = {"id", "username", "email", "firstname", "lastname", "idnumber"}


class UserCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    key: str = Field(..., description="One of: id, username, email, firstname, lastname, idnumber.")
    value: str = Field(..., min_length=1, description="Value to match for this key.")


class SearchUsersByCriteriaInput(_PaginatedInput):
    criteria: list[UserCriterion] = Field(
        ...,
        description=(
            "List of {key, value} filters. Allowed keys: id, username, email, "
            "firstname, lastname, idnumber. Multiple criteria are ANDed."
        ),
        min_length=1,
        max_length=20,
    )


@mcp.tool(
    name="moodle_search_users",
    annotations={
        "title": "Search Users by Multiple Criteria",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def moodle_search_users(params: SearchUsersByCriteriaInput) -> str:
    """Search for users using one or more criteria (ANDed together).

    Calls `core_user_get_users`. Unlike `moodle_get_users_by_field` (single field,
    multiple values), this matches across multiple fields simultaneously — e.g.
    firstname=Anna AND lastname=Rossi. Wildcards (`%`) are allowed in values per
    Moodle convention.

    Returns:
        Markdown list (default) or JSON with each user's id, names, email.
        Refuses 'rag' format (PII).
    """
    if params.response_format == ResponseFormat.RAG:
        return (
            "Error: 'rag' format is not applicable to user records (PII). "
            "Use 'json' to resolve users to IDs, then index content tools "
            "in 'rag' mode."
        )
    bad = [c.key for c in params.criteria if c.key not in _USER_CRITERIA_KEYS]
    if bad:
        return (
            f"Invalid criteria key(s): {', '.join(bad)}. "
            f"Allowed: {', '.join(sorted(_USER_CRITERIA_KEYS))}."
        )
    try:
        data = await _get_client().call(
            "core_user_get_users",
            {"criteria": [{"key": c.key, "value": c.value} for c in params.criteria]},
        )
        users: list[dict] = data.get("users", []) if isinstance(data, dict) else []
        page, pag = paginate(users, params.offset, params.limit)
        return as_response(page, params.response_format, _render_users_md, pag)
    except Exception as e:
        return format_error(e)


class GetEnrolledUsersInput(_PaginatedInput):
    course_id: int = Field(..., ge=1, description="Course ID.")


@mcp.tool(
    name="moodle_get_enrolled_users",
    annotations={
        "title": "List Users Enrolled in a Course",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def moodle_get_enrolled_users(params: GetEnrolledUsersInput) -> str:
    """List all users enrolled in a course, with their role assignments.

    Calls `core_enrol_get_enrolled_users`. Useful for participation analysis,
    grading-cohort export, or building a class roster for RAG context.

    Returns:
        Markdown list (default) or JSON with each user's id, fullname, email,
        and assigned roles within the course.
    """
    if params.response_format == ResponseFormat.RAG:
        return (
            "Error: 'rag' format is not applicable to user records (PII). "
            "Use 'json' to get the roster for resolution purposes."
        )
    try:
        data = await _get_client().call(
            "core_enrol_get_enrolled_users",
            {"courseid": params.course_id},
        )
        users: list[dict] = data or []
        page, pag = paginate(users, params.offset, params.limit)

        def render(items, pagination):
            if not items:
                return "_No enrolled users._"
            lines = [f"# Enrolled users (course {params.course_id})\n"]
            for u in items:
                roles = ", ".join(r.get("shortname", "?") for r in u.get("roles", []) or [])
                lines.append(
                    f"- **{u.get('fullname')}** (id={u.get('id')}, email={u.get('email', '—')})"
                    + (f" — roles: {roles}" if roles else "")
                )
            return "\n".join(lines) + md_pagination_footer(pagination)

        return as_response(page, params.response_format, render, pag)
    except Exception as e:
        return format_error(e)


# ===========================================================================
# ASSIGNMENTS
# ===========================================================================


class ListAssignmentsInput(_BaseInput):
    course_ids: list[int] = Field(
        ...,
        description="One or more course IDs to fetch assignments for.",
        min_length=1,
        max_length=50,
    )


def _render_assignments_md(courses: list[dict], pagination: dict | None) -> str:
    if not courses:
        return "_No assignments found._"
    lines = ["# Assignments\n"]
    for c in courses:
        lines.append(f"## Course: {c.get('fullname', '?')} (id={c.get('id')})")
        assigns = c.get("assignments", []) or []
        if not assigns:
            lines.append("_(no assignments)_\n")
            continue
        for a in assigns:
            lines.append(f"### {a.get('name')} (id={a.get('id')}, cmid={a.get('cmid')})")
            meta = []
            if a.get("duedate"):
                meta.append(f"due: {fmt_timestamp(a['duedate'])}")
            if a.get("allowsubmissionsfromdate"):
                meta.append(f"opens: {fmt_timestamp(a['allowsubmissionsfromdate'])}")
            if a.get("cutoffdate"):
                meta.append(f"cutoff: {fmt_timestamp(a['cutoffdate'])}")
            if a.get("grade") is not None:
                meta.append(f"max grade: {a['grade']}")
            if meta:
                lines.append(" • ".join(meta))
            intro = strip_html(a.get("intro", ""))
            if intro:
                lines.append(f"\n{truncate(intro, 400)}")
            lines.append("")
    return "\n".join(lines)


@mcp.tool(
    name="moodle_get_assignments",
    annotations={
        "title": "Get Assignments for Courses",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def moodle_get_assignments(params: ListAssignmentsInput) -> str:
    """Get all assignments for one or more courses, with instructions and due dates.

    Calls `mod_assign_get_assignments`. Returns nested structure: courses
    containing assignment lists. Each assignment includes the instructions
    (intro), due/cutoff dates, max grade, and assignment id (for fetching
    submissions).

    Returns:
        Markdown breakdown (default) or JSON.
    """
    try:
        data = await _get_client().call(
            "mod_assign_get_assignments",
            {"courseids": params.course_ids},
        )
        courses = data.get("courses", []) if isinstance(data, dict) else []
        if params.response_format == ResponseFormat.RAG:
            return build_rag_response(assignments_to_docs(courses, _site_host()))
        return as_response(courses, params.response_format, _render_assignments_md)
    except Exception as e:
        return format_error(e)


class GetSubmissionsInput(_PaginatedInput):
    assignment_ids: list[int] = Field(
        ...,
        description="One or more assignment IDs (from `moodle_get_assignments`).",
        min_length=1,
        max_length=20,
    )
    status: Optional[str] = Field(
        default=None,
        description="Optional filter: 'submitted', 'draft', 'new', 'reopened'.",
        pattern=r"^(submitted|draft|new|reopened)$",
    )


def _render_submissions_md(assignments: list[dict], pagination: dict | None) -> str:
    if not assignments:
        return "_No submissions found._"
    lines = ["# Submissions\n"]
    for a in assignments:
        lines.append(f"## Assignment id={a.get('assignmentid')}")
        subs = a.get("submissions", []) or []
        if not subs:
            lines.append("_(no submissions)_\n")
            continue
        for s in subs:
            lines.append(
                f"- user={s.get('userid')} • status=`{s.get('status')}` • "
                f"submitted: {fmt_timestamp(s.get('timemodified'))}"
            )
            # Submission plugins contain the actual text/files
            for plugin in s.get("plugins", []) or []:
                if plugin.get("type") == "onlinetext":
                    for ef in plugin.get("editorfields", []) or []:
                        text = strip_html(ef.get("text", ""))
                        if text:
                            lines.append(f"  > {truncate(text, 300)}")
                elif plugin.get("type") == "file":
                    files = []
                    for fa in plugin.get("fileareas", []) or []:
                        for f in fa.get("files", []) or []:
                            files.append(f.get("filename", "?"))
                    if files:
                        lines.append(f"  📎 {', '.join(files)}")
        lines.append("")
    return "\n".join(lines) + md_pagination_footer(pagination)


@mcp.tool(
    name="moodle_get_submissions",
    annotations={
        "title": "Get Submissions for Assignments",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def moodle_get_submissions(params: GetSubmissionsInput) -> str:
    """Get student submissions for one or more assignments, including text content and file names.

    Calls `mod_assign_get_submissions`. The token's user must have grading
    permissions in the course to see submissions other than their own.

    Submission `plugins` array contains the actual content: `onlinetext`
    plugin has `editorfields[].text` (HTML), `file` plugin lists attached
    file names.

    Returns:
        Markdown listing per assignment (default) or full JSON.
    """
    try:
        params_dict: dict[str, Any] = {"assignmentids": params.assignment_ids}
        if params.status:
            params_dict["status"] = params.status
        data = await _get_client().call("mod_assign_get_submissions", params_dict)
        assignments = data.get("assignments", []) if isinstance(data, dict) else []
        # Client-side pagination is awkward here (nested), so paginate top-level.
        page, pag = paginate(assignments, params.offset, params.limit)
        if params.response_format == ResponseFormat.RAG:
            return build_rag_response(submissions_to_docs(page, _site_host()))
        return as_response(page, params.response_format, _render_submissions_md, pag)
    except Exception as e:
        return format_error(e)


# ===========================================================================
# FORUMS
# ===========================================================================


class ListForumsInput(_BaseInput):
    course_ids: list[int] = Field(
        ...,
        description="One or more course IDs.",
        min_length=1,
        max_length=50,
    )


def _render_forums_md(forums: list[dict], pagination: dict | None) -> str:
    if not forums:
        return "_No forums found._"
    lines = ["# Forums\n"]
    for f in forums:
        lines.append(f"## {f.get('name')} (id={f.get('id')}, course={f.get('course')})")
        meta = []
        if f.get("type"):
            meta.append(f"type: {f['type']}")
        if f.get("numdiscussions") is not None:
            meta.append(f"discussions: {f['numdiscussions']}")
        if meta:
            lines.append(" • ".join(meta))
        intro = strip_html(f.get("intro", ""))
        if intro:
            lines.append(f"\n{truncate(intro, 300)}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool(
    name="moodle_get_forums",
    annotations={
        "title": "List Forums in Courses",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def moodle_get_forums(params: ListForumsInput) -> str:
    """List all forums across one or more courses.

    Calls `mod_forum_get_forums_by_courses`. Use forum id with
    `moodle_get_forum_discussions` to drill down.

    Returns:
        Markdown list (default) or JSON.
    """
    try:
        data = await _get_client().call(
            "mod_forum_get_forums_by_courses",
            {"courseids": params.course_ids},
        )
        forums = data or []
        if params.response_format == ResponseFormat.RAG:
            return build_rag_response(forums_to_docs(forums, _site_host()))
        return as_response(forums, params.response_format, _render_forums_md)
    except Exception as e:
        return format_error(e)


class GetForumDiscussionsInput(_PaginatedInput):
    forum_id: int = Field(..., ge=1, description="Forum ID (from `moodle_get_forums`).")
    sort_by: str = Field(
        default="timemodified",
        description="Sort field: 'timemodified', 'created', 'name'.",
        pattern=r"^(timemodified|created|name)$",
    )
    sort_direction: str = Field(
        default="DESC",
        description="'ASC' or 'DESC'.",
        pattern=r"^(ASC|DESC)$",
    )
    time_modified_since: Optional[int] = Field(
        default=None,
        ge=0,
        description=(
            "Unix timestamp (seconds). Only return discussions modified at or after this time. "
            "Use for incremental RAG sync: pass back `sync.latest_modified_at` from the previous response "
            "(converted to epoch seconds). Forces sort_by='timemodified' and sort_direction='DESC' for "
            "efficient early-stop."
        ),
    )


def _render_discussions_md(discussions: list[dict], pagination: dict | None) -> str:
    if not discussions:
        return "_No discussions._"
    lines = ["# Forum discussions\n"]
    for d in discussions:
        lines.append(f"## {d.get('name')} (discussion={d.get('discussion')})")
        lines.append(
            f"by **{d.get('userfullname')}** • created {fmt_timestamp(d.get('created'))} "
            f"• last reply {fmt_timestamp(d.get('timemodified'))} • {d.get('numreplies', 0)} replies"
        )
        msg = strip_html(d.get("message", ""))
        if msg:
            lines.append(f"\n{truncate(msg, 400)}")
        lines.append("")
    return "\n".join(lines) + md_pagination_footer(pagination)


@mcp.tool(
    name="moodle_get_forum_discussions",
    annotations={
        "title": "Get Discussions in a Forum",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def moodle_get_forum_discussions(params: GetForumDiscussionsInput) -> str:
    """Get top-level discussion threads in a forum.

    Calls `mod_forum_get_forum_discussions`. Use `discussion` id with
    `moodle_get_discussion_posts` to fetch the full thread.

    Returns:
        Markdown list with thread starter messages (default) or JSON.
    """
    try:
        # When filtering incrementally, force timemodified DESC so we can early-stop.
        if params.time_modified_since is not None:
            sort_by, sort_dir = "timemodified", "DESC"
        else:
            sort_by, sort_dir = params.sort_by, params.sort_direction

        data = await _get_client().call(
            "mod_forum_get_forum_discussions",
            {
                "forumid": params.forum_id,
                "sortby": sort_by,
                "sortdirection": sort_dir,
                "page": params.offset // params.limit,
                "perpage": params.limit,
            },
        )
        discussions = data.get("discussions", []) if isinstance(data, dict) else []

        # Apply incremental filter. Because results are sorted DESC by timemodified,
        # once we hit an older item we can stop — anything after is also older.
        if params.time_modified_since is not None:
            filtered: list[dict] = []
            for d in discussions:
                if (d.get("timemodified") or 0) >= params.time_modified_since:
                    filtered.append(d)
                else:
                    break
            discussions = filtered

        pag = {
            "total": safe_get(data, "totaldiscussions", default=len(discussions)),
            "count": len(discussions),
            "offset": params.offset,
            "has_more": len(discussions) == params.limit,
            "next_offset": params.offset + len(discussions) if len(discussions) == params.limit else None,
        }
        if params.response_format == ResponseFormat.RAG:
            return build_rag_response(
                discussions_to_docs(discussions, params.forum_id, _site_host())
            )
        return as_response(discussions, params.response_format, _render_discussions_md, pag)
    except Exception as e:
        return format_error(e)


class GetDiscussionPostsInput(_BaseInput):
    discussion_id: int = Field(..., ge=1, description="Discussion ID.")


def _render_posts_md(posts: list[dict], pagination: dict | None) -> str:
    if not posts:
        return "_No posts._"
    lines = ["# Forum thread\n"]
    # Build a quick index for parent lookup if rendering hierarchically
    for p in posts:
        author = safe_get(p, "author", "fullname", default="?")
        lines.append(
            f"## {p.get('subject', '(no subject)')} — {author}, "
            f"{fmt_timestamp(p.get('timecreated'))} (post id={p.get('id')})"
        )
        msg = strip_html(p.get("message", ""))
        if msg:
            lines.append(msg)
        lines.append("")
    return "\n".join(lines)


@mcp.tool(
    name="moodle_get_discussion_posts",
    annotations={
        "title": "Get Posts in a Forum Discussion",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def moodle_get_discussion_posts(params: GetDiscussionPostsInput) -> str:
    """Get all posts (thread starter + replies) within a forum discussion.

    Calls `mod_forum_get_discussion_posts`. Best primitive for ingesting
    discussion threads into a RAG store: one document per post, with parent
    pointers preserved in JSON mode.

    Returns:
        Markdown rendering of the thread (default) or full JSON post list.
    """
    try:
        data = await _get_client().call(
            "mod_forum_get_discussion_posts",
            {"discussionid": params.discussion_id},
        )
        posts = data.get("posts", []) if isinstance(data, dict) else []
        if params.response_format == ResponseFormat.RAG:
            return build_rag_response(
                posts_to_docs(posts, params.discussion_id, _site_host())
            )
        return as_response(posts, params.response_format, _render_posts_md)
    except Exception as e:
        return format_error(e)


# ===========================================================================
# CALENDAR
# ===========================================================================


class GetCalendarEventsInput(_PaginatedInput):
    course_ids: Optional[list[int]] = Field(
        default=None,
        description="Optional list of course IDs to include course-level events.",
        max_length=50,
    )
    group_ids: Optional[list[int]] = Field(
        default=None,
        description="Optional list of group IDs to include group-level events.",
        max_length=50,
    )
    time_start: Optional[int] = Field(
        default=None,
        ge=0,
        description="Unix timestamp (seconds). Lower bound for event timestart.",
    )
    time_end: Optional[int] = Field(
        default=None,
        ge=0,
        description="Unix timestamp (seconds). Upper bound for event timestart.",
    )


def _render_events_md(events: list[dict], pagination: dict | None) -> str:
    if not events:
        return "_No events._"
    lines = ["# Calendar events\n"]
    for e in events:
        lines.append(
            f"- **{e.get('name')}** (id={e.get('id')}) — {fmt_timestamp(e.get('timestart'))}"
        )
        if e.get("courseid"):
            lines.append(f"  course={e['courseid']}")
        desc = strip_html(e.get("description", ""))
        if desc:
            lines.append(f"  {truncate(desc, 200)}")
    return "\n".join(lines) + md_pagination_footer(pagination)


@mcp.tool(
    name="moodle_get_calendar_events",
    annotations={
        "title": "Get Calendar Events",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def moodle_get_calendar_events(params: GetCalendarEventsInput) -> str:
    """Get calendar events filtered by courses, groups, and/or time window.

    Calls `core_calendar_get_calendar_events`. Pass `time_start`/`time_end` as
    Unix timestamps in seconds. The token's user sees their own user-level
    events plus events for courses/groups specified.

    Returns:
        Markdown list (default) or JSON with each event's id, name, timestart,
        course/group/user association, and description.
    """
    try:
        events_filter: dict[str, Any] = {}
        if params.course_ids:
            events_filter["courseids"] = params.course_ids
        if params.group_ids:
            events_filter["groupids"] = params.group_ids

        options: dict[str, Any] = {"userevents": 1, "siteevents": 1}
        if params.time_start is not None:
            options["timestart"] = params.time_start
        if params.time_end is not None:
            options["timeend"] = params.time_end

        data = await _get_client().call(
            "core_calendar_get_calendar_events",
            {"events": events_filter, "options": options},
        )
        events = data.get("events", []) if isinstance(data, dict) else []
        page, pag = paginate(events, params.offset, params.limit)
        if params.response_format == ResponseFormat.RAG:
            return build_rag_response(calendar_events_to_docs(page, _site_host()))
        return as_response(page, params.response_format, _render_events_md, pag)
    except Exception as e:
        return format_error(e)


class GetUpcomingEventsInput(_PaginatedInput):
    time_sort_from: Optional[int] = Field(
        default=None,
        ge=0,
        description="Unix timestamp (seconds). Lower bound — defaults to 'now' server-side.",
    )
    time_sort_to: Optional[int] = Field(
        default=None,
        ge=0,
        description="Unix timestamp (seconds). Upper bound for event timesort.",
    )


@mcp.tool(
    name="moodle_get_upcoming_events",
    annotations={
        "title": "Get Upcoming Action Events",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def moodle_get_upcoming_events(params: GetUpcomingEventsInput) -> str:
    """Get upcoming action events for the authenticated user, sorted by `timesort`.

    Calls `core_calendar_get_action_events_by_timesort`. Action events are
    things requiring user attention (assignment deadlines, quiz openings, etc).
    Use this for "what's next for this user" queries.

    Returns:
        Markdown list (default) or JSON.
    """
    try:
        api_params: dict[str, Any] = {
            "limitnum": params.limit,
            "aftereventid": 0,
        }
        if params.time_sort_from is not None:
            api_params["timesortfrom"] = params.time_sort_from
        if params.time_sort_to is not None:
            api_params["timesortto"] = params.time_sort_to

        data = await _get_client().call(
            "core_calendar_get_action_events_by_timesort",
            api_params,
        )
        events = data.get("events", []) if isinstance(data, dict) else []
        if params.response_format == ResponseFormat.RAG:
            return build_rag_response(calendar_events_to_docs(events, _site_host()))
        return as_response(events, params.response_format, _render_events_md)
    except Exception as e:
        return format_error(e)


# ===========================================================================
# Entry point
# ===========================================================================


def main() -> None:
    """Entry point for `moodle-mcp` console script. stdio transport."""
    mcp.run()


if __name__ == "__main__":
    main()
