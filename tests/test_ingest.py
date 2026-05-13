"""Tests for moodle-ingest.

All tests use fake Moodle / OpenAI / Qdrant clients — no network, no API
keys required. The goal is to lock down:
- The walker call sequence (correct wsfunctions in correct order).
- Idempotent upsert (UUIDv5 from doc.id, same doc → same UUID).
- --dry-run skips upsert.
- --only filters domains.
- env-var validation refuses to run without required vars.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("MOODLE_URL", "https://moodle.test.it")
os.environ.setdefault("MOODLE_TOKEN", "test_token")

from moodle_mcp.client import MoodleAPIError
from moodle_mcp.ingest import (
    DOC_ID_NAMESPACE,
    Embedder,  # noqa: F401  (re-exported for documentation purposes)
    IngestReport,
    IngestState,
    Ingester,
    MoodleWalker,
    QdrantSink,
    VALID_DOMAINS,
    _FUNCTION_UNAVAILABLE_CODES,
    _parse_args,
    _validate_only,
    doc_uuid,
    main as ingest_main,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeMoodleClient:
    """Returns canned responses keyed by wsfunction. Records every call.

    To simulate a Moodle WS denial, set `errors[wsfunction]` to a MoodleAPIError
    — the next call to that function will raise it.
    """

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.responses: dict[str, object] = {}
        self.errors: dict[str, MoodleAPIError] = {}

    async def call(self, wsfunction, params=None):
        self.calls.append((wsfunction, dict(params or {})))
        if wsfunction in self.errors:
            raise self.errors[wsfunction]
        return self.responses.get(wsfunction)


class FakeEmbedder:
    """Returns deterministic 1536-dim vectors based on text hash. Records inputs."""

    def __init__(self, dim: int = 1536):
        self.dim = dim
        self.received: list[str] = []

    def embed(self, texts):
        self.received.extend(texts)
        return [[float((hash(t) >> i) & 1) for i in range(self.dim)] for t in texts]


class FakeQdrantSink:
    """Captures upserts in-memory."""

    def __init__(self):
        self.ensured = False
        self.upserted_points: list[tuple[str, list[float], dict]] = []

    def ensure_collection(self):
        self.ensured = True

    def upsert_batch(self, docs, vectors):
        for d, v in zip(docs, vectors):
            self.upserted_points.append((doc_uuid(d["id"]), v, dict(d)))
        return len(docs)


# ---------------------------------------------------------------------------
# doc_uuid — deterministic UUIDv5 from moodle:// URI
# ---------------------------------------------------------------------------


class TestDocUUID:
    def test_deterministic(self):
        a = doc_uuid("moodle://h/course/42")
        b = doc_uuid("moodle://h/course/42")
        assert a == b

    def test_different_ids_different_uuids(self):
        assert doc_uuid("moodle://h/course/1") != doc_uuid("moodle://h/course/2")

    def test_uses_url_namespace(self):
        expected = str(uuid.uuid5(DOC_ID_NAMESPACE, "moodle://h/forum_post/12"))
        assert doc_uuid("moodle://h/forum_post/12") == expected


# ---------------------------------------------------------------------------
# IngestState — sqlite checkpoint store
# ---------------------------------------------------------------------------


class TestIngestState:
    def test_set_then_get(self, tmp_path: Path):
        s = IngestState(tmp_path / "state.db")
        assert s.get("bbs", "forum:7") is None
        s.set("bbs", "forum:7", 1700000000)
        assert s.get("bbs", "forum:7") == 1700000000
        s.close()

    def test_tenant_isolation(self, tmp_path: Path):
        s = IngestState(tmp_path / "state.db")
        s.set("bbs", "x", 100)
        s.set("unibo", "x", 200)
        assert s.get("bbs", "x") == 100
        assert s.get("unibo", "x") == 200
        s.close()

    def test_upsert_replaces(self, tmp_path: Path):
        s = IngestState(tmp_path / "state.db")
        s.set("bbs", "x", 100)
        s.set("bbs", "x", 200)
        assert s.get("bbs", "x") == 200
        s.close()


# ---------------------------------------------------------------------------
# CLI parsing + validation
# ---------------------------------------------------------------------------


class TestCLI:
    def test_tenant_required(self):
        with pytest.raises(SystemExit):
            _parse_args([])

    def test_only_parsing(self):
        args = _parse_args(["--tenant", "bbs", "--only", "courses,forums"])
        assert args.only == "courses,forums"

    def test_validate_only_accepts_known(self):
        assert _validate_only("courses,forums") == {"courses", "forums"}

    def test_validate_only_rejects_unknown(self, capsys):
        with pytest.raises(SystemExit) as exc:
            _validate_only("nope,forums")
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "nope" in err

    def test_validate_only_none(self):
        assert _validate_only(None) is None
        assert _validate_only("") is None


# ---------------------------------------------------------------------------
# Walker — order of wsfunction calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestWalker:
    async def test_walks_categories_then_courses(self):
        client = FakeMoodleClient()
        client.responses["core_course_get_categories"] = [
            {"id": 1, "name": "Top", "parent": 0, "depth": 1, "coursecount": 1},
        ]
        client.responses["core_course_get_courses"] = []  # no courses → stop after course step
        walker = MoodleWalker(client, host="h")
        docs = [d async for d in walker.walk()]
        # First two calls must be categories then courses
        first_two = [c[0] for c in client.calls[:2]]
        assert first_two == ["core_course_get_categories", "core_course_get_courses"]
        # One category doc emitted
        assert any(d["type"] == "category" for d in docs)

    async def test_only_filters_domains(self):
        client = FakeMoodleClient()
        client.responses["core_course_get_courses"] = [
            {"id": 42, "fullname": "ML", "shortname": "ML25", "summary": ""},
        ]
        # --only courses: must NOT call categories or per-course drilldowns
        walker = MoodleWalker(client, host="h", only={"courses"})
        docs = [d async for d in walker.walk()]
        called = {c[0] for c in client.calls}
        assert "core_course_get_categories" not in called
        assert "core_course_get_contents" not in called
        assert called == {"core_course_get_courses"}
        assert {d["type"] for d in docs} == {"course"}

    async def test_limit_caps_courses(self):
        client = FakeMoodleClient()
        client.responses["core_course_get_categories"] = []
        client.responses["core_course_get_courses"] = [
            {"id": i, "fullname": f"C{i}", "shortname": f"C{i}", "summary": ""}
            for i in range(1, 11)
        ]
        # contents return [] so per-course drilldown is cheap
        client.responses["core_course_get_contents"] = []
        client.responses["mod_forum_get_forums_by_courses"] = []
        client.responses["mod_assign_get_assignments"] = {"courses": []}
        client.responses["mod_chat_get_chats_by_courses"] = {"chats": []}
        walker = MoodleWalker(client, host="h", limit=3)
        docs = [d async for d in walker.walk()]
        # Only 3 courses → 3 course docs (plus zero from drilldowns)
        assert sum(1 for d in docs if d["type"] == "course") == 3
        # Per-course endpoints should fire 3 times each (not 10)
        contents_calls = [c for c in client.calls if c[0] == "core_course_get_contents"]
        assert len(contents_calls) == 3

    async def test_accessexception_on_categories_does_not_abort(self):
        """If categories WS is denied, walker should skip it and continue to courses."""
        client = FakeMoodleClient()
        client.errors["core_course_get_categories"] = MoodleAPIError(
            "accessexception", "user not in authorized users"
        )
        client.responses["core_course_get_courses"] = [
            {"id": 1, "fullname": "ML", "shortname": "ML25", "summary": ""},
        ]
        client.responses["core_course_get_contents"] = []
        client.responses["mod_forum_get_forums_by_courses"] = []
        client.responses["mod_assign_get_assignments"] = {"courses": []}
        client.responses["mod_chat_get_chats_by_courses"] = {"chats": []}

        walker = MoodleWalker(client, host="h")
        docs = [d async for d in walker.walk()]

        # Categories tried once and failed → recorded as unavailable
        assert walker.unavailable == {"core_course_get_categories": "accessexception"}
        # But the walk continued: course doc is present
        assert any(d["type"] == "course" for d in docs)
        # And courses ws was actually called
        assert any(c[0] == "core_course_get_courses" for c in client.calls)

    async def test_unavailable_function_not_retried_within_same_walk(self):
        """If a per-course endpoint is denied for course 1, walker shouldn't keep retrying it for course 2…N."""
        client = FakeMoodleClient()
        client.responses["core_course_get_categories"] = []
        client.responses["core_course_get_courses"] = [
            {"id": 1, "fullname": "C1", "shortname": "C1", "summary": ""},
            {"id": 2, "fullname": "C2", "shortname": "C2", "summary": ""},
            {"id": 3, "fullname": "C3", "shortname": "C3", "summary": ""},
        ]
        # Block course-contents entirely
        client.errors["core_course_get_contents"] = MoodleAPIError(
            "nopermissions", "role lacks capability"
        )
        client.responses["mod_forum_get_forums_by_courses"] = []
        client.responses["mod_assign_get_assignments"] = {"courses": []}
        client.responses["mod_chat_get_chats_by_courses"] = {"chats": []}

        walker = MoodleWalker(client, host="h")
        async for _ in walker.walk():
            pass

        # Called exactly once before being cached as unavailable
        contents_calls = [c for c in client.calls if c[0] == "core_course_get_contents"]
        assert len(contents_calls) == 1
        assert walker.unavailable == {"core_course_get_contents": "nopermissions"}

    async def test_non_access_errors_still_abort(self):
        """invalidtoken / network errors should propagate — only function-availability is tolerant."""
        client = FakeMoodleClient()
        client.errors["core_course_get_categories"] = MoodleAPIError(
            "invalidtoken", "token bad"
        )
        walker = MoodleWalker(client, host="h")
        with pytest.raises(MoodleAPIError) as exc:
            async for _ in walker.walk():
                pass
        assert exc.value.errorcode == "invalidtoken"

    async def test_all_unavailable_codes_recognised(self):
        for code in _FUNCTION_UNAVAILABLE_CODES:
            client = FakeMoodleClient()
            client.errors["core_course_get_categories"] = MoodleAPIError(code, "denied")
            client.responses["core_course_get_courses"] = []
            walker = MoodleWalker(client, host="h", only={"categories"})
            async for _ in walker.walk():
                pass
            assert walker.unavailable == {"core_course_get_categories": code}, (
                f"code {code!r} should be tolerated but wasn't"
            )

    async def test_since_forces_timemodified_desc_on_discussions(self):
        client = FakeMoodleClient()
        client.responses["core_course_get_categories"] = []
        client.responses["core_course_get_courses"] = [
            {"id": 1, "fullname": "C", "shortname": "C", "summary": ""},
        ]
        client.responses["core_course_get_contents"] = []
        client.responses["mod_forum_get_forums_by_courses"] = [
            {"id": 7, "name": "F", "course": 1, "intro": "", "type": "general"},
        ]
        client.responses["mod_forum_get_forum_discussions"] = {"discussions": [], "totaldiscussions": 0}
        client.responses["mod_assign_get_assignments"] = {"courses": []}
        client.responses["mod_chat_get_chats_by_courses"] = {"chats": []}
        walker = MoodleWalker(client, host="h", since=1700000000, only={"forums", "discussions"})
        async for _ in walker.walk():
            pass
        disc_call = next(c for c in client.calls if c[0] == "mod_forum_get_forum_discussions")
        assert disc_call[1]["sortby"] == "timemodified"
        assert disc_call[1]["sortdirection"] == "DESC"


# ---------------------------------------------------------------------------
# Ingester — orchestration + dry-run + idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestIngester:
    async def _build(self, dry_run: bool = False, only: set[str] | None = None) -> tuple[Ingester, FakeMoodleClient, FakeEmbedder, FakeQdrantSink]:
        client = FakeMoodleClient()
        client.responses["core_course_get_categories"] = []
        client.responses["core_course_get_courses"] = [
            {"id": 1, "fullname": "ML", "shortname": "ML25", "summary": "<p>Intro</p>"},
        ]
        client.responses["core_course_get_contents"] = []
        client.responses["mod_forum_get_forums_by_courses"] = []
        client.responses["mod_assign_get_assignments"] = {"courses": []}
        client.responses["mod_chat_get_chats_by_courses"] = {"chats": []}
        walker = MoodleWalker(client, host="h", only=only)
        embedder = FakeEmbedder()
        sink = FakeQdrantSink()
        ingester = Ingester(
            tenant="bbs", walker=walker, embedder=embedder,
            sink=sink, dry_run=dry_run, batch_size=10,
        )
        return ingester, client, embedder, sink

    async def test_normal_run_upserts(self):
        ingester, _, embedder, sink = await self._build()
        report = await ingester.run()
        assert report.fetched == 1
        assert report.embedded == 1
        assert report.upserted == 1
        assert sink.ensured is True
        assert len(sink.upserted_points) == 1
        # The embedder received "title\n\ncontent" combined text
        assert "ML" in embedder.received[0]
        assert "Intro" in embedder.received[0]

    async def test_dry_run_skips_upsert(self):
        ingester, _, _, sink = await self._build(dry_run=True)
        report = await ingester.run()
        assert report.fetched == 1
        assert report.embedded == 1
        assert report.upserted == 0
        assert report.skipped == 1
        assert sink.upserted_points == []  # never touched
        assert sink.ensured is False  # don't create collection in dry-run either

    async def test_idempotent_double_run(self):
        ingester1, _, _, sink1 = await self._build()
        await ingester1.run()
        first_uuid = sink1.upserted_points[0][0]

        ingester2, _, _, sink2 = await self._build()
        await ingester2.run()
        second_uuid = sink2.upserted_points[0][0]

        # Same doc.id (moodle://h/course/1) → same Qdrant point UUID, every run.
        assert first_uuid == second_uuid

    async def test_report_propagates_walker_unavailable(self):
        client = FakeMoodleClient()
        client.errors["core_course_get_categories"] = MoodleAPIError(
            "accessexception", "denied"
        )
        client.responses["core_course_get_courses"] = [
            {"id": 1, "fullname": "ML", "shortname": "ML25", "summary": ""},
        ]
        client.responses["core_course_get_contents"] = []
        client.responses["mod_forum_get_forums_by_courses"] = []
        client.responses["mod_assign_get_assignments"] = {"courses": []}
        client.responses["mod_chat_get_chats_by_courses"] = {"chats": []}
        walker = MoodleWalker(client, host="h")
        embedder = FakeEmbedder()
        sink = FakeQdrantSink()
        ingester = Ingester(
            tenant="bbs", walker=walker, embedder=embedder, sink=sink, batch_size=10,
        )
        report = await ingester.run()
        assert report.unavailable == {"core_course_get_categories": "accessexception"}
        # report.format() mentions the unavailable section
        formatted = report.format()
        assert "unavailable wsfunctions" in formatted
        assert "core_course_get_categories" in formatted

    async def test_only_courses(self):
        ingester, client, _, _ = await self._build(only={"courses"})
        report = await ingester.run()
        assert report.fetched == 1
        # contents/forums/assignments/chats endpoints must not be called
        called = {c[0] for c in client.calls}
        assert called == {"core_course_get_courses"}


# ---------------------------------------------------------------------------
# main() env-var validation
# ---------------------------------------------------------------------------


class TestMainValidation:
    def test_missing_openai_key_exits_2(self, monkeypatch, capsys):
        # Neutralise dotenv so a developer's local .env doesn't repopulate
        # the env vars we're deliberately stripping below.
        monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
        # Strip all relevant vars; tenant arg is the only thing supplied
        for k in ("MOODLE_URL", "MOODLE_TOKEN", "QDRANT_URL",
                  "QDRANT_API_KEY", "OPENAI_API_KEY"):
            monkeypatch.delenv(k, raising=False)
        # Re-add a couple so we can isolate the OPENAI_API_KEY message
        monkeypatch.setenv("MOODLE_URL", "x")
        monkeypatch.setenv("MOODLE_TOKEN", "x")
        with pytest.raises(SystemExit) as exc:
            ingest_main(["--tenant", "bbs", "--dry-run"])
        assert exc.value.code == 2
        assert "OPENAI_API_KEY" in capsys.readouterr().err

    def test_dry_run_skips_qdrant_env_requirement(self, monkeypatch):
        """dry-run shouldn't need QDRANT_URL / QDRANT_API_KEY to be present."""
        monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
        monkeypatch.setenv("MOODLE_URL", "https://moodle.test.it")
        monkeypatch.setenv("MOODLE_TOKEN", "x")
        monkeypatch.delenv("QDRANT_URL", raising=False)
        monkeypatch.delenv("QDRANT_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

        # We can't actually run end-to-end because Embedder will hit OpenAI.
        # Just verify env validation passes by patching Embedder + walker.
        called = {}

        class _NoopEmbedder:
            def embed(self, texts): return [[0.0] * 1536 for _ in texts]

        def _fake_embedder_init(self, api_key, model="text-embedding-3-small", batch_size=100):
            called["api_key"] = api_key
            self._client = None
            self._model = model
            self._batch_size = batch_size
        monkeypatch.setattr("moodle_mcp.ingest.Embedder.__init__", _fake_embedder_init)
        monkeypatch.setattr("moodle_mcp.ingest.Embedder.embed", lambda self, texts: [[0.0] * 1536 for _ in texts])

        # Walker → no Moodle calls because MoodleClient will fail; patch it to a fake.
        async def _fake_walk(self):
            if False:
                yield None  # empty async generator
        monkeypatch.setattr("moodle_mcp.ingest.MoodleWalker.walk", _fake_walk)

        rc = ingest_main(["--tenant", "bbs", "--dry-run"])
        assert rc == 0
        assert called["api_key"] == "sk-test"
