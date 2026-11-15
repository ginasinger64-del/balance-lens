# Case 002 — A5 trusted apply-and-receipt service: specification

**Written:** 2026-11-14, before any A5 service code or test was written. Pushed to branch `case002-a5-runner` first, so the commit timestamp anchors the spec ahead of results.
**Scope:** build and test the trusted apply service only. No live agent trial. No signing of a real edit with Gina Singer's key: all tests use throwaway keys labelled TEST. Nothing merged.
**Reuse:** adapted from Case 001 v0.3 (`approval_gate.py`, `sign_approval.py` @ `1bf50296`): canonical JSON, duplicate-key rejection, Ed25519 envelope check against a trust policy, proposer ≠ approver, strict payload typing (int64 indices, uint16 BF16 bits), sqlite one-time nonce reservation. The A5 signing domain is new (`BALANCE-LENS-CASE002-A5-APPLY/v0.1`), so a Case 001 signature cannot be replayed into A5.
**Patch format:** Case 001 form, `manifest.json` + `edit.npz` (`t{i}_idx` int64 flat indices, `t{i}_val` uint16 BF16 bit patterns).

## Requirements

R1. **Exact signed approval.** Apply only on an Ed25519-signed approval from a key in the service-held trust policy with role `patch_approver`. The approval binds: one patch (manifest SHA-256 + payload SHA-256), one parent (full-checkpoint SHA-256), one approver principal, the proposer principal (must differ from the approver), case/run/epoch, grant SHA-256, scope nonce, a distinct approval nonce, and the claimed child-tensor SHA-256s. Missing, malformed, unknown-key, wrong-role or bad-signature approvals are denied.

R2. **Private copy, re-hash, apply from the copy.** The service copies the parent, manifest, payload and signed approval from the staging area (opened without following symlinks) into a per-request directory that only the service user can access. It re-hashes the copies there. Every later step reads only those copies. Changes to the staging area after ingest cannot affect the result.

R3. **Independent scope check.** The service checks the patch against its own copy of the frozen grant, regardless of the signature. It checks: exact tensor set, names, shapes and BF16 dtype (from the parent header), index range, no duplicate indices, columns ⊆ allowed columns, entries ≤ max per tensor, number of tensors ≤ max, parent tensor hashes = grant `base_tensor_sha256`, and that the payload is consistent with the manifest. The computed child-tensor hashes must equal the approval's claimed hashes.

R4. **Write, verify, then mark valid.** The child is written to a `.partial` file and fsynced, then re-read from disk.
- The re-read must match the expected full-file SHA-256 and the claimed tensor hashes.
- It is diffed bytewise against the parent copy: bytes outside the permitted tensors must be identical, and changed entries inside them must be ⊆ the approved indices.
- Only then is the child renamed atomically to its final name and the directory fsynced.
- Then an `APPLIED` receipt is written. A child counts as valid only if a verified receipt chain contains an `APPLIED` receipt with its hash.

R5. **Receipts for every outcome.** An append-only receipt log records `APPLIED`, `DENIED`, `CRASHED` and `ERROR` events.
- Each receipt has a sequence number, the SHA-256 of the previous receipt, and the request's identifying hashes.
- A head file holds the latest sequence number and hash.
- Chain verification detects an edited receipt, a deleted middle receipt and a truncated tail (log vs head).
- *Known limit, stated in advance:* an actor with service-user privileges who rewrites both the log and the head consistently is not detectable without an external anchor, such as publishing the head hash.

R6. **One-time use.** The approval nonce is reserved atomically (sqlite, `BEGIN IMMEDIATE`, UNIQUE) before any child is written.
- A reserved or used nonce can never apply again.
- A nonce reserved for an attempt that then crashed or errored is burned and needs a fresh approval.
- Denials before reservation do not burn the nonce.
- Applies are serialised by an exclusive service lock (`flock`), which the kernel releases if the process dies.

R7. **Revocation.** A service-held revocation list (by approval nonce or approval SHA-256) is checked at ingest and again immediately before the child is marked valid.

R8. **Fail closed and recover.** Before writing, the service records an in-flight journal entry (fsynced). At startup, under the lock, any journal left without a final receipt is treated as a crash: the partial or unreceipted child is deleted, the nonce is burned, and a `CRASHED` receipt is written. Write errors (e.g. ENOSPC) take the same path with an `ERROR` receipt. A partial or unreceipted child is never valid.

R9. **OS separation.** Three OS users: signer (owns the private key, mode 0600), service (owns state, config, nonce DB, receipts and children, mode 0700), agent (owns only the staging area). The agent cannot read the key, and cannot write or replace the checkpoint, grant, trust policy, revocation list, nonce DB or receipts. The OS enforces this, not the service. Root or the host administrator is trusted and out of scope.

R10. **Test hooks.** Fault-injection hooks (pause, swap, kill points) run only if the service's own admin-held config enables them. Environment variables alone cannot enable them. Production config has them off.

## Test list

Synthetic checkpoints first (small BF16 safetensors with the same two tensor names, a synthetic grant and TEST keys). The real Qwen parent is used only after all synthetic tests pass.

1. **Successful apply.** Valid approval + in-scope patch → `APPLIED`. The child exists, its hash matches the receipt, `is_valid_child` is true, and the chain verifies.
2. **Denial.** Missing approval, malformed envelope, bad signature, unknown key, wrong role, approver = proposer, wrong grant hash → `DENIED` receipt each time. No child is written and the nonce is not burned.
3. **Tampered patch or parent.** A payload, manifest or parent byte changed after signing → `DENIED` (hash mismatch). No child.
4. **Files swapped between check and apply.** After ingest, the staging patch and parent are replaced with different valid-looking files. The result is still the approved child, built from the private copies, not the swapped files. A swap before ingest → `DENIED`.
5. **Valid signature on an out-of-scope patch, or on the wrong parent.** Approvals with valid signatures on patches that touch a non-permitted column, exceed the entry budget, touch a non-permitted tensor, use non-BF16 / wrong-typed payload arrays, or name the wrong parent → `DENIED`.
6. **Replayed approval.** A second apply with the same approval after a success → `DENIED` (nonce used).
7. **Revoked approval.** Revoked before apply → `DENIED`. Revoked during apply (hook, before commit) → not marked valid: no `APPLIED` receipt and the child is removed.
8. **Two requests at once for the same approval.** Two concurrent processes → exactly one `APPLIED` and one `DENIED`; one valid child.
9. **Process killed mid-apply, then recovery.** SIGKILL during the child write (and again after the rename but before the receipt). Before recovery, no child is valid. Recovery writes `CRASHED`, removes the partial or unreceipted child and burns the nonce. Re-using the same approval → `DENIED`.
10. **Disk full during write.** Children directory on a small tmpfs → `ERROR` receipt, no partial left, nonce burned, chain still verifies.
11. **Edited or deleted past receipt.** Editing a middle receipt, deleting a middle receipt and truncating the tail are each detected by chain verification. `is_valid_child` refuses while the chain is broken.
12. **OS-enforced separation.** As the agent user: reading the signing key, writing or replacing a child checkpoint, and writing the grant, trust policy, revocation list, nonce DB or receipts all fail with EACCES/EPERM from the OS. A full apply run as the service user from the agent's staging area succeeds.

Then: **real-model check.** Apply the B4 attempt-4 FEASIBILITY_ONLY patch to the verified Qwen parent through the service, with a TEST key. The child's two tensor SHA-256s should equal B4's recorded `1e7d3646…700d` / `2080fb4c…1080`. This is not a signed approval of a real edit.

## Also on this branch
Fix the B4 loading issue: split `b4lib.py` so `edit_b4.py` never imports the neighbour-check items, and correct the wording that said they were "never loaded". In attempts 2–4 they were loaded into memory through the shared import but used in no loss.
