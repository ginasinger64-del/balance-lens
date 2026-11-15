# Case 002 — A5.1 apply-service hardening: results

**Date:** 2026-11-14. **Branch:** `case002-a5-runner`. Not merged. **Keys:** TEST only. No agent trial, no real approval.
**Spec:** `A5_1_SPEC.md`, pushed as `b8fa883` (19:26:25 −0600) before any code change.
**Service:** `apply_service.py` v0.1.1, SHA-256 `08621ea44dc48032f0e1b4e2fbce29b2b8b3a866a87dee9304da528943cfb0ae`. A5 version: `55331bb4…597b` @ `be12aa6`.

## Fixes
1. **Full-parent pin.** The grant carries `authorization.parent_checkpoint_sha256`. The service denies a parent whose full hash differs from it, and a grant without the pin, before looking at the approval's parent claim.
   - Before the fix, the A5 code **applied** a correctly signed, in-scope patch to a parent with an altered non-permitted tensor (`model.norm.weight`) when the approval named that parent. Now it is denied.
2. **Journal before reservation.** The in-flight journal is written and fsynced before the nonce is reserved. Recovery burns the approval whether or not the reservation committed: it updates the row, or inserts a `BURNED_CRASH` row if none exists. The `CRASHED` receipt says which case applied.
3. **External head anchor.**
   - `apply_service.py head --state S` prints a publishable one-line anchor: state ID, seq, head hash, time and version.
   - `verify-chain` and `is-valid` take `--anchor FILE`. The log must contain each anchored seq with the exact anchored hash, for the same state ID.
   - Example anchor from the real-model run: `anchors/TEST_real_model_check_2026-11-14.jsonl`.

## Tests: 28/28 pass

| Test | Result |
|---|---|
| A5 tests 1–12 (21 cases, incl. kill at `mid_write` and `after_rename`, head-lag repair, ENOSPC, OS separation) | PASS (re-run) |
| T13 parent with another tensor altered, approval names it → `DENIED`, no child, nonce untouched | PASS |
| T13b grant without the parent pin → `DENIED` | PASS |
| T14 SIGKILL at `after_journal` → recovery `CRASHED` ("had not committed"), nonce `BURNED_CRASH`, re-use `DENIED` | PASS |
| T14 SIGKILL at `during_reserve` (inside the uncommitted sqlite transaction) → same | PASS |
| T14b SIGKILL after the reservation commits → `CRASHED` ("had committed"), burned | PASS |
| T15 the A5 11b rewrite (drop last receipt + rewrite head) passes plain `verify-chain`, but is BROKEN against the published anchor; `is-valid --anchor` NOT_VALID | PASS |
| T15 same-length consistent rewrite (edit receipt 3, re-chain, rewrite head) → BROKEN against the anchor | PASS |
| T15b a legitimate later apply still verifies against the older anchor; an anchor with a foreign state ID is rejected | PASS |

Two existing assertions were updated because the denial reason changed, not the outcome. A tampered parent (test 3) and the wrong-parent case (test 5) are now caught earlier, by the grant pin.

**Real-model re-run** (grant now pins `04b1c301…4696`): `APPLIED`, VALID. The child is still byte-identical to B4 attempt 4 (`e4c6c6a7…30cf`), the tensor hashes match the B4 record, and the apply took 11.8 s.

## Not done / limits
- **Publishing is manual.** The anchor only protects the log if it is published somewhere the service user can't rewrite. Receipts after the latest published anchor are unprotected until the next one. Receipts are still not signed.
- **Crash before the journal:** after ingest and checks but before the journal, nothing is reserved or written, so there is no receipt and the approval stays usable. Stated in the spec; unchanged.
- **Carried over from A5:** no real keys or trust policy; replay protection per service instance; unsigned revocation list and no approval expiry; no post-apply epoch change or revert receipts; agent sandbox beyond file permissions not built; disk-full tested only on the children directory.
