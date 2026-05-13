# Changelog

All notable changes to `moodle-mcp` are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — 2026-05-13

Aligned with the **Bologna Business School (Università di Bologna) Moodle 3.4
Web Services developer guide**. Tool surface grows from 12 to 19; two new
domains (Chat, Files) and one new entity (Categories) are now exposed.

### Added

- **Categories domain** — `moodle_list_categories` wrapping
  `core_course_get_categories`. Supports `name_search`,
  `include_subcategories`, and emits `category` documents in RAG mode
  (`category_id`, `parent_id`, `course_count`, `depth`, `path` in metadata).
- **Multi-criteria user search** — `moodle_search_users` wrapping
  `core_user_get_users`. Accepts a list of `{key, value}` filters ANDed
  together; allowed keys: `id`, `username`, `email`, `firstname`,
  `lastname`, `idnumber`. Refuses `rag` format (PII), like the other
  user-facing tools.
- **Files domain** — two tools and one client method:
  - `moodle_list_files` wrapping `core_files_get_files` (markdown / JSON /
    RAG). RAG emits `file` documents with empty `content` and full
    metadata (filename, filesize, mimetype, fileurl, modified_at).
  - `moodle_fetch_file_bytes` for binary downloads via the BBS-specified
    `pluginfile.php` endpoint, base64-returned in JSON. Size-capped
    (default 10 MB, hard cap 100 MB) and **SSRF-guarded**: refuses URLs
    not starting with `{base_url}/webservice/pluginfile.php/`.
  - `MoodleClient.download_file_bytes(url)` appends `?token=…` (or
    `&token=…`) and follows redirects.
- **Chat domain** — three tools:
  - `moodle_get_chats` wrapping `mod_chat_get_chats_by_courses` (RAG →
    `chat`).
  - `moodle_get_chat_sessions` wrapping `mod_chat_get_sessions`
    (chat_id, group_id, show_all). Refuses RAG — sessions are time
    ranges, not content.
  - `moodle_get_chat_session_messages` wrapping
    `mod_chat_get_session_messages` (RAG → `chat_message`, one per
    message, with `chat_id` and `session_start` in metadata).

### Changed

- **Server-side course search** — when `search` is provided,
  `moodle_list_courses` now calls the native paginated
  `core_course_search_courses` (criterianame=search) instead of fetching
  all courses and filtering client-side. Pagination metadata is built
  from the server-reported `total`. No-search behavior unchanged
  (`core_course_get_courses` + client-side slice).
- **Error hints expanded to the BBS-documented six** — `format_error()`
  now produces distinct, actionable messages for `invalidtoken`,
  `couldnotauthenticate`, `accessexception`, `nopermissions`,
  `servicerequireslogin`, `invalidparameter`.
  `nopermissions` (role/capability) is now correctly distinguished from
  `accessexception` (external-service membership).
- **Repository layout** moved to the canonical `src/moodle_mcp/` +
  `tests/` structure that `pyproject.toml` already expected — flat
  layout couldn't be tested as shipped.

### Tests

- Test count grows from 34 to 62; one assertion locks the registered tool
  surface at exactly 19 (`test_all_tools_registered`).

### Compliance

- All six BBS-documented error codes mapped.
- File downloads use the BBS-specified `pluginfile.php` endpoint with WS
  token and an SSRF guard against unrelated URLs.
- Array parameters PHP-form encoded (`options[ids][0]=1`) per the guide.

## [0.1.0] — 2026-04-21

Initial release.

### Added

- 12 read-only MCP tools across 5 Moodle domains: Courses (3), Users (2),
  Assignments (2), Forums (3), Calendar (2).
- Three response formats per tool: `markdown` (LLM context), `json` (raw
  payload), `rag` (uniform `Document[]` with stable `moodle://{host}/{type}/{id}`
  URIs).
- Incremental sync via `time_modified_since` on forum discussions and
  native `time_start` on calendar events.
- HTML stripping for content fields in markdown / RAG modes.
- Error-code mapping for common Moodle WS exceptions.
