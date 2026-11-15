# Case 002 — A5.2 final pre-agent hardening: results

**Date:** 2026-11-14. **Branch:** `case002-a5-runner`. Not merged. **Keys:** TEST only. No agent trial, no real approval.
**Checklist:** `../READINESS_CHECKLIST.md`, pushed as `e4c0c53` (19:39:49). **Spec:** `A5_2_SPEC.md`, pushed as `0b2ba59` (19:40:13). Both went up before any A5.2 code.
**Service:** `apply_service.py` v0.2, SHA-256 `aa295166a8dcbe8d4589195561a5130a2581a5e9931980da10d588ab73d0ae2b`.

## Tests: 37/37 pass

All 28 A5/A5.1 tests were re-run, with three assertions updated for intended A5.2 behaviour:
- `is-valid` without an anchor now returns `VALID_LOCAL` (tests 1, 11, 12).
- T15b expects `VALID_LOCAL` for the post-anchor child.
- Test 6's second use is now denied as "epoch closed" (it was "replay"). Nonce replay is still asserted in tests 9 and 14.

| New test | Result |
|---|---|
| T16 VALID_LOCAL without an anchor and against an older anchor; VALID_ANCHORED after publishing | PASS |
| T17 the same grant on a second state with another state ID → DENIED; a grant naming no state → DENIED | PASS |
| T18 expired / missing / malformed `not_after_utc` → DENIED; expiry passing during apply → DENIED at commit, nonce burned, no child | PASS |
| T19 unsigned list, altered signed content, wrong domain, approver key without the revocation role, unknown key, other state's list → each DENIED without burning the nonce; rollback (seq 5 then 3) refused at install and detected at apply; a signed seq 6 works | PASS |
| T20 second approval under the same grant → DENIED (epoch closed); reinstalling the closed-epoch grant refused; new grant (new epoch + scope nonce) → APPLIED | PASS |
| T21 revert → REVERTED, restored bytes == parent, child NOT_VALID, restored file NOT_VALID; second revert DENIED; unreceipted file DENIED | PASS |
| T21b tampered child → revert DENIED, no restored file; SIGKILL mid-revert → recovery CRASHED (revert), child still VALID_LOCAL, revert then succeeds | PASS |
| T22a state dir on a 2 MB tmpfs filled after rename → FAILED_CLOSED; child removed; no partial receipt line; chain OK; after freeing space, recovery CRASHED; approval unusable | PASS |
| T22b state dir full before ingest → FAILED_CLOSED/ERROR, nothing reserved, chain OK; after freeing space the same approval applies | PASS |

**Real-model check** (TEST key, B4 attempt-4 FEASIBILITY_ONLY patch, grant pinned to parent `04b1c301…4696` and state `TEST-A5-REAL-STATE-1`):

| Step | Result |
|---|---|
| Apply | `APPLIED` in 11.5 s; child `e4c6c6a7…30cf`, byte-identical to B4 attempt 4; tensor hashes match the B4 record |
| `is-valid` without anchor | `VALID_LOCAL` |
| `is-valid` after publishing the head | `VALID_ANCHORED` |
| Revert | `REVERTED` in 10.0 s; restored file re-hashed to `04b1c301…4696` (the verified parent) |
| Child after revert | `NOT_VALID` |
| Chain | OK against the anchors (`anchors/TEST_real_model_check_A5_2_2026-11-14.jsonl`) |

## Problems found and fixed during this step
1. **Revocation rollback** (found by T19): the service only learned a list's sequence when an apply read it, so an unseen newer list could be rolled back. Now `install_revocations` validates the signature and records the sequence at install time, and refuses an older one.
2. **Revocation blocked by the lock** (found by T07b): `install_revocations` waited on the service lock, so a revocation couldn't reach an apply already in progress. It no longer takes the lock: sequence recording is an atomic sqlite transaction and the file replace is atomic. The commit-time re-check now sees it.
3. **Revert out of memory on the real model:** the byte comparison held about 4 copies of the 1.75 GB checkpoint and was killed by the OS. That kill left a journal, and recovery correctly recorded a CRASHED revert. The comparison now streams; the real revert peaks at about 3.5 GB.

## Remaining on the checklist
Items 8–14 are now DONE (dated note in the checklist). Still open:
- **LATER:** #15 receipt signing; #16 anchor publishing procedure; #17 signed revert authorization; #18 agent sandbox; #19 append-only trace capture; #20 handoff inside the real sandbox; #21 pinned agent harness; #23–24 sealed held-out and near-neighbour sets; #25 real per-run grants; #26 frozen run prompts/order; #27 planted negatives on the real setup; #28 evidence packet template; #29 scoring sheet; #36 trusted time source (added).
- **GINA:** #16 choose the anchor location; #22 whether the agent gets neighbour negatives; #30 approval key; #31 revocation key; #32 API account; #33 prices and dollar cap; #34 go/no-go; #35 review and sign during the trial.
