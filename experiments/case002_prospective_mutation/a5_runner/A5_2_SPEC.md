# Case 002 — A5.2 final pre-agent hardening: specification

**Written:** 2026-11-14, after `READINESS_CHECKLIST.md` (`e4c0c53`) and before any A5.2 code. Rules: TEST keys only, no live agent trial, nothing merged. Baseline: A5.1 @ `74cbb1b` (`apply_service.py` `08621ea4…b0ae`).

## Changes (checklist items 8–14)

1. **Anchored vs local validity (item 8).** `is-valid` returns:
   - `VALID_ANCHORED`: the whole chain verifies, every supplied anchor matches, and the child's `APPLIED` receipt is at or before the highest anchored seq.
   - `VALID_LOCAL`: chain OK, but no anchor was supplied or the receipt is after the latest anchor.
   - `NOT_VALID`: anything else. That includes a reverted child and a restored parent; the reason is given.
   - Exit codes: 0 / 2 / 1.
   - T15b now expects `VALID_LOCAL` for the child applied after the anchor.
2. **One authoritative state per grant (item 9).** The grant carries `authorization.service_state_id`. `init_state` takes the state ID from the grant (or an explicit argument). Apply is denied if the grant has no `service_state_id` or names another state.
3. **Approval expiry (item 10).** Signed field `not_after_utc` (`YYYY-MM-DDTHH:MM:SSZ`), required. It is denied at ingest if missing, unparseable or past, and re-checked immediately before commit. The host clock is trusted.
4. **Signed revocations (item 11).**
   - `config/revocations.json` must be an Ed25519 envelope over `{schema, service_state_id, sequence, issued_at_utc, revoked_approval_nonces, revoked_approval_sha256}`, under a separate domain, from a trust-policy key with role `revocation_signer`.
   - Any of these makes the service refuse to apply (`DENIED`, reason "revocation list invalid: …"): unsigned, bad signature, unknown key, wrong role, wrong state ID, or a sequence lower than the highest the service has seen (rollback).
   - Checked at ingest and again before commit.
5. **Epoch transition (item 12).**
   - After an `APPLIED`, the grant's `authorization_epoch` is recorded as closed, in the same transaction that marks the nonce `APPLIED`; recovery does the same.
   - Any approval under a closed epoch is denied.
   - `install_grant` refuses a grant whose epoch is closed, so applying again needs a new grant with a new epoch and scope nonce.
6. **Revert receipts (item 13).**
   - At apply time, before the `APPLIED` receipt, the service stores an inverse payload: the parent's original values at the changed indices. Its SHA-256 goes in the receipt.
   - `revert --state S --child C` checks, in order: the child has a valid `APPLIED` receipt, has not been reverted, and its bytes hash to the receipted child hash. It then rebuilds the parent from the child plus the inverse payload.
   - It writes the parent to a `.partial`, re-reads it, requires an exact byte match with the rebuilt bytes and with the receipted parent SHA-256, renames it, and writes a `REVERTED` receipt.
   - After that, the child is `NOT_VALID` (reverted). A journal plus recovery covers crashes during a revert.
   - Revert is an admin command; signed revert authorization is a later item.
7. **Disk full in the state/receipt directory (item 14).**
   - A failed receipt write rolls the log back to its previous length. A failed head update rolls the appended line back, so a receipt is either fully recorded or not at all.
   - If writing a receipt fails after reservation, the service removes the child files, leaves the journal for recovery, and returns `FAILED_CLOSED`. Nothing counts as valid.
   - A child is never removed once its `APPLIED` receipt is recorded.

## New tests
- **T16** VALID_LOCAL without an anchor; VALID_ANCHORED after publishing the head; VALID_LOCAL against an older anchor.
- **T17** a second state with the same grant but a different state ID → DENIED; a grant without `service_state_id` → DENIED.
- **T18** expired → DENIED; missing `not_after_utc` → DENIED; expiry passing during apply (pause before commit) → DENIED, nonce burned, no child.
- **T19** each of these → DENIED: unsigned list, bad signature, approver key without the revocation role, wrong state ID, rollback (seq 5 then 3). A valid signed list still works.
- **T20** after an APPLIED, a second approval under the same grant → DENIED (epoch closed); reinstalling the same grant is refused; a new grant (new epoch + scope nonce) with a new approval → APPLIED.
- **T21** revert → REVERTED, restored bytes == parent, child NOT_VALID; second revert → DENIED; revert of an unreceipted file → DENIED; tampered child → no REVERTED and no restored file; SIGKILL during a revert → recovery CRASHED, no restored file.
- **T22** state dir on a small tmpfs: (a) fill at `after_rename` → FAILED_CLOSED, child removed, NOT_VALID, chain still verifies; after freeing space, recovery → CRASHED, approval unusable. (b) full before ingest → nothing valid, chain verifies.
- **Re-run:** all A5 and A5.1 tests and the real-model check. The real-model check adds VALID_ANCHORED and a revert back to the verified parent hash `04b1c301…4696`.
