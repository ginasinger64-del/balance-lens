# Case 002 — A5.1 apply-service hardening: specification

**Written:** 2026-11-14, before any A5.1 code change. Pushed to `case002-a5-runner` first, so the commit timestamp anchors it.
**Rules:** same as A5. No live agent trial, no real keys (TEST keys only), nothing merged.
**Baseline:** A5 at `be12aa6` (`apply_service.py` SHA-256 `55331bb4…597b`).

## Fix 1 — pin the full parent
- The grant gains `authorization.parent_checkpoint_sha256`, the full-file SHA-256 of the parent. For the real Qwen parent it is `04b1c301231dd422b8860db31311ab2721511346a32cb1e079c4c4e5f1fe4696`.
- The service denies any parent whose full hash differs from the grant's, whatever the approval says. A grant missing the field is denied.
- Why: in A5 the service checked only the two permitted tensors against the grant, plus the full parent hash against the *approval*. An approval naming an altered parent (correct permitted tensors, some other tensor changed) would have applied.

## Fix 2 — close the crash window
- New order inside the lock: checks → **in-flight journal written and fsynced** → nonce reservation → child write → … A5 reserved first and wrote the journal second.
- Recovery handles a journal whose reservation never committed. It records the approval nonce as burned (inserting a `BURNED_CRASH` row if none exists) and writes a `CRASHED` receipt. A journal whose reservation did commit is handled as before.
- A crash before the journal is written leaves no reservation and no child. Nothing was applied, so no receipt is required and the approval stays usable. This case is stated, not changed.
- New kill points: `after_journal` (journal written, before reservation) and `during_reserve` (inside the sqlite transaction, after the INSERT, before COMMIT).

## Fix 3 — anchor the receipt log outside the service
- New command `apply_service.py head --state S` prints one canonical JSON line: state ID, sequence number, head receipt SHA-256, time and service version. It can be published, e.g. committed to GitHub after each run.
- `verify-chain` and `is-valid` accept `--anchor FILE`, a previously published head (one or more lines). The chain must verify, have the same state ID, and contain each anchored sequence number with exactly the anchored hash. Otherwise it is BROKEN / NOT_VALID.
- A state ID is generated at `init_state` and stored in the service config.
- Stated limit: an anchor protects the log up to the anchored entry. Entries after the latest published anchor are protected only once a newer head is published.

## New tests
- **T13 (Fix 1):** correct signature, in-scope patch, parent with both permitted tensors unchanged but another tensor altered; the approval names the altered parent's hash → `DENIED`, no child. Also: a grant without `parent_checkpoint_sha256` → `DENIED`.
- **T14 (Fix 2):** SIGKILL at `after_journal` and at `during_reserve`. Before recovery no child is valid. Recovery writes `CRASHED` and the approval nonce is burned. Re-using the same approval → `DENIED`.
- **T15 (Fix 3):** the A5 11b case (consistent rewrite of log + head) passes plain `verify-chain` but is BROKEN against a previously published anchor, and `is-valid` refuses. A legitimate later append still verifies against the old anchor. An anchor from another state ID is rejected.
- **Re-run:** all A5 tests 1–12 (all crash points: `mid_write`, `after_rename`, head-lag) and the real-model check, with the grant now carrying the parent hash.
