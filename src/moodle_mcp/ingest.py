"""Walk a Moodle instance, embed every entity, upsert into Qdrant.

The script is structured as a small composition of classes:

- `IngestState` — sqlite-backed per-(tenant, source) latest_modified_at,
  used to enable incremental re-syncs.
- `Embedder` — wraps OpenAI text-embedding-3-small with batching.
- `QdrantSink` — wraps qdrant-client with auto-create-collection +
  idempotent upsert keyed by deterministic UUIDv5 of the doc's
  `moodle://…` URI.
- `MoodleWalker` — iterates the Moodle surface (categories, courses,
  contents, forums, discussions, posts, assignments, chats, sessions,
  messages) and yields `Document` objects.
- `Ingester` — orchestrates the four above.

Each component is mockable: the tests inject fakes for the Moodle client,
the embedding client, and the Qdrant client to keep the suite hermetic.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Iterator, Protocol

from .client import MoodleClient, MoodleAPIError
from .rag import (
    Document,
    _host,
    assignments_to_docs,
    categories_to_docs,
    chat_messages_to_docs,
    chats_to_docs,
    course_contents_to_docs,
    courses_to_docs,
    discussions_to_docs,
    forums_to_docs,
    posts_to_docs,
)

# Standard URL namespace UUID — used so that uuid5(NS_URL, "moodle://h/forum_post/12")
# is deterministic and globally unique. Qdrant points must have UUID or int IDs;
# we want stable, content-addressed IDs and `moodle://…` itself is too long.
DOC_ID_NAMESPACE = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")

OPENAI_EMBED_MODEL = "text-embedding-3-small"
OPENAI_EMBED_DIM = 1536

# Domains a caller can restrict the walk to via --only.
VALID_DOMAINS = {
    "categories", "courses", "contents", "forums", "discussions",
    "posts", "assignments", "chats", "sessions", "messages",
}

# Default state file location follows XDG conventions on Linux/macOS.
DEFAULT_STATE_DIR = (
    Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    / "moodle-ingest"
)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


@dataclass
class IngestReport:
    """End-of-run summary. Printed to stdout, returned for tests to inspect."""

    fetched: int = 0
    embedded: int = 0
    upserted: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    by_type: dict[str, int] = field(default_factory=dict)

    def add(self, doc_type: str) -> None:
        self.by_type[doc_type] = self.by_type.get(doc_type, 0) + 1

    def format(self) -> str:
        types = ", ".join(f"{k}={v}" for k, v in sorted(self.by_type.items())) or "—"
        return (
            f"fetched={self.fetched} embedded={self.embedded} "
            f"upserted={self.upserted} skipped={self.skipped} "
            f"errors={len(self.errors)}\n"
            f"by_type: {types}"
        )


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class IngestState:
    """Tiny sqlite-backed checkpoint store.

    One row per (tenant, source). `source` is a free-form string identifying
    the Moodle endpoint family (e.g. `forum_discussions:7`) so different
    forums get independent checkpoints. Callers decide the granularity.
    """

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), isolation_level=None)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS checkpoints (
                tenant TEXT NOT NULL,
                source TEXT NOT NULL,
                latest_modified_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (tenant, source)
            )
        """)

    def get(self, tenant: str, source: str) -> int | None:
        row = self._conn.execute(
            "SELECT latest_modified_at FROM checkpoints WHERE tenant = ? AND source = ?",
            (tenant, source),
        ).fetchone()
        return row[0] if row else None

    def set(self, tenant: str, source: str, ts: int) -> None:
        import time
        self._conn.execute(
            "INSERT OR REPLACE INTO checkpoints (tenant, source, latest_modified_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (tenant, source, int(ts), int(time.time())),
        )

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------------------
# Embedder
# ---------------------------------------------------------------------------


class EmbeddingClient(Protocol):
    """Duck-typed interface so tests can swap in a fake without touching OpenAI."""

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class Embedder:
    """OpenAI text-embedding-3-small with batching and empty-text handling."""

    def __init__(
        self,
        api_key: str,
        model: str = OPENAI_EMBED_MODEL,
        batch_size: int = 100,
    ):
        from openai import OpenAI
        self._client = OpenAI(api_key=api_key)
        self._model = model
        self._batch_size = batch_size

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # OpenAI rejects empty strings; substitute a single space which embeds
        # cheaply and lets us preserve list alignment with the caller's docs.
        safe = [t if t else " " for t in texts]
        out: list[list[float]] = []
        for i in range(0, len(safe), self._batch_size):
            chunk = safe[i : i + self._batch_size]
            resp = self._client.embeddings.create(model=self._model, input=chunk)
            out.extend(item.embedding for item in resp.data)
        return out


# ---------------------------------------------------------------------------
# Qdrant sink
# ---------------------------------------------------------------------------


def doc_uuid(doc_id: str) -> str:
    """Deterministic UUIDv5 derived from a `moodle://…` URI.

    Same doc.id across runs → same UUID → Qdrant upserts replace in place.
    No duplicates on re-runs.
    """
    return str(uuid.uuid5(DOC_ID_NAMESPACE, doc_id))


class QdrantSink:
    """Wraps qdrant-client with auto-create + idempotent batch upsert."""

    def __init__(
        self,
        client: Any,
        collection: str,
        vector_size: int = OPENAI_EMBED_DIM,
    ):
        self._client = client
        self._collection = collection
        self._vector_size = vector_size

    @classmethod
    def from_url(
        cls,
        url: str,
        api_key: str,
        collection: str,
        vector_size: int = OPENAI_EMBED_DIM,
    ) -> "QdrantSink":
        from qdrant_client import QdrantClient
        return cls(QdrantClient(url=url, api_key=api_key), collection, vector_size)

    def ensure_collection(self) -> None:
        from qdrant_client.http.models import Distance, VectorParams
        if not self._client.collection_exists(self._collection):
            self._client.create_collection(
                collection_name=self._collection,
                vectors_config=VectorParams(size=self._vector_size, distance=Distance.COSINE),
            )

    def upsert_batch(self, docs: list[Document], vectors: list[list[float]]) -> int:
        if not docs:
            return 0
        from qdrant_client.http.models import PointStruct
        points = [
            PointStruct(
                id=doc_uuid(d["id"]),
                vector=vec,
                payload=dict(d),  # full doc as payload — searchable + retrievable
            )
            for d, vec in zip(docs, vectors)
        ]
        self._client.upsert(collection_name=self._collection, points=points, wait=True)
        return len(points)


# ---------------------------------------------------------------------------
# Moodle walker
# ---------------------------------------------------------------------------


class MoodleWalker:
    """Walks a Moodle instance in deterministic top-down order.

    Calls `MoodleClient.call()` directly (bypassing the MCP tool layer) and
    routes raw responses through the RAG converters from `moodle_mcp.rag`.
    """

    def __init__(
        self,
        client: MoodleClient,
        host: str,
        *,
        since: int | None = None,
        limit: int | None = None,
        only: set[str] | None = None,
    ):
        self._client = client
        self._host = host
        self._since = since
        self._limit = limit
        # None = walk everything. Empty set is treated the same — defensive.
        self._only = only or None

    def _enabled(self, domain: str) -> bool:
        return self._only is None or domain in self._only

    def _cap(self, items: list[Any]) -> list[Any]:
        return items[: self._limit] if self._limit else items

    async def walk(self) -> AsyncIterator[Document]:
        # 1. Categories
        if self._enabled("categories"):
            cats = await self._client.call("core_course_get_categories", {"addsubcategories": 1})
            for doc in categories_to_docs(self._cap(cats or []), self._host):
                yield doc

        # 2. Courses (full list, no search)
        all_courses: list[dict] = []
        if self._enabled("courses"):
            courses_resp = await self._client.call("core_course_get_courses")
            all_courses = courses_resp or []
            for doc in courses_to_docs(self._cap(all_courses), self._host):
                yield doc
        else:
            # We still need the course list for per-course drilldowns even
            # when --only excludes courses themselves.
            if self._enabled("contents") or self._enabled("forums") or \
               self._enabled("assignments") or self._enabled("chats"):
                courses_resp = await self._client.call("core_course_get_courses")
                all_courses = courses_resp or []

        course_ids = [c["id"] for c in self._cap(all_courses) if c.get("id")]

        # 3. Per-course drilldowns
        for course_id in course_ids:
            if self._enabled("contents"):
                sections = await self._client.call(
                    "core_course_get_contents", {"courseid": course_id}
                ) or []
                for doc in course_contents_to_docs(sections, course_id, self._host):
                    yield doc

            if self._enabled("forums") or self._enabled("discussions") or self._enabled("posts"):
                forums_resp = await self._client.call(
                    "mod_forum_get_forums_by_courses", {"courseids": [course_id]}
                ) or []
                if self._enabled("forums"):
                    for doc in forums_to_docs(forums_resp, self._host):
                        yield doc

                # 4. Per-forum discussions → posts
                if self._enabled("discussions") or self._enabled("posts"):
                    for forum in forums_resp:
                        forum_id = forum.get("id")
                        if not forum_id:
                            continue
                        discussions = await self._fetch_discussions(forum_id)
                        if self._enabled("discussions"):
                            for doc in discussions_to_docs(discussions, forum_id, self._host):
                                yield doc
                        if self._enabled("posts"):
                            for d in discussions:
                                disc_id = d.get("discussion")
                                if not disc_id:
                                    continue
                                posts_resp = await self._client.call(
                                    "mod_forum_get_discussion_posts",
                                    {"discussionid": disc_id},
                                )
                                posts = posts_resp.get("posts", []) if isinstance(posts_resp, dict) else []
                                for doc in posts_to_docs(posts, disc_id, self._host):
                                    yield doc

            if self._enabled("assignments"):
                a_resp = await self._client.call(
                    "mod_assign_get_assignments", {"courseids": [course_id]}
                )
                a_courses = a_resp.get("courses", []) if isinstance(a_resp, dict) else []
                for doc in assignments_to_docs(a_courses, self._host):
                    yield doc

            if self._enabled("chats") or self._enabled("sessions") or self._enabled("messages"):
                c_resp = await self._client.call(
                    "mod_chat_get_chats_by_courses", {"courseids": [course_id]}
                )
                chats = c_resp.get("chats", []) if isinstance(c_resp, dict) else []
                if self._enabled("chats"):
                    for doc in chats_to_docs(chats, self._host):
                        yield doc

                # 5. Per-chat sessions → messages
                if self._enabled("sessions") or self._enabled("messages"):
                    for chat in chats:
                        chat_id = chat.get("id")
                        if not chat_id:
                            continue
                        s_resp = await self._client.call(
                            "mod_chat_get_sessions",
                            {"chatid": chat_id, "groupid": 0, "showall": 1},
                        )
                        sessions = s_resp.get("sessions", []) if isinstance(s_resp, dict) else []
                        if self._enabled("messages"):
                            for sess in sessions:
                                start = sess.get("sessionstart")
                                end = sess.get("sessionend")
                                if start is None or end is None:
                                    continue
                                m_resp = await self._client.call(
                                    "mod_chat_get_session_messages",
                                    {
                                        "chatid": chat_id,
                                        "sessionstart": start,
                                        "sessionend": end,
                                        "groupid": 0,
                                    },
                                )
                                messages = m_resp.get("messages", []) if isinstance(m_resp, dict) else []
                                for doc in chat_messages_to_docs(messages, chat_id, start, self._host):
                                    yield doc

    async def _fetch_discussions(self, forum_id: int) -> list[dict]:
        """Fetch one forum's discussions, with incremental filter applied.

        If `since` is set, force timemodified DESC sort so we can early-stop.
        """
        params: dict[str, Any] = {
            "forumid": forum_id,
            "page": 0,
            "perpage": 100,
        }
        if self._since is not None:
            params["sortby"] = "timemodified"
            params["sortdirection"] = "DESC"
        resp = await self._client.call("mod_forum_get_forum_discussions", params)
        discussions = resp.get("discussions", []) if isinstance(resp, dict) else []
        if self._since is not None:
            out = []
            for d in discussions:
                if (d.get("timemodified") or 0) >= self._since:
                    out.append(d)
                else:
                    break  # DESC sort + threshold = early stop
            return out
        return discussions


# ---------------------------------------------------------------------------
# Ingester
# ---------------------------------------------------------------------------


class Ingester:
    """Top-level orchestrator. Walk → embed → upsert."""

    def __init__(
        self,
        *,
        tenant: str,
        walker: MoodleWalker,
        embedder: EmbeddingClient,
        sink: QdrantSink | None,
        state: IngestState | None = None,
        dry_run: bool = False,
        batch_size: int = 100,
    ):
        self._tenant = tenant
        self._walker = walker
        self._embedder = embedder
        self._sink = sink
        self._state = state
        self._dry_run = dry_run
        self._batch_size = batch_size

    async def run(self) -> IngestReport:
        report = IngestReport()
        if self._sink and not self._dry_run:
            self._sink.ensure_collection()

        batch: list[Document] = []
        async for doc in self._walker.walk():
            report.fetched += 1
            report.add(doc["type"])
            batch.append(doc)
            if len(batch) >= self._batch_size:
                self._flush(batch, report)
                batch = []
        if batch:
            self._flush(batch, report)
        return report

    def _flush(self, batch: list[Document], report: IngestReport) -> None:
        texts = [self._embeddable_text(d) for d in batch]
        vectors = self._embedder.embed(texts)
        report.embedded += len(vectors)
        if self._dry_run:
            report.skipped += len(batch)
            return
        if self._sink is None:
            return
        upserted = self._sink.upsert_batch(batch, vectors)
        report.upserted += upserted

    @staticmethod
    def _embeddable_text(doc: Document) -> str:
        """Combine title + content for a richer embedding signal.

        Many Moodle entities (modules, files) have meaningful titles but
        short or empty content. Prepending the title makes the embedding
        more useful at retrieval time.
        """
        title = doc.get("title") or ""
        content = doc.get("content") or ""
        if title and content:
            return f"{title}\n\n{content}"
        return title or content or " "


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="moodle-ingest",
        description="Walk Moodle, embed with OpenAI text-embedding-3-small, upsert into Qdrant.",
    )
    parser.add_argument(
        "--tenant", required=True,
        help="Qdrant collection name. One tenant per invocation (e.g. 'bbs', 'unibo').",
    )
    parser.add_argument(
        "--since", type=int, default=None,
        help="Unix timestamp (seconds). Only fetch entities modified at or after this time.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Walk + embed but skip Qdrant upsert. Useful for development.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Cap items per Moodle source (e.g. first N categories, courses). For testing.",
    )
    parser.add_argument(
        "--only", type=str, default=None,
        help="Comma-separated subset of domains to walk. "
             f"Allowed: {sorted(VALID_DOMAINS)}.",
    )
    parser.add_argument(
        "--state-dir", type=Path, default=DEFAULT_STATE_DIR,
        help=f"Where to store the sqlite checkpoint DB. Defaults to {DEFAULT_STATE_DIR}.",
    )
    return parser.parse_args(argv)


def _validate_env(args: argparse.Namespace) -> tuple[str, str, str, str, str]:
    """Read required env vars; return them or exit with a clear message."""
    missing: list[str] = []
    moodle_url = os.environ.get("MOODLE_URL")
    moodle_token = os.environ.get("MOODLE_TOKEN")
    qdrant_url = os.environ.get("QDRANT_URL")
    qdrant_key = os.environ.get("QDRANT_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")
    if not moodle_url: missing.append("MOODLE_URL")
    if not moodle_token: missing.append("MOODLE_TOKEN")
    if not args.dry_run and not qdrant_url: missing.append("QDRANT_URL")
    if not args.dry_run and not qdrant_key: missing.append("QDRANT_API_KEY")
    if not openai_key: missing.append("OPENAI_API_KEY")
    if missing:
        print(
            f"ERROR: missing required env vars: {', '.join(missing)}\n"
            "Copy .env.example to .env, fill in, and re-run (or `export` them).",
            file=sys.stderr,
        )
        sys.exit(2)
    return moodle_url, moodle_token, qdrant_url or "", qdrant_key or "", openai_key


def _validate_only(only_arg: str | None) -> set[str] | None:
    if not only_arg:
        return None
    requested = {s.strip() for s in only_arg.split(",") if s.strip()}
    bad = requested - VALID_DOMAINS
    if bad:
        print(
            f"ERROR: unknown --only domains: {sorted(bad)}.\n"
            f"Allowed: {sorted(VALID_DOMAINS)}",
            file=sys.stderr,
        )
        sys.exit(2)
    return requested


def main(argv: list[str] | None = None) -> int:
    # python-dotenv auto-loads .env from cwd if present; do it explicitly
    # so the user can run from anywhere and still get the env loaded.
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    args = _parse_args(argv if argv is not None else sys.argv[1:])
    moodle_url, moodle_token, qdrant_url, qdrant_key, openai_key = _validate_env(args)
    only = _validate_only(args.only)

    client = MoodleClient(base_url=moodle_url, token=moodle_token)
    walker = MoodleWalker(
        client=client,
        host=_host(moodle_url),
        since=args.since,
        limit=args.limit,
        only=only,
    )
    embedder = Embedder(api_key=openai_key)
    sink = (
        QdrantSink.from_url(qdrant_url, qdrant_key, args.tenant)
        if not args.dry_run else None
    )
    state = IngestState(args.state_dir / "state.db")

    ingester = Ingester(
        tenant=args.tenant,
        walker=walker,
        embedder=embedder,
        sink=sink,
        state=state,
        dry_run=args.dry_run,
    )
    try:
        report = asyncio.run(ingester.run())
    except MoodleAPIError as e:
        print(f"ERROR: Moodle API: [{e.errorcode}] {e.message}", file=sys.stderr)
        return 1
    finally:
        state.close()

    print(report.format())
    return 0 if not report.errors else 1


if __name__ == "__main__":
    sys.exit(main())
