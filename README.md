# moodle-mcp

A read-only MCP server for extracting data from a Moodle LMS into LLM context windows or RAG pipelines.

Built for the `claude-opus-4-7` family and any MCP-compatible client. Python / FastMCP, stdio transport by default.

## What it does

12 read-only tools across 5 Moodle domains:

| Domain | Tool | Moodle WS function |
|---|---|---|
| **Courses** | `moodle_list_courses` | `core_course_get_courses` |
| | `moodle_get_course_contents` | `core_course_get_contents` |
| | `moodle_get_user_courses` | `core_enrol_get_users_courses` |
| **Users** | `moodle_get_users_by_field` | `core_user_get_users_by_field` |
| | `moodle_get_enrolled_users` | `core_enrol_get_enrolled_users` |
| **Assignments** | `moodle_get_assignments` | `mod_assign_get_assignments` |
| | `moodle_get_submissions` | `mod_assign_get_submissions` |
| **Forums** | `moodle_get_forums` | `mod_forum_get_forums_by_courses` |
| | `moodle_get_forum_discussions` | `mod_forum_get_forum_discussions` |
| | `moodle_get_discussion_posts` | `mod_forum_get_discussion_posts` |
| **Calendar** | `moodle_get_calendar_events` | `core_calendar_get_calendar_events` |
| | `moodle_get_upcoming_events` | `core_calendar_get_action_events_by_timesort` |

Every tool supports three output modes:

- `response_format="markdown"` (default) — compact, structured, ideal for direct injection into an LLM prompt.
- `response_format="json"` — raw Moodle payload with pagination metadata, for inspection or custom processing.
- `response_format="rag"` — uniform `Document[]` shape with stable URIs, plain-text content, and rich metadata, ready for vector store ingestion.

HTML in Moodle text fields (course summaries, forum posts, assignment instructions) is stripped to plain text in markdown and rag modes; preserved as-is in raw json mode.

### The RAG document shape

Every tool that returns content (everything except the three user-resolution tools) supports `response_format="rag"`. The output is a uniform envelope:

```json
{
  "documents": [
    {
      "id": "moodle://moodle.your-institution.it/forum_post/12345",
      "type": "forum_post",
      "title": "Domanda sul lab 3",
      "content": "How do I handle the bias term?",
      "metadata": {
        "post_id": 12345,
        "discussion_id": 500,
        "parent_post_id": 0,
        "is_thread_starter": true,
        "author_id": 11,
        "author_name": "Alice",
        "created_at": "2025-03-15T10:00:00+00:00",
        "modified_at": "2025-03-15T10:00:00+00:00"
      }
    }
  ],
  "count": 1,
  "sync": {"latest_modified_at": "2025-03-15T10:00:00+00:00"}
}
```

Key properties:

- **Stable IDs**: `moodle://{host}/{type}/{id}` — deterministic, safe for upsert into vector stores keyed by document ID. Re-running ingestion produces the same ID for the same entity.
- **One entity = one document**: no automatic splitting. Chunking is the consumer's responsibility (depends on embedding model token budget).
- **Plain-text `content`**: HTML stripped, ready for embedding.
- **`metadata`** holds everything a retrieval filter typically needs: course/forum/discussion IDs, author, timestamps in ISO 8601 UTC, URLs.

Document types produced: `course`, `section`, `module`, `assignment`, `submission`, `forum`, `forum_post`, `calendar_event`.

### Incremental sync

Two tools support a `time_modified_since` parameter (Unix seconds) for fetching only what's changed since the last sync:

- `moodle_get_forum_discussions` — sorts DESC by modification time and stops early when older items are reached
- `moodle_get_calendar_events` — via the native `time_start` parameter

The standard pattern:

1. First sync: call without `time_modified_since`, persist `response.sync.latest_modified_at` per source
2. Next sync: convert that ISO timestamp to Unix seconds, pass as `time_modified_since`, get only the delta
3. Upsert by document `id` — old versions are replaced, new entries inserted

For tools without a `since` parameter (courses, course contents, assignments, forum posts, submissions), Moodle's Web Services don't expose incremental filtering server-side. Re-fetch periodically and rely on the stable document IDs for idempotent upsert.

## Setup

### 1. Moodle side: enable Web Services

A Moodle admin must:

1. **Enable Web Services**: Site administration → Advanced features → Enable web services
2. **Enable the REST protocol**: Site administration → Server → Web services → Manage protocols
3. **Create (or use) an external service**: Site administration → Server → Web services → External services
4. **Add the 12 functions above** to that service
5. **Create a token** for a service user: Site administration → Server → Web services → Manage tokens, OR generate from `/user/managetoken.php`

The token's user must have the relevant capabilities in any course you want to query. For full-site read access, a manager-role user is typical.

### 2. Client side: install

```bash
pip install -e .
# or, for an isolated install
pipx install .
```

### 3. Configure environment

```bash
export MOODLE_URL="https://moodle.your-institution.it"
export MOODLE_TOKEN="paste_token_here"
```

### 4. Register with your MCP client

For Claude Desktop / Claude Code, add to `claude_desktop_config.json` (or equivalent):

```json
{
  "mcpServers": {
    "moodle": {
      "command": "moodle-mcp",
      "env": {
        "MOODLE_URL": "https://moodle.your-institution.it",
        "MOODLE_TOKEN": "paste_token_here"
      }
    }
  }
}
```

Or run directly: `moodle-mcp` (stdio transport).

## Usage patterns

### Pattern 1: LLM-on-the-fly context

User asks "what's due this week in my courses?". The agent:

1. Calls `moodle_get_upcoming_events(time_sort_to=<end_of_week_unix>)`
2. Drops the markdown response straight into context
3. Answers from it

### Pattern 2: RAG ingestion

Build a per-course knowledge base:

1. `moodle_list_courses(response_format="rag")` → one `course` document per course
2. For each course: `moodle_get_course_contents(course_id, response_format="rag")` → one `section` + one `module` document per activity
3. For each forum: `moodle_get_forum_discussions(forum_id, response_format="rag")` then `moodle_get_discussion_posts(discussion_id, response_format="rag")` → one `forum_post` document per post
4. `moodle_get_assignments(course_ids=[...], response_format="rag")` → one `assignment` document per assignment
5. Embed `content` field → upsert into vector store keyed by `id`
6. Persist `sync.latest_modified_at` per source

Incremental re-sync:
- Forum: `moodle_get_forum_discussions(forum_id, time_modified_since=<epoch>, response_format="rag")` → only modified/new threads
- Other sources: re-fetch periodically; upsert by stable ID is idempotent

### Pattern 3: Hybrid live + RAG

For a query like *"answer Mario's question about lab 3 using my course materials"*:

1. RAG retrieves relevant `module`/`forum_post` documents from the vector store (the index)
2. `moodle_get_users_by_field(field="email", values=["mario@unibo.it"], response_format="json")` → resolve user live
3. `moodle_get_user_courses(user_id=...)` → check enrollment context live
4. Compose answer from RAG hits + live context

## Architecture notes

- **Single-tenant token**: one Moodle instance per process via env vars. For multi-tenant deployments (e.g. Didaflow Agent serving multiple universities), wrap this server behind a router or fork to accept per-request tokens.
- **Client-side pagination**: most Moodle WS functions don't paginate server-side. We slice locally and return `next_offset`. Token cost stays bounded via `limit`.
- **Error mapping**: common Moodle error codes (`invalidtoken`, `accessexception`, `webservice_function_not_found_in_service`) are translated to actionable hints so the LLM knows what to ask the admin.
- **HTML stripping**: tolerant regex-based, not a security boundary. Don't pipe output to a browser without re-escaping.

## Roadmap

Not yet implemented but on the natural roadmap for an educational-RAG MCP:

- `mod_quiz_*` — quiz definitions and attempts
- `gradereport_user_get_grade_items` — grade book extraction
- `core_completion_*` — activity completion tracking (key for dropout-risk signals)
- File-content fetching for `resource` modules (currently you get the URL, not the bytes)
- Logs / analytics for participation signals

PRs welcome.

## License

MIT.
