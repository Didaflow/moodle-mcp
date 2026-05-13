"""Tests for moodle-mcp.

All tests use a mocked Moodle client — no live API calls. The goal is to
catch regressions in:
- Parameter flattening (Moodle's PHP-style form encoding)
- Pydantic input validation
- Response format dispatch (markdown / json / rag)
- Stable document ID generation
- Incremental sync filtering (early-stop on time_modified_since)
- Error mapping (Moodle error codes → actionable messages)
"""

from __future__ import annotations

import json
import os

import pytest

# Set fake credentials before any module-level client init can happen
os.environ.setdefault("MOODLE_URL", "https://moodle.test.it")
os.environ.setdefault("MOODLE_TOKEN", "test_token")

from moodle_mcp import server as srv
from moodle_mcp.client import MoodleAPIError, MoodleConfigError, _flatten_params, format_error
from moodle_mcp.formatting import ResponseFormat, strip_html
from moodle_mcp.rag import (
    assignments_to_docs,
    categories_to_docs,
    chat_messages_to_docs,
    chats_to_docs,
    courses_to_docs,
    discussions_to_docs,
    files_to_docs,
    make_doc_id,
    posts_to_docs,
)
from moodle_mcp.server import (
    FetchFileBytesInput,
    GetChatSessionMessagesInput,
    GetChatSessionsInput,
    GetChatsInput,
    GetCourseContentsInput,
    GetDiscussionPostsInput,
    GetForumDiscussionsInput,
    ListAssignmentsInput,
    ListCategoriesInput,
    ListCoursesInput,
    ListFilesInput,
    SearchUsersByCriteriaInput,
    SearchUsersInput,
    moodle_fetch_file_bytes,
    moodle_get_chat_session_messages,
    moodle_get_chat_sessions,
    moodle_get_chats,
    moodle_get_course_contents,
    moodle_get_discussion_posts,
    moodle_get_forum_discussions,
    moodle_get_assignments,
    moodle_get_users_by_field,
    moodle_list_categories,
    moodle_list_courses,
    moodle_list_files,
    moodle_search_users,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class FakeClient:
    """Stub MoodleClient that returns whatever we set on .response."""

    def __init__(self):
        self.base_url = "https://moodle.test.it"
        self.response = None
        self.exception = None
        self.calls: list[tuple[str, dict]] = []

    async def call(self, wsfunction, params=None):
        self.calls.append((wsfunction, params or {}))
        if self.exception is not None:
            raise self.exception
        return self.response


@pytest.fixture
def fake_client(monkeypatch):
    c = FakeClient()
    monkeypatch.setattr(srv, "_client", c)
    return c


# ---------------------------------------------------------------------------
# Parameter flattening (Moodle's PHP form encoding)
# ---------------------------------------------------------------------------


class TestFlattenParams:
    def test_simple_list(self):
        assert _flatten_params({"courseids": [1, 2, 3]}) == {
            "courseids[0]": 1,
            "courseids[1]": 2,
            "courseids[2]": 3,
        }

    def test_nested_dict(self):
        assert _flatten_params({"options": {"userid": 5}}) == {"options[userid]": 5}

    def test_list_of_dicts(self):
        result = _flatten_params({"criteria": [{"key": "id", "value": "3"}]})
        assert result == {"criteria[0][key]": "id", "criteria[0][value]": "3"}

    def test_combined_calendar_query(self):
        result = _flatten_params({
            "events": {"courseids": [10, 20]},
            "options": {"userevents": 1, "timestart": 1700000000},
        })
        assert result == {
            "events[courseids][0]": 10,
            "events[courseids][1]": 20,
            "options[userevents]": 1,
            "options[timestart]": 1700000000,
        }

    def test_none_values_dropped(self):
        assert _flatten_params({"a": 1, "b": None, "c": "x"}) == {"a": 1, "c": "x"}


# ---------------------------------------------------------------------------
# HTML stripping
# ---------------------------------------------------------------------------


class TestStripHtml:
    def test_basic_tags(self):
        assert strip_html("<p>Hello <b>world</b></p>") == "Hello world"

    def test_entities(self):
        # &amp; → "&", " " stays, &lt;tag&gt; → "<tag>", " " stays, &nbsp; → " ", "x"
        assert strip_html("&amp; &lt;tag&gt; &nbsp;x") == "& <tag>  x"

    def test_br_becomes_newline(self):
        assert "\n" in strip_html("line1<br>line2")

    def test_empty(self):
        assert strip_html(None) == ""
        assert strip_html("") == ""


# ---------------------------------------------------------------------------
# Stable document IDs
# ---------------------------------------------------------------------------


class TestDocIds:
    def test_format(self):
        assert make_doc_id("moodle.test.it", "course", 42) == "moodle://moodle.test.it/course/42"

    def test_string_id(self):
        assert make_doc_id("host", "forum_post", "abc") == "moodle://host/forum_post/abc"

    def test_deterministic(self):
        # Same inputs → same ID, every time. Critical for upsert.
        assert make_doc_id("h", "course", 1) == make_doc_id("h", "course", 1)


# ---------------------------------------------------------------------------
# RAG converters (pure functions, no I/O)
# ---------------------------------------------------------------------------


class TestRagConverters:
    def test_courses_to_docs_basic(self):
        docs = courses_to_docs(
            [{"id": 42, "fullname": "ML", "summary": "<p>Intro</p>", "shortname": "ML25"}],
            "host",
        )
        assert len(docs) == 1
        assert docs[0]["id"] == "moodle://host/course/42"
        assert docs[0]["type"] == "course"
        assert docs[0]["content"] == "Intro"  # HTML stripped
        assert docs[0]["metadata"]["course_id"] == 42

    def test_courses_to_docs_skips_missing_id(self):
        docs = courses_to_docs([{"fullname": "no id here"}], "host")
        assert docs == []

    def test_assignments_to_docs_nested(self):
        docs = assignments_to_docs(
            [{
                "id": 42, "fullname": "ML",
                "assignments": [{"id": 100, "name": "Lab", "intro": "<b>do it</b>",
                                 "duedate": 1740000000}],
            }],
            "host",
        )
        assert len(docs) == 1
        assert docs[0]["id"] == "moodle://host/assignment/100"
        assert docs[0]["metadata"]["course_id"] == 42
        assert docs[0]["metadata"]["due_date"] is not None  # converted to ISO

    def test_chats_to_docs(self):
        docs = chats_to_docs(
            [{"id": 5, "course": 42, "name": "Q&A", "intro": "<p>hi</p>",
              "coursemodule": 100, "chattime": 1700000000}],
            "host",
        )
        assert docs[0]["type"] == "chat"
        assert docs[0]["content"] == "hi"
        assert docs[0]["metadata"]["chat_id"] == 5
        assert docs[0]["metadata"]["course_id"] == 42

    def test_chat_messages_to_docs(self):
        docs = chat_messages_to_docs(
            [{"id": 11, "userid": 7, "system": False,
              "message": "<b>hi</b>", "timestamp": 1700000010}],
            chat_id=5, session_start=1700000000, host="host",
        )
        assert docs[0]["type"] == "chat_message"
        assert docs[0]["content"] == "hi"
        assert docs[0]["metadata"]["chat_id"] == 5
        assert docs[0]["metadata"]["user_id"] == 7
        # session_start should be ISO-converted
        assert docs[0]["metadata"]["session_start"] is not None

    def test_categories_to_docs_strips_html(self):
        docs = categories_to_docs(
            [{"id": 7, "name": "ML", "parent": 1, "depth": 2,
              "coursecount": 3, "path": "/1/7", "description": "<b>desc</b>"}],
            "host",
        )
        assert docs[0]["type"] == "category"
        assert docs[0]["content"] == "desc"
        assert docs[0]["metadata"]["parent_id"] == 1
        assert docs[0]["metadata"]["course_count"] == 3

    def test_files_to_docs_skips_dirs(self):
        docs = files_to_docs(
            [
                {"filename": "x.pdf", "fileurl": "https://h/wsp/x.pdf",
                 "filesize": 10, "mimetype": "application/pdf", "isdir": False,
                 "timemodified": 1700000000},
                {"filename": "sub", "isdir": True},
            ],
            "host",
        )
        assert len(docs) == 1
        assert docs[0]["type"] == "file"
        assert docs[0]["content"] == ""
        assert docs[0]["metadata"]["filename"] == "x.pdf"

    def test_posts_to_docs_parent_linking(self):
        docs = posts_to_docs(
            [
                {"id": 1, "parentid": 0, "subject": "Q", "message": "Hi",
                 "author": {"id": 11, "fullname": "Alice"}, "timecreated": 1000},
                {"id": 2, "parentid": 1, "subject": "Re", "message": "Hello",
                 "author": {"id": 12, "fullname": "Bob"}, "timecreated": 2000},
            ],
            discussion_id=99,
            host="host",
        )
        assert docs[0]["metadata"]["is_thread_starter"] is True
        assert docs[0]["metadata"]["parent_post_id"] == 0
        assert docs[1]["metadata"]["is_thread_starter"] is False
        assert docs[1]["metadata"]["parent_post_id"] == 1


# ---------------------------------------------------------------------------
# Pydantic input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_assignments_rejects_empty_course_ids(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            ListAssignmentsInput(course_ids=[])

    def test_users_rejects_invalid_field(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            SearchUsersInput(field="not_a_field", values=["x"])

    def test_courses_limit_bounds(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            ListCoursesInput(limit=500)  # max is 200

    def test_extra_fields_forbidden(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            ListCoursesInput(unknown_param="x")


# ---------------------------------------------------------------------------
# Tool dispatch — response format branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestResponseFormats:
    async def test_courses_markdown(self, fake_client):
        fake_client.response = [
            {"id": 42, "fullname": "ML", "shortname": "ML25", "summary": ""},
        ]
        out = await moodle_list_courses(ListCoursesInput())
        assert "# Courses" in out
        assert "ML" in out

    async def test_courses_json(self, fake_client):
        fake_client.response = [
            {"id": 42, "fullname": "ML", "shortname": "ML25", "summary": ""},
        ]
        out = await moodle_list_courses(ListCoursesInput(response_format=ResponseFormat.JSON))
        parsed = json.loads(out)
        assert "data" in parsed
        assert "pagination" in parsed

    async def test_courses_rag(self, fake_client):
        fake_client.response = [
            {"id": 42, "fullname": "ML", "shortname": "ML25",
             "summary": "<p>Intro</p>"},
        ]
        out = await moodle_list_courses(ListCoursesInput(response_format=ResponseFormat.RAG))
        parsed = json.loads(out)
        assert parsed["count"] == 1
        assert parsed["documents"][0]["id"] == "moodle://moodle.test.it/course/42"
        assert parsed["documents"][0]["content"] == "Intro"

    async def test_courses_search_uses_search_courses_ws(self, fake_client):
        fake_client.response = {
            "total": 42,
            "courses": [{"id": 1, "fullname": "ML", "shortname": "ML25", "summary": ""}],
        }
        out = await moodle_list_courses(ListCoursesInput(
            search="ml", limit=10, offset=20,
            response_format=ResponseFormat.JSON,
        ))
        wsfunc, params = fake_client.calls[-1]
        assert wsfunc == "core_course_search_courses"
        assert params["criterianame"] == "search"
        assert params["criteriavalue"] == "ml"
        assert params["perpage"] == 10
        assert params["page"] == 2  # offset 20 // limit 10
        parsed = json.loads(out)
        assert parsed["pagination"]["total"] == 42
        assert parsed["pagination"]["offset"] == 20
        assert parsed["pagination"]["has_more"] is True
        assert parsed["pagination"]["next_offset"] == 21

    async def test_courses_no_search_uses_get_courses_ws(self, fake_client):
        fake_client.response = [
            {"id": 1, "fullname": "ML", "shortname": "ML25", "summary": ""},
        ]
        await moodle_list_courses(ListCoursesInput())
        wsfunc, _ = fake_client.calls[-1]
        assert wsfunc == "core_course_get_courses"

    async def test_course_contents_rag_splits_section_and_modules(self, fake_client):
        fake_client.response = [{
            "id": 100, "section": 1, "name": "Week 1", "summary": "",
            "modules": [
                {"id": 555, "name": "Lab", "modname": "assign", "description": ""},
                {"id": 556, "name": "Quiz", "modname": "quiz", "description": ""},
            ],
        }]
        out = await moodle_get_course_contents(
            GetCourseContentsInput(course_id=42, response_format=ResponseFormat.RAG)
        )
        parsed = json.loads(out)
        types = [d["type"] for d in parsed["documents"]]
        assert "section" in types
        assert types.count("module") == 2


# ---------------------------------------------------------------------------
# Incremental sync — time_modified_since early-stop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestIncrementalSync:
    async def test_early_stop_on_old_item(self, fake_client):
        # Discussions sorted DESC by timemodified. Filter should stop at the
        # first item older than the threshold.
        fake_client.response = {
            "discussions": [
                {"id": 1, "discussion": 1, "name": "new", "userid": 1,
                 "userfullname": "A", "message": "", "course": 1,
                 "created": 1000, "timemodified": 2000, "numreplies": 0},
                {"id": 2, "discussion": 2, "name": "newer", "userid": 1,
                 "userfullname": "A", "message": "", "course": 1,
                 "created": 1000, "timemodified": 1500, "numreplies": 0},
                {"id": 3, "discussion": 3, "name": "old", "userid": 1,
                 "userfullname": "A", "message": "", "course": 1,
                 "created": 1000, "timemodified": 500, "numreplies": 0},
            ],
            "totaldiscussions": 3,
        }
        out = await moodle_get_forum_discussions(GetForumDiscussionsInput(
            forum_id=7,
            time_modified_since=1000,
            response_format=ResponseFormat.RAG,
        ))
        parsed = json.loads(out)
        assert parsed["count"] == 2  # third item filtered out

    async def test_since_forces_timemodified_sort(self, fake_client):
        """When time_modified_since is set, sort must be timemodified DESC for early-stop."""
        fake_client.response = {"discussions": [], "totaldiscussions": 0}
        await moodle_get_forum_discussions(GetForumDiscussionsInput(
            forum_id=7,
            time_modified_since=1000,
            sort_by="name",  # should be overridden
            sort_direction="ASC",  # should be overridden
        ))
        _, params = fake_client.calls[-1]
        assert params["sortby"] == "timemodified"
        assert params["sortdirection"] == "DESC"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_invalidtoken_has_hint(self):
        msg = format_error(MoodleAPIError("invalidtoken", "Invalid"))
        assert "managetoken.php" in msg

    def test_accessexception_has_hint(self):
        msg = format_error(MoodleAPIError("accessexception", "Denied"))
        assert "capability" in msg.lower() or "external service" in msg.lower()

    def test_function_not_in_service_has_hint(self):
        msg = format_error(MoodleAPIError("webservice_function_not_found_in_service", "x"))
        assert "external service" in msg.lower()

    def test_couldnotauthenticate_has_hint(self):
        msg = format_error(MoodleAPIError("couldnotauthenticate", "x"))
        assert "token" in msg.lower()
        assert "service" in msg.lower()
        assert "disabled" in msg.lower()

    def test_nopermissions_distinct_from_accessexception(self):
        nop = format_error(MoodleAPIError("nopermissions", "x"))
        acc = format_error(MoodleAPIError("accessexception", "x"))
        assert "capability" in nop.lower() or "role" in nop.lower()
        assert "authorized users" in acc.lower()
        assert nop != acc

    def test_servicerequireslogin_has_hint(self):
        msg = format_error(MoodleAPIError("servicerequireslogin", "x"))
        assert "function" in msg.lower()
        assert "service" in msg.lower()

    def test_invalidparameter_has_hint(self):
        msg = format_error(MoodleAPIError("invalidparameter", "x"))
        assert "param" in msg.lower() or "type" in msg.lower()

    def test_unknown_code_still_surfaces(self):
        msg = format_error(MoodleAPIError("totally_new_code", "bad thing"))
        assert "totally_new_code" in msg
        assert "bad thing" in msg


@pytest.mark.asyncio
class TestToolErrorPropagation:
    async def test_api_error_returns_message(self, fake_client):
        fake_client.exception = MoodleAPIError("accessexception", "No permission")
        out = await moodle_get_assignments(ListAssignmentsInput(course_ids=[42]))
        assert "accessexception" in out
        assert "No permission" in out

    async def test_users_rag_format_refused(self, fake_client):
        out = await moodle_get_users_by_field(SearchUsersInput(
            field="email", values=["a@b.it"], response_format=ResponseFormat.RAG))
        assert "PII" in out


@pytest.mark.asyncio
class TestChats:
    async def test_get_chats_calls_chats_by_courses(self, fake_client):
        fake_client.response = {"chats": [
            {"id": 5, "course": 42, "name": "Q&A", "intro": "<p>hello</p>",
             "coursemodule": 100, "chattime": 1700000000, "schedule": 0},
        ]}
        await moodle_get_chats(GetChatsInput(course_ids=[42]))
        wsfunc, params = fake_client.calls[-1]
        assert wsfunc == "mod_chat_get_chats_by_courses"
        assert params["courseids"] == [42]

    async def test_get_chats_rag(self, fake_client):
        fake_client.response = {"chats": [
            {"id": 5, "course": 42, "name": "Q&A", "intro": "<p>hi</p>",
             "coursemodule": 100},
        ]}
        out = await moodle_get_chats(GetChatsInput(
            course_ids=[42], response_format=ResponseFormat.RAG,
        ))
        parsed = json.loads(out)
        assert parsed["count"] == 1
        assert parsed["documents"][0]["type"] == "chat"
        assert parsed["documents"][0]["content"] == "hi"
        assert parsed["documents"][0]["metadata"]["chat_id"] == 5

    async def test_get_chat_sessions_passes_showall(self, fake_client):
        fake_client.response = {"sessions": [
            {"sessionstart": 1, "sessionend": 100, "sessionusers": []},
        ]}
        await moodle_get_chat_sessions(GetChatSessionsInput(
            chat_id=5, group_id=0, show_all=False,
        ))
        _, params = fake_client.calls[-1]
        assert params["chatid"] == 5
        assert params["groupid"] == 0
        assert params["showall"] == 0

    async def test_get_chat_sessions_refuses_rag(self, fake_client):
        out = await moodle_get_chat_sessions(GetChatSessionsInput(
            chat_id=5, response_format=ResponseFormat.RAG,
        ))
        assert "rag" in out.lower()
        assert fake_client.calls == []

    async def test_get_chat_session_messages_rag(self, fake_client):
        fake_client.response = {"messages": [
            {"id": 11, "chatid": 5, "userid": 7, "system": False,
             "message": "<p>hi</p>", "timestamp": 1700000010},
            {"id": 12, "chatid": 5, "userid": 8, "system": False,
             "message": "hello", "timestamp": 1700000020},
        ]}
        out = await moodle_get_chat_session_messages(GetChatSessionMessagesInput(
            chat_id=5, session_start=1700000000, session_end=1700000100,
            response_format=ResponseFormat.RAG,
        ))
        wsfunc, params = fake_client.calls[-1]
        assert wsfunc == "mod_chat_get_session_messages"
        assert params["sessionstart"] == 1700000000
        assert params["sessionend"] == 1700000100
        parsed = json.loads(out)
        assert parsed["count"] == 2
        assert parsed["documents"][0]["type"] == "chat_message"
        assert parsed["documents"][0]["content"] == "hi"  # HTML stripped
        assert parsed["documents"][0]["metadata"]["chat_id"] == 5


@pytest.mark.asyncio
class TestCategories:
    async def test_no_name_search_passes_only_addsubcategories(self, fake_client):
        fake_client.response = [
            {"id": 1, "name": "Top", "parent": 0, "depth": 1, "coursecount": 5},
        ]
        await moodle_list_categories(ListCategoriesInput())
        wsfunc, params = fake_client.calls[-1]
        assert wsfunc == "core_course_get_categories"
        assert params["addsubcategories"] == 1
        assert "criteria" not in params

    async def test_name_search_passes_criteria(self, fake_client):
        fake_client.response = []
        await moodle_list_categories(ListCategoriesInput(
            name_search="machine", include_subcategories=False,
        ))
        _, params = fake_client.calls[-1]
        assert params["addsubcategories"] == 0
        assert params["criteria"] == [{"key": "name", "value": "machine"}]

    async def test_rag_emits_category_docs(self, fake_client):
        fake_client.response = [
            {"id": 7, "name": "ML", "parent": 1, "depth": 2,
             "coursecount": 3, "path": "/1/7", "description": "<p>desc</p>"},
        ]
        out = await moodle_list_categories(ListCategoriesInput(
            response_format=ResponseFormat.RAG,
        ))
        parsed = json.loads(out)
        assert parsed["count"] == 1
        doc = parsed["documents"][0]
        assert doc["type"] == "category"
        assert doc["content"] == "desc"  # HTML stripped
        assert doc["metadata"]["category_id"] == 7
        assert doc["metadata"]["parent_id"] == 1
        assert doc["metadata"]["depth"] == 2
        assert doc["metadata"]["path"] == "/1/7"
        assert doc["metadata"]["course_count"] == 3


@pytest.mark.asyncio
class TestFiles:
    async def test_list_files_calls_core_files_get_files(self, fake_client):
        fake_client.response = {"files": [
            {"filename": "slides.pdf", "filepath": "/", "filesize": 1234,
             "fileurl": "https://moodle.test.it/webservice/pluginfile.php/1/x/slides.pdf",
             "mimetype": "application/pdf", "timemodified": 1700000000,
             "isdir": False},
        ]}
        out = await moodle_list_files(ListFilesInput(
            context_id=1, component="mod_resource", filearea="content",
        ))
        wsfunc, params = fake_client.calls[-1]
        assert wsfunc == "core_files_get_files"
        assert params["contextid"] == 1
        assert params["component"] == "mod_resource"
        assert params["filearea"] == "content"
        assert params["itemid"] == 0
        assert params["filepath"] == "/"
        assert "slides.pdf" in out

    async def test_list_files_rag_emits_file_docs(self, fake_client):
        fake_client.response = {"files": [
            {"filename": "x.pdf", "fileurl": "https://h/webservice/pluginfile.php/1/x/x.pdf",
             "filesize": 10, "mimetype": "application/pdf", "isdir": False,
             "timemodified": 1700000000},
            {"filename": "subdir", "isdir": True},
        ]}
        out = await moodle_list_files(ListFilesInput(
            context_id=1, component="c", filearea="f",
            response_format=ResponseFormat.RAG,
        ))
        parsed = json.loads(out)
        assert parsed["count"] == 1  # directory filtered out
        doc = parsed["documents"][0]
        assert doc["type"] == "file"
        assert doc["content"] == ""
        assert doc["metadata"]["filename"] == "x.pdf"
        assert doc["metadata"]["mimetype"] == "application/pdf"
        assert doc["metadata"]["fileurl"].startswith("https://")

    async def test_fetch_file_bytes_rejects_foreign_url(self, fake_client):
        out = await moodle_fetch_file_bytes(FetchFileBytesInput(
            file_url="https://evil.com/webservice/pluginfile.php/1/x/y.pdf",
        ))
        parsed = json.loads(out)
        assert "error" in parsed
        assert "pluginfile.php" in parsed["error"]
        assert fake_client.calls == []  # never reached

    async def test_fetch_file_bytes_oversize_refused(self, fake_client, monkeypatch):
        async def fake_download(url):
            return b"x" * 1000
        monkeypatch.setattr(fake_client, "download_file_bytes", fake_download, raising=False)
        out = await moodle_fetch_file_bytes(FetchFileBytesInput(
            file_url="https://moodle.test.it/webservice/pluginfile.php/1/x/big.bin",
            max_size_bytes=100,
        ))
        parsed = json.loads(out)
        assert "error" in parsed
        assert parsed["size"] == 1000

    async def test_fetch_file_bytes_success(self, fake_client, monkeypatch):
        async def fake_download(url):
            return b"hello"
        monkeypatch.setattr(fake_client, "download_file_bytes", fake_download, raising=False)
        out = await moodle_fetch_file_bytes(FetchFileBytesInput(
            file_url="https://moodle.test.it/webservice/pluginfile.php/1/x/hi.txt",
        ))
        parsed = json.loads(out)
        assert parsed["filename"] == "hi.txt"
        assert parsed["size"] == 5
        import base64
        assert base64.b64decode(parsed["base64"]) == b"hello"

    async def test_download_file_bytes_appends_token(self, monkeypatch):
        """Verify MoodleClient.download_file_bytes adds ?token= correctly."""
        import httpx
        from moodle_mcp.client import MoodleClient

        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, content=b"ok")

        transport = httpx.MockTransport(handler)

        # Patch AsyncClient to use our mock transport
        real_async_client = httpx.AsyncClient

        def make_client(*args, **kwargs):
            kwargs["transport"] = transport
            return real_async_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", make_client)

        client = MoodleClient(base_url="https://moodle.test.it", token="tok123")
        data = await client.download_file_bytes(
            "https://moodle.test.it/webservice/pluginfile.php/1/x/y.pdf"
        )
        assert data == b"ok"
        assert "token=tok123" in captured["url"]

    async def test_download_file_bytes_appends_with_existing_query(self, monkeypatch):
        import httpx
        from moodle_mcp.client import MoodleClient

        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, content=b"ok")

        transport = httpx.MockTransport(handler)
        real_async_client = httpx.AsyncClient
        monkeypatch.setattr(
            httpx, "AsyncClient",
            lambda *a, **k: real_async_client(*a, **{**k, "transport": transport}),
        )
        client = MoodleClient(base_url="https://h", token="t")
        await client.download_file_bytes("https://h/webservice/pluginfile.php/1/x/y.pdf?foo=1")
        assert "&token=t" in captured["url"]


@pytest.mark.asyncio
class TestSearchUsers:
    async def test_calls_get_users_with_criteria(self, fake_client):
        fake_client.response = {"users": [
            {"id": 7, "fullname": "Anna Rossi", "username": "arossi",
             "email": "a@b.it"},
        ]}
        await moodle_search_users(SearchUsersByCriteriaInput(criteria=[
            {"key": "firstname", "value": "Anna"},
            {"key": "lastname", "value": "Rossi"},
        ]))
        wsfunc, params = fake_client.calls[-1]
        assert wsfunc == "core_user_get_users"
        assert params["criteria"][0] == {"key": "firstname", "value": "Anna"}
        assert params["criteria"][1] == {"key": "lastname", "value": "Rossi"}

    async def test_rejects_unknown_key(self, fake_client):
        out = await moodle_search_users(SearchUsersByCriteriaInput(criteria=[
            {"key": "phone", "value": "x"},
        ]))
        assert "Invalid criteria key" in out
        assert "phone" in out
        assert fake_client.calls == []  # never reached Moodle

    async def test_refuses_rag(self, fake_client):
        out = await moodle_search_users(SearchUsersByCriteriaInput(
            criteria=[{"key": "email", "value": "a@b.it"}],
            response_format=ResponseFormat.RAG,
        ))
        assert "PII" in out


# ---------------------------------------------------------------------------
# Tool registry — make sure all tools register
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_tools_registered():
    tools = await srv.mcp.list_tools()
    names = {t.name for t in tools}
    expected = {
        "moodle_list_courses",
        "moodle_get_course_contents",
        "moodle_get_user_courses",
        "moodle_get_users_by_field",
        "moodle_search_users",
        "moodle_get_enrolled_users",
        "moodle_get_assignments",
        "moodle_get_submissions",
        "moodle_get_forums",
        "moodle_get_forum_discussions",
        "moodle_get_discussion_posts",
        "moodle_get_calendar_events",
        "moodle_get_upcoming_events",
        "moodle_list_files",
        "moodle_fetch_file_bytes",
        "moodle_list_categories",
        "moodle_get_chats",
        "moodle_get_chat_sessions",
        "moodle_get_chat_session_messages",
    }
    assert expected.issubset(names)
    # BBS gap target: exactly 19 tools total
    assert len(names) == 19, f"expected 19 tools, got {len(names)}: {sorted(names)}"


@pytest.mark.asyncio
async def test_all_tools_are_read_only():
    """Defense in depth: every tool must declare readOnlyHint=True."""
    tools = await srv.mcp.list_tools()
    for t in tools:
        assert t.annotations is not None, f"{t.name} missing annotations"
        assert t.annotations.readOnlyHint is True, f"{t.name} not marked read-only"
        assert t.annotations.destructiveHint is False, f"{t.name} marked destructive"
