# Wave A — Implementation Spec (unified matcher + AA fold-in + route-outcome)

Derived from a read-only deep-dive of the matcher internals (backend-architect),
refining `PIPELINE_OVERHAUL_PLAN_2026-06-20.md` Wave A. All paths under
`/root/Shelf/backend`. **Decisions locked:** (a) Wave A is a PURE refactor — keep
`ledger.mark_unavailable` behavior byte-identical, defer the unavailable-vs-no_match
gating change to Wave B; (b) the shared author+title core lives in `verify.py` (no new
matcher module). `outcome.py` IS a new tiny module (the route-outcome type).

## What's already true (don't rebuild)
- **Torrents already share the Prowlarr scorer** (`release_matcher.py`) with usenet
  (`torrents.py:116`). So "unify usenet+torrent" is DONE. The only fold-in is AA/libgen.
- `verify.score_match` (`verify.py:259`) is ALREADY the shared post-download gate for
  both paths (ISBN short-circuit + multi-title segment score + fuzzy author). It is the
  natural single core to expose for the pre-download AA decision.

## The invariant (safety property)
`release_matcher.py` is **untouched**. The usenet/torrent accept/reject path must be
bit-identical. **`tests/test_release_matcher.py`, `test_matching_improvements.py`,
`test_matching_recall_2026.py`, `test_verify.py` (core), `test_fuzzy.py`,
`test_downloads.py`, `test_torrents.py` must pass UNEDITED.** If any needs editing, the
fold-in leaked into the usenet path → revert that part.

## Implementation order
1. **`app/ingestion/outcome.py`** (new ~30 lines) + `tests/test_outcome.py`.
   `Outcome(str, Enum)`: MATCHED, NO_MATCH, EXHAUSTED, UNAVAILABLE, ERROR.
   `@dataclass(frozen=True) RouteResult(outcome, job=None, status=None, retry_at=None,
   reason=None, route=None)` + `.matched` property. Enum values align with
   `ledger.REASONS` (`ledger.py:35`).
2. **`verify.py`**: add `CandidateScore(score, accept, reason)` + `score_candidate(meta,
   cand_title, cand_author, *, cand_isbn=None, cand_type=None, floor=0.5)` — calls the
   existing `score_match` core (passing alt-titles + ISBN), applies the `type_compat`
   multiplier ONCE, returns score+accept+reason. Pure addition. + tests in
   `test_verify.py` (new cases only; don't edit existing).
3. **`libgen.py`**: `_score_hit` delegates to `verify.score_candidate` (graded fuzzy
   author instead of binary `authors_compatible ×0.4`); REMOVE the now-duplicate
   `type_compat` from `_score_hit` (it moves into `score_candidate` — apply once). Add a
   small `_passes_content_gates(meta, h)` in `candidates_for` reusing
   `release_matcher._BOXSET_TOKENS`/`_COMPANION_TOKENS` + a language gate (today only a
   `_edition_quality` nudge). Keep `_candidate_floor` loosening + `_good_format` +
   `_edition_quality` AA-specific. Update `test_libgen.py` assertions to `>=` bands
   (author-hit `+0.1` may shift exact numbers); add a boxset-drop regression test.
4. **`acquire.py`**: refactor the `for r in order` loop (`:194-285`) so each route block
   builds a `RouteResult` (via a per-route try/except adapter) instead of mutating
   `last_err`. On `res.matched` return the SAME public dict
   (`{"route","status",<id field>}`) — byte-for-byte. Collect non-matched outcomes.
   **Keep `mark_unavailable(reason="no_match")` at the bottom behind the existing
   `if route is None and not audiobook:` guard, UNCHANGED (CODE-H1).** Thread the worst
   outcome's reason into the returned `detail`. `test_acquire.py` cascade + CODE-H1 tests
   pass unchanged; add tests for raise→UNAVAILABLE/ERROR→continue and exact matched dicts.
5. **`scripts/backtest_matching.py`**: add an AA arm — for each stuck title, if
   `libgen.configured`, run `libgen.search_book` (read-only) and score hits OLD (frozen
   copy of today's `_score_hit`) vs NEW (`verify.score_candidate`). Report usenet-recovered
   / AA-recovered / union-recovered (the R5 ≥15% gate), and count titles with ZERO
   releases/hits anywhere (the availability attribution). Read-only; grabs nothing.
6. Run the full suite; confirm the §invariant tests pass unedited; run the backtest.
   **Do NOT deploy** — leave for review.

## Per-route outcome mapping (acquire.py)
| Route | helper | job→ | None→ | raise→ |
|---|---|---|---|---|
| torrent | `torrents.grab` | MATCHED(downloading) | NO_MATCH | infra("no qBittorrent")→UNAVAILABLE; else ERROR |
| pipeline | `downloads.auto_grab` | MATCHED(downloading) | NO_MATCH | SAB-unreachable→UNAVAILABLE; exhausted→EXHAUSTED; else ERROR |
| libgen | `libgen.grab` | MATCHED(downloading) | NO_MATCH | ERROR |
| web_index | `catalog.hook_entry` | MATCHED(hooked) | (skip if no row) | ERROR |
| readarr/kapowarr | `isync.grab_external` | MATCHED(grabbed) | (skip if no row) | ERROR |
| librivox | `librivox.grab` | MATCHED(downloading) | NO_MATCH | ERROR |

Note: libgen/pipeline refine NO_MATCH-vs-EXHAUSTED only LATER in the worker
(`libgen.py:1167`, `downloads.py:639`) via existing `ledger` calls — Wave A does not move
that; at acquire-time an enqueued job is MATCHED.

## Gotchas
- `type_compat` applied ONCE (remove from `_score_hit` when delegating).
- `WorkMeta.raw` carries `extra` incl. `isbn` (`matchmeta.py:286`); a `None` isbn must be
  harmless in `score_candidate`.
- `_candidate_floor` now floors the type-compat-multiplied score — a mistyped comic could
  drop below floor where it previously passed title-only floor then got rank-penalized.
  More correct (matches usenet); cover with a test.
</content>
