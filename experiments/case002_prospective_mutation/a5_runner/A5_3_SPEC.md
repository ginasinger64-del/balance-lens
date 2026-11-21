# Case 002 — A5.3 service hardening: specification

**Written:** 2026-11-21, before any A5.3 code. **Revised the same day after Gina's review** (independent anchors; revert limitations and crash reporting; TEST-time isolation; second revision: state mode fixed at creation, §3a), still before any code. **Branch:** `case002-a5.3-service`, cut from `case002-a5-runner` @ `11240d4`.
**Rules:** TEST keys only. No live agent trial, no real approval, nothing merged.
**Baseline:** A5.2 (`apply_service.py` v0.2, SHA-256 `aa295166…ae2b`). 37/37 tests re-run green on 2026-11-21 before this spec was written.
**Checklist items:** #15 (service-signed receipts), #17 (signed revert authorization), #36 (trusted time source).

## Why these three

- **#15.** In A5.2 the chain is only hash-linked. Someone who can write the state directory can edit a receipt, recompute every later `prev_sha256` and the head, and `verify-chain` reports OK. Only a previously published anchor catches that. Signing makes a rewrite need the service key too. It also lets a third party check an exported receipt log, or a published anchor, against one pinned public key without trusting the machine it came from.
- **#17.** Revert is an admin command in A5.2. Anyone who can run it can turn a valid child into `NOT_VALID`. Revert must need a signed, one-time, child-specific request, like apply does.
- **#36.** A5.2 trusts the host clock for approval expiry. In this cloud sandbox the kernel itself reports the clock as **not synchronised** (`adjtimex` returns `TIME_ERROR`, status `STA_UNSYNC`, max error 16 s, observed 2026-11-21). A live trial cannot depend on that.

## Changes

### 1. Service-signed receipts and anchors (item 15)

- **Key.** `service_config.json` names `receipt_key_id` and `receipt_key_path`: an Ed25519 PKCS#8 PEM private key, kept **outside** the state directory. The service refuses to operate (`REFUSED_MISCONFIGURED`) if the key file:
  - is missing, a symlink or not a regular file,
  - is not owned by the service's uid, or has any group or other permission bits.
- **Pinning.** The trust policy must hold exactly one enabled entry for `receipt_key_id` with role `receipt_signer`, and its public key must equal the private key's public key. Otherwise the service refuses.
- **Role separation.** A `receipt_signer` key may hold no other role. No key holding `patch_approver`, `revocation_signer` or `revert_authorizer` may also hold `receipt_signer`. A policy that breaks this is refused.
- **Receipt format.** Each receipt line is `canonical({...body, "receipt_key_id", "receipt_sig_b64"})`. The signature covers `RECEIPT_DOMAIN + canonical(body)`, and `body` already includes `seq` and `prev_sha256`. The chain hash stays SHA-256 of the full line, signature included.
- **Verification.** `read_chain` verifies every receipt's signature against the pinned key. A missing, invalid or other-key signature is a chain problem, and the service refuses to operate (as A5.2 does for a broken chain).
- **Anchors.** The `head` command emits a signed anchor (`ANCHOR_DOMAIN`). It includes `receipt_key_id` and `receipt_public_key_sha256`. `check_anchors` verifies the anchor signature against the pinned key and requires the fingerprint to match. So swapping the policy key and re-signing the whole log is caught by any earlier published anchor.
- **Third-party check.** A new command, `verify-receipts --receipts FILE --pubkey-b64 K [--anchor FILE]`, verifies an exported log (and anchors) with nothing but a public key. It needs no state directory and no trust policy.
- **Independent trust anchors.** Everything the service host holds can be replaced by whoever controls that host: the trust policy, the receipt log, the anchor files beside it. Someone could swap in a new key, re-sign the whole history and write matching anchors, and every local check would pass. So:
  - **Out-of-band pin.** At state creation the service prints a pin record: `{state_id, state_mode, receipt_public_key_sha256, genesis_receipt_sha256}` (`init` output, and a `pin` command). It must be recorded somewhere the service host cannot write, before the first apply. That location is checklist #16 (Gina chooses it). The pin fixes the state's mode as well as its key (§3a).
  - **`VALID_ANCHORED` needs evidence the verifier supplies.** `is-valid` returns `VALID_ANCHORED` only when the caller passes **both** an anchor file and `--pubkey-b64` (or `--pubkey-sha256`). The pinned key must match that independently held value, and each anchor must verify under it. Anchors read from the state directory, or verified only against the state's own policy, give at most `VALID_LOCAL`.
  - **Evidence boundary, stated in the results.** A valid signature shows a receipt was produced by the holder of the pinned key. It does not show the history is complete: the key holder could drop or withhold receipts after the last externally held anchor. It also does not show the external action happened beyond what the receipt hashes bind (the child file's bytes on disk). Completeness holds only up to the highest anchor held outside the host.
- **Out of scope.** Key rotation and storing the key in hardware or a separate signing process are not covered. The key is readable by the service user, so it protects against anyone who can write the state directory but is not the service user. It does not protect against a compromised service user. This is stated in the results.

### 2. Signed revert authorization (item 17)

- **Command.** `revert --state S --child C --request R.signed.json`. Revert without `--request` is denied.
- **Envelope.** `{revert_request, key_id, algorithm: "Ed25519", signature_b64}`, signed over `REVERT_DOMAIN + canonical(revert_request)` by a policy key with role `revert_authorizer`.
- **Request fields** (schema `case002-a5-revert-request/v0.1`): `service_state_id`, `child_sha256`, `applied_receipt_seq`, `applied_receipt_line_sha256`, `revert_nonce`, `reason`, `not_after_utc`, `requester_principal`.
- **Checks before reservation.** Any failure here is `DENIED` and the nonce is untouched:
  - signature, role and schema;
  - `requester_principal` equals the key's principal;
  - the state ID matches;
  - the receipt at `applied_receipt_seq` is `APPLIED`, its line hashes to `applied_receipt_line_sha256`, and its `child_sha256` equals both the request's and the file's hash;
  - the child has not already been reverted;
  - not expired (trusted time, §3);
  - the revert nonce has not been used.
- **One-time use.** The revert nonce is recorded in a new sqlite table, `revert_nonces`. Order: the journal first, then the nonce reservation, then the write. Recovery after a crash marks the nonce `BURNED_CRASH`. An error after reservation burns it.
- **Receipt.** `REVERTED` records `revert_request_sha256`, `revert_nonce` and `revert_authorizer`.
- **Short validity window.** The request carries a signed `issued_at_utc`. The service refuses a request whose `not_after_utc - issued_at_utc` exceeds `service_config.max_revert_window_s` (default 900 s), or whose `issued_at_utc` is in the future beyond the clock error.
- **Completion is shown only by a `REVERTED` receipt.** A consumed nonce never means the revert happened:
  - Nonce states are distinct: `RESERVED`, `USED` (set together with the `REVERTED` receipt), and `BURNED_CRASH` / `BURNED_ERROR` (never completed).
  - Crash before the `REVERTED` receipt: recovery removes any partial or unreceipted restored file, writes a `CRASHED` receipt (`action: revert`, `completed: false`), and marks the nonce `BURNED_CRASH`. The child stays valid, and `is-valid` does not call it reverted.
  - Crash after the `REVERTED` receipt but before cleanup: the receipt is the completion evidence. Recovery marks the nonce `USED`, and no `CRASHED` receipt is written.
  - A write or verify failure after reservation is reported as `ERROR` or `FAILED_CLOSED`, never `REVERTED`, and the nonce is burned.

### Accepted limitations (revert)
- **No revocation of revert requests.** A signed revert request cannot be cancelled before it is used. This is accepted for A5.3. The limits are single use, binding to one exact child and receipt, and the short validity window above. Revert-request revocation stays open as a LATER item.

### 3. Trusted time (item 36)

- **Time policy.** `service_config.time_policy` is one of:
  - `kernel_synced` (default; required outside tests);
  - `host_unverified_TEST`, accepted only in a TEST-mode state (§3a).
- **`kernel_synced` check.** At each point where time decides something (apply ingest, apply pre-commit, revert), the service reads the kernel's clock state (`adjtimex`, read-only). It denies with `"trusted time unavailable: …"` if:
  - the call returns `TIME_ERROR` or the status has `STA_UNSYNC`, or
  - the kernel's `maxerror` exceeds `service_config.max_clock_error_s` (default 1.0 s).
- **Rollback floor.** The service records the highest time it has used (sqlite `meta`). It denies `"clock moved backwards"` if the current time is more than 2 s below that floor.
- **Conservative expiry.** An approval or revert request counts as expired when `now + maxerror_s >= not_after_utc`.
- **Evidence.** Every receipt carries `time_evidence`: `{policy, synced, maxerror_us, kernel_status_hex, timex_ret}`.
- **Test injection.** A `fake_timex` object in `service_config` replaces the kernel reading **only** when `test_hooks_enabled` is true; otherwise it is ignored. Receipts record that the fake was used.
- **Isolation of TEST time from production:** see §3a. TEST time is a property of a TEST-mode state, never a per-request or per-verifier option.
- **Reporting rule.** A pass under `host_unverified_TEST` or `fake_timex` is never counted as evidence that strict trusted-time enforcement passed. The results report the strict-policy run on the real host in its own section, with the raw `adjtimex` values, apart from any TEST-time run.
- **Threat model.** The agent runs as a separate unprivileged user and cannot set the clock (that needs `CAP_SYS_TIME`). This item guards against an unsynchronised or drifting host and against clock rollback by an admin mistake. It does not guard against a malicious root. A host with working NTP is a precondition for a live trial and is added to the checklist as a GINA/LATER item.

### 3a. State mode: TEST vs PRODUCTION (fixed at creation)

The `TEST-` prefix on a state ID is a naming convenience only. It is **not** a boundary: anyone who can create or rename a state can choose its name. The boundary is the following.

1. **Mode is fixed at creation and bound into signed history.**
   - `init` requires an explicit `--mode TEST|PRODUCTION`; there is no default.
   - Receipt 1 of every state is a signed `STATE_CREATED` receipt recording `state_mode`, `state_id`, `time_policy`, the receipt key fingerprint, and the SHA-256 of the trust policy and service config at creation.
   - Every later receipt repeats `state_mode`.
   - The out-of-band pin (§1) records the mode and the genesis receipt hash.
2. **No promotion path.**
   - No command changes a state's mode.
   - On every start the service refuses to operate (`REFUSED_MISCONFIGURED`) if the mode file, the service config or any receipt disagrees with the `STATE_CREATED` receipt.
   - Editing the mode file or config breaks agreement with the signed genesis receipt. Rewriting the genesis receipt breaks the chain, the signature or the off-host pin (§1).
   - A TEST state can only be discarded, never converted.
3. **PRODUCTION enforces strict time at the service boundary.**
   - A PRODUCTION state refuses to operate if its config contains `test_hooks_enabled: true`, any `fake_timex`, or any `time_policy` other than `kernel_synced`. Those keys are refused, not ignored.
   - Every apply and revert in a PRODUCTION state uses the real kernel reading (§3). No request field, CLI flag or verifier option can relax this.
4. **Cryptographic mode separation.**
   - Every signing domain includes the mode: approval, revocation list, revert request, receipt and anchor, e.g. `BALANCE-LENS-CASE002-A5-APPLY/v0.2/PRODUCTION\0` vs `…/TEST\0`.
   - An approval, revert request or revocation list signed for one mode fails signature verification in the other. A TEST-mode run therefore cannot consume a production authorization.
   - A TEST receipt or anchor cannot verify as a PRODUCTION one, whatever the state is named.
5. **No TEST evidence as a production claim.**
   - `is-valid` and `verify-receipts` default to **PRODUCTION-only**. A TEST-mode child, receipt or anchor returns `NOT_VALID` with reason "TEST-mode evidence", unless the caller passes `--accept-test-mode`. That output is then labelled `evidence_class: TEST`.
   - `VALID_ANCHORED` in PRODUCTION also requires the off-host pin to say `PRODUCTION`.
   - The `--require-strict-time` option from this spec's first revision is dropped. Strictness is the default and is set by the state's mode, not a verifier choice.
6. **Scope of this item.**
   - All A5.3 work uses TEST keys.
   - The tests create PRODUCTION-mode states only in temporary directories, to prove the refusals. Those states are deleted after each test and are never pinned, published or reused.
   - No PRODUCTION state is created outside tests until the live trial (#34).

## New tests (T23–T29)

- **T23 signed chain.**
  - Every receipt verifies.
  - Editing a receipt and rebuilding every later `prev_sha256` and the head without the key → `verify-chain` BROKEN and apply `REFUSED_CHAIN_BROKEN`. This exact rewrite passes `verify-chain` under A5.2; the test shows it does.
  - Re-signing the whole log with a new key and swapping that key into the policy → passes locally but fails against an anchor published before the swap.
- **T23b independent anchor.** Re-sign the whole log with a new key, swap the policy **and** rewrite the local anchor files to match. The local `verify-chain` and `is-valid` without an external key → OK / `VALID_LOCAL` only. `verify-receipts` and `is-valid` with the original out-of-band key (or its SHA-256) → BROKEN / `NOT_VALID`. This pins the boundary: without evidence held off the host, a full rewrite is not detectable.
- **T24 key hygiene.** Each of these → `REFUSED_MISCONFIGURED`, with no receipt written: key file mode 0640; key file is a symlink; policy public key ≠ private key; receipt key also holds `patch_approver`; receipt key missing from the policy; `host_unverified_TEST` without test hooks.
- **T25 third-party verification.** `verify-receipts` with the right public key → OK. With the wrong key → BROKEN. A tampered anchor signature → BROKEN. An anchor with a mismatched key fingerprint → BROKEN.
- **T26 revert authorization.**
  - Each of these → `DENIED`, nonce untouched, child still `VALID_LOCAL`: no request; unsigned request; a key with `patch_approver` only; wrong state; wrong child hash; wrong receipt seq or line hash; requester principal ≠ key principal; expired request.
  - A valid request → `REVERTED`, with the request hash and authorizer in the receipt.
  - The same request again → `DENIED` (already reverted).
  - Its nonce reused for a different child → `DENIED` (replay).
- **T26b revert window.** `not_after_utc - issued_at_utc` above the limit → `DENIED`; `issued_at_utc` in the future → `DENIED`.
- **T27 revert crash.**
  - SIGKILL mid-write → recovery writes `CRASHED` (`completed: false`), the nonce is `BURNED_CRASH`, there is no `REVERTED` receipt and no restored file, the child is still `VALID_LOCAL`, and `is-valid` does not say "reverted".
  - SIGKILL after the `REVERTED` receipt (new hook `after_revert_receipt`) → recovery writes no `CRASHED`, the nonce is `USED`, and the child is `NOT_VALID` (reverted).
  - A fresh request after the first case reverts the child.
- **T28 trusted time.** Each of these → `DENIED` before reservation, nonce not burned: unsynced kernel; `maxerror` above the limit; clock 60 s behind the recorded floor. An approval expiring within `maxerror` → `DENIED` "approval expired". Every receipt carries `time_evidence`.
- **T28b mode isolation.**
  - `init` without `--mode` → refused.
  - A PRODUCTION state whose config has `test_hooks_enabled`, `fake_timex` or `host_unverified_TEST` → `REFUSED_MISCONFIGURED` at start, even when its ID starts with `TEST-`.
  - A TEST state promoted by editing its mode file → refused (disagrees with the genesis receipt). The same with the config edited → refused. The same with the genesis receipt also rewritten → chain or signature broken, and the out-of-band pin check fails.
  - An approval and a revert request signed under the PRODUCTION domain, submitted to a TEST state → `DENIED` (invalid signature). The reverse → `DENIED`.
  - A TEST child: `is-valid` → `NOT_VALID` "TEST-mode evidence"; with `--accept-test-mode` → `VALID_*` with `evidence_class: TEST`. `verify-receipts` on a TEST log without the flag → BROKEN.
  - A PRODUCTION state on this host (strict time, real kernel) → apply `DENIED` "trusted time unavailable", nonce untouched.
- **T28c strict policy on the real kernel.** A TEST-mode state with `kernel_synced`, no hooks and no `fake_timex` reads this host's real clock. The test asserts the outcome matches the real reading: `DENIED` "trusted time unavailable" when the kernel reports `STA_UNSYNC`. The raw reading is recorded in the results.
- **T29 time lost at commit.** Sync is lost between ingest and commit (via the `before_commit` hook) → `DENIED`, nonce burned, no child.
- **Re-run.** All 37 A5/A5.1/A5.2 tests. Fixtures gain a TEST receipt key, a `revert_authorizer` TEST key and `fake_timex`. Assertions changed only where A5.3 intends it: revert now needs a request, and fixtures are created with `--mode TEST`; signing domains change to the mode-specific ones. Each such change is listed in the results.
- **Real-model check, in two separately reported parts:**
  - **(a) Strict.** `kernel_synced` on the real checkpoint. The expected result on this host is a refusal ("trusted time unavailable") with the raw `adjtimex` values. This is reported as the strict-policy result.
  - **(b) TEST time.** `host_unverified_TEST`: apply → `VALID_ANCHORED` (signed anchor, out-of-band key) → authorized revert → parent `04b1c301…4696`. Labelled `TEST_TIME`. It shows the signing, anchoring and revert path work on the real model. It is **not** evidence about trusted-time enforcement.
