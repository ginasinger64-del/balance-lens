# Case 002 — A5 apply-and-receipt service: results

**Date:** 2026-11-14. **Branch:** `case002-a5-runner` (from `case002-plan` @ `3b978da`). Not merged.
**Spec:** `A5_SPEC.md`, pushed as `44043b2` (19:06:01 −0600) before any service code or test.
**Keys:** throwaway TEST keys only. Gina Singer's key was not used. No real edit was approved or signed. No editing agent was run.

## Result

All 12 spec tests pass on synthetic checkpoints (21 pytest cases, including sub-cases and 3 extra checks). The real-model check also passes.

| # | Test | Result | What was shown |
|---:|---|---|---|
| 1 | Successful apply | PASS | `APPLIED`; child bytes = independently computed child; receipt hash matches; `is-valid` VALID; nonce `APPLIED`; no leftover work/journal files |
| 2 | Denial (bad/missing approval) | PASS | Missing, malformed, bad signature, unknown key, wrong role, approver = proposer, wrong grant hash and extra envelope field → 8× `DENIED`, no child, nonce not burned; the same valid approval then applies |
| 3 | Tampered patch or parent | PASS | Payload, manifest or parent changed after signing → `DENIED` (hash mismatch), no child |
| 4 | Swap between check and apply | PASS | Staging patch, manifest and parent swapped after ingest → still the *approved* child, built from the private copies. A swap before ingest, and a symlinked staging file → `DENIED` |
| 5 | Validly signed but out of scope / wrong parent | PASS | Column outside grant, over entry budget, third tensor, non-permitted tensor swapped in, int32 indices, float32 values, parent whose tensors aren't the grant base, approval naming a different parent → all `DENIED` by the service's own checks |
| 6 | Replayed approval | PASS | Second use after a success → `DENIED` (replay) |
| 7 | Revoked approval | PASS | Revoked by nonce or by approval hash before apply → `DENIED`. Revoked during apply (after the child was written and verified, before commit) → `DENIED`, child removed, nonce burned, no `APPLIED` |
| 8 | Two requests at once, same approval | PASS | Exactly one `APPLIED` and one `DENIED`, one child. Separately, 8 threads racing the sqlite reservation → exactly 1 succeeds |
| 9 | Killed mid-apply, then recovery | PASS | SIGKILL mid-write and after rename (before receipt): child NOT_VALID, recovery writes `CRASHED`, deletes the child, burns the nonce; re-use → `DENIED`. A crash between receipt append and head update is repaired for exactly one entry; two unrecorded entries → refused |
| 10 | Disk full during write | PASS | Children dir on a 96 KB tmpfs, real ENOSPC → `ERROR` receipt, no partial left, nonce burned, chain OK |
| 11 | Edited or deleted past receipt | PASS | Edited middle (non-canonical and canonical edits), deleted middle, deleted first, truncated tail → chain BROKEN, `is-valid` NOT_VALID, service refuses to apply |
| 12 | OS refuses agent | PASS | As the agent user, all 15 attempts failed with EACCES (errno 13): read signing key, list signer dir, forge handoff approval, list service state, create/overwrite/replace/delete child, write grant / trust policy / revocations / service config / nonce DB / receipts / head. The service user also can't read the signing key. A full apply as the service user from the agent's staging succeeded |
| — | Hooks need admin config (R10) | PASS | With the admin config set to hooks off, `BL_A5_HOOKS` set in the environment has no effect |
| — | Known limit (R5) | Documented | Rewriting the log *and* the head consistently is not detected locally. The test asserts this limit so it stays visible |

**Real-model check** (`real_model_check.py`; TEST key; B4 attempt-4 FEASIBILITY_ONLY patch; TEST_ONLY copy of the Case 001 envelope):
- Result: `APPLIED`, VALID, chain OK.
- Child file SHA-256 `e4c6c6a76b8f11e707bc72479df90af5c0089d4d2b85a2cb1e6872139df630cf`. That is **byte-identical** to the B4 attempt-4 child and to the independently computed child.
- Tensor hashes L15 `1e7d3646…700d` and L19 `2080fb4c…1080` match the B4 record. Changed entries: 65,432 / 65,425.
- Apply took 11.5 s on 2 CPUs. Peak RSS was 5.2 GB for the largest process; the measurement can't separate the check script from the service.
- The first run was correctly `DENIED` because my script staged a symlink. It was re-run with the real file.

Environment: Python 3.13.18, cryptography 50.0.1, numpy 2.5.3. Tests 1–11 run the service as root, and only test 12 uses separate users.

## Not done / limits
- **Real signing:** no human key, no real trust policy, no real approval. Those need Gina's offline key setup.
- **External anchor:** receipts are hash-chained but not signed, and there is no external anchor. A service-user-level actor who rewrites log + head together is not detected (test 11b). Fix: publish or anchor the head hash, or sign receipts with a separate key.
- **Replay across instances:** replay protection is per service state. A second service instance with its own nonce DB would accept the same approval. Deployment rule: one authoritative state per grant. Not tested across hosts.
- **Revocation and approval lifetime:** the revocation list is an admin-held local file and isn't signed. Approvals have no expiry time.
- **Plan §5 items not implemented:** a new authorization epoch / invalidation of prior privilege after apply (beyond burning the nonce), and revert receipts.
- **Agent sandbox:** only the file-permission boundary was tested. The live trial's sandbox (process, network, ptrace isolation) is not built.
- **Disk full:** tested only for the children directory, not for the state/receipt directory.
- **Root:** root / the host admin is trusted and out of scope.

## Also on this branch
B4 loading fix: `b4lib.py` no longer loads the neighbour-check items. They moved to `b4_check_items.py`, which only `eval_b4.py` imports. The `edit_b4.py` docstring, the README and a `B4_FEASIBILITY.md` erratum now say the check items *were* loaded into memory in attempts 2–4 (via the shared import) but were used in no loss or stopping rule. Verified: `b4lib` has no `NB_CHECK`, importing it opens no check file, and the refactored `eval_b4.py` reproduces the B4 baseline outputs exactly.

## Files (`a5_runner/`)
`A5_SPEC.md`, `A5_RESULTS.md`, `apply_service.py` (`55331bb4…597b`), `real_model_check.py`, `tests/a5_testlib.py`, `tests/test_a5_service.py`, `tests/test_a5_os_separation.py` (the latter needs root to create its test users). No weights, patches or generated state are committed.
