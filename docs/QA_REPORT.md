# QA Report — Unit Test Suite (Missing Test Files)

**Date:** 2026-06-16
**Scope:** New unit tests added for `classifier.py`, `llm_service.py`, `context_retriever.py`, `event_handler.py`
**Environment:** Python 3.8.8, pytest 6.2.3, Windows 10

---

## Final Test Run Result

```
========================= 109 passed, 1 warning in 1.26s =========================
```

All 109 tests pass. This includes 20 pre-existing tests (pii_filter, token_counter) and 89 new tests across the 4 added files.

---

## Files Created

| File | Tests | Status |
|------|-------|--------|
| `slack_bot/tests/conftest.py` | autouse fixtures | NEW |
| `slack_bot/tests/test_classifier.py` | 23 | NEW — all PASS |
| `slack_bot/tests/test_llm_service.py` | 22 | NEW — all PASS |
| `slack_bot/tests/test_context_retriever.py` | 19 | NEW — all PASS |
| `slack_bot/tests/test_event_handler.py` | 25 | NEW — all PASS |

---

## Test Coverage by Module

### `services/classifier.py` (23 tests)
- `MessageCategory` enum values
- `ClassifyResult.none_result()` default values
- `_is_bot_message_by_heuristic()` for all known bot prefixes and negative cases
- `classify_message()`: empty/whitespace/None inputs, bot self-message filter, heuristic filter, mention shortcut (LLM not called), LLM integration for all 3 categories, invalid category fallback, LLM failure fallback, cache hit on second call, FIFO cache eviction at `_CACHE_MAX_SIZE`

### `services/llm_service.py` (22 tests)
- `_strip_json_fence()`: markdown fence removal, plain fence, no fence, empty string
- `parse_json_response()`: valid JSON, empty string default, invalid JSON default, JSON in fence, incomplete JSON
- `call_with_fallback()` (via `_call_with_retry`): first-attempt success, all-model failure returns None, fallback to second model, `RateLimitError` retry with sleep patched, `content=None` (reasoning-only mode), `APITimeoutError` retry
- `call_classifier()`: JSON response_format enforced, correct model chain passed
- `call_qa()`: response forwarded, correct QA chain passed
- `call_summary()`, `call_rag_query()`: basic response forwarding

### `services/context_retriever.py` (19 tests)
- `embed_text()`: empty/whitespace/None returns None, valid text returns vector, exception returns None
- `_generate_rag_query()`: successful LLM returns trimmed string, LLM None falls back to original question, LLM empty string falls back to original question
- `retrieve_context()`: returns context list, exception returns empty list, channel_id passed, top_k passed, embed failure passes `[]` to search
- `format_context_for_prompt()`: empty list placeholder, single context format, multiple contexts, empty chunk_text skipped (but index preserved), all-empty placeholder, missing similarity defaults to 0.0

### `handlers/event_handler.py` (25 tests)
- `_clean_mention_text()`: removes mention tag, removes from middle, no-mention strip, empty string, only mention, different bot ID not removed
- `_evaluate_answer()`: returns True, returns False, LLM failure defaults to True, JSON parse failure defaults to True
- `_save_message_and_embed()`: returns message ID on success, returns None on duplicate (upsert returns None), returns None on upsert exception (rollback called), PII content filtered before save, embedding failure does NOT fail message save
- `_delete_thinking_msg()`: deletes when ts provided, no-op when ts None, swallows exception on failure
- `_send_error_or_fallback()`: calls `post_error` with correct arguments

---

## Environmental Findings (Non-Test Issues)

### MEDIUM — Dependency version mismatch between project and test environment
`db/models.py` uses `sqlalchemy.orm.DeclarativeBase` (SQLAlchemy 2.0+), but the installed version is 1.4.7. `slack_sdk` and `slack_bolt` are not installed at all. Tests compensate with `sys.modules` stubs, but the application cannot run in this environment without installing the correct dependencies.

**Action:** Install production dependencies via `pip install -r slack_bot/requirements.txt` in the target environment before running the application.

### LOW — `openai` version mismatch
Project requires `openai==1.57.0`; installed is 2.2.0. The API surface used (`chat.completions.create`, `RateLimitError`, `APITimeoutError`, `APIError`) is present in both versions, so tests pass. However, there may be behavioral differences not caught by unit tests.

### NOTE — `format_context_for_prompt` uses list index, not filtered index
`enumerate(contexts, 1)` preserves the original list position as the display number, so a context with an empty `chunk_text` at position 1 causes the next valid item to display as `[2]`. This is the current intended behavior (confirmed by code reading) and tests were written to match it. However, it could confuse users who see `[2]` as the first item with no `[1]`. Consider switching to a counter that only increments on valid (non-empty) chunks if sequential display numbering is desired.

---

## Verdict

**APPROVE** — All 109 tests pass. The 4 missing test files are now implemented with comprehensive coverage of happy paths, edge cases, error handling branches, and caching behavior. The 3 environmental findings above are infrastructure issues outside test scope.
