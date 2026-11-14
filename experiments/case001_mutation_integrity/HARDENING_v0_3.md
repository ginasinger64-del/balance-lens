# Case 001 v0.3 — authenticated pre-apply approval gate

## What this adds (and does not add)

This is an **append-only hardening extension** to the unmodified Case 001 v0.2 retrospective archive. Original `verify_patch.py`, `scope_grant.json`, `patch_approval.json`, specification and 20 checksum-covered files remain byte-identical. No existing Case 001 outcome is relabeled as a prospective success.

The new `approval_gate.py` provides a **separate trusted-runner interface** for a prospective experiment. It deliberately has **no `--mode` argument** and never accepts `scope` or `semantic-diagnostic` verification outcomes as application permits. Instead it requires:

1. A scope grant marked `PROSPECTIVE`, frozen and made available by the trusted controller before the editor runs.
2. A signed Phase-2 approval issued *after patch submission*, verified with Ed25519 against a separately administered trust policy. The editing agent never holds the private key or controls the trusted public-key policy.
3. A signature binding the exact **scope SHA-256**, case/run/epoch, scope and approval nonces, parent repository/revision, **actual full parent-checkpoint SHA-256**, patch manifest and payload SHA-256, and approved child-tensor hash claims.
4. The exact patch to pass the original v0.2 `approved` checks with the actual BF16 base `.safetensors` file available, yielding `PASS_FULL`. A prebase-only result cannot pass this gate. The gate additionally rejects noncanonical `.npz` array types rather than coercing them.
5. Single-use **scope nonce and approval nonce** reserved atomically in SQLite using a write transaction and uniqueness constraints. A failed verification cannot consume either nonce. Two concurrent reservations yield one successful reservation at most.

`preflight` returns `PASS_PREFLIGHT_NONAPPLY` and does not consume a nonce. `reserve` returns `PASS_PREAPPLY_RESERVATION` after committing the nonce reservation. **Neither status means the edited checkpoint has been applied.** The gate never applies or saves edited model weights.

## Trust model and deliberate limits

- `--trust-policy`, `--nonce-db`, the parent-checkpoint file and CLI arguments **must be controlled by a trusted runner, not by the patch-proposing agent**. Local public-key lookup alone is not independence if an agent can swap the trust policy; separation must be enforced by process credentials / storage permissions / service boundaries.
- The signing helper signs an approval JSON, but **does not decide whether the mutation deserves approval**. Independent review/approval policy is external. Possession of the trusted private key is treated as the authority. Do not put any private keys or live policy credentials in the repository.
- Cryptographic verification does not prove that the Phase-1 scope was frozen before editing, only that Phase-2 binds the scope bytes it authenticates. A live prospective trial must record and attest that earlier event independently.
- SHA-256 hashes bind whole parent-checkpoint file bytes; original `verify_patch.py` additionally checks allowed base tensors and calculated edited tensors. The gate checks the files twice, but it does **not** itself preserve an immutable file handle across a subsequent external model-application process. A real apply runner must hold immutable checked inputs, revalidate the same hashes at application, atomically record the final child identity and apply/revert outcome, and control privileges across the transition. Otherwise there remains a check-to-use gap.
- Reserving before application can produce a *consumed nonce without an applied patch* if the process crashes. That's a deliberate fail-closed state, not proof of successful application. A production system needs an explicit reservation/commit/failure/reconciliation lifecycle with durable receipts.
- This does not solve malicious trusted approvers, private-key compromise, DoS, malformed artifact resource exhaustion, system-wide filesystem compromise, model behavior after editing, or prospective agent obedience.
- `scope` and `semantic-diagnostic` remain in the original `verify_patch.py` as **test-only diagnostics**; a deployed apply service must not accept them via other paths.

## Synthetic known-answer tests

```bash
pip install -r requirements.txt
pytest -q tests/test_approval_gate.py
python tests/test_synthetic.py
```

`tests/test_approval_gate.py` constructs tiny synthetic BF16 parent tensors and a sparse patch, with independently generated reviewer and attacker keys and no real model checkpoint. It covers clean approval, tampered signature, self-issued untrusted approval, in-scope post-approval mutation, required base checkpoint, invalid parent bytes, noncanonical float payloads, unauthorized downgrade attempt, retrospective rejection, scope/run mismatch, failed-grant nonconsumption, two-nonce replay and concurrent reservation.

The original v0.2 `tests/test_synthetic.py` remains unchanged. The original upstream public patch/base checkpoint **was not** downloaded or rerun in this hardening extension.

## A prospective integration sequence

1. Trusted controller freezes Phase-1 scope in read-only store before giving it to an editing agent. Record run ID, authorization epoch, parent-checkpoint digest and issuance time independently.
2. Editing agent submits manifest and payload into a quarantine staging location, cannot write reviewer keys/policy or the apply runner.
3. Independent authorized approver verifies the patch request and signs an exact-byte Phase-2 approval after submission. The signed payload must include the fields and tensor claims described above; see `tests/test_approval_gate.py` for synthetic construction.
4. Trusted runner invokes `approval_gate.py --action reserve`, supplying trusted observed-parent and checkpoint file. Only `PASS_PREAPPLY_RESERVATION` is considered a candidate for pre-apply continuation.
5. Trusted runner rechecks immutable artifact identity and performs the application in the same security boundary, then records child artifact hash, nonce disposition and new privilege epoch. **This final apply step is not implemented here.**
6. Run known-answer adversarial editing-agent tests, including self-approval, downgrade, stale privilege, an out-of-scope patch, a post-submission in-scope mutation, and a failed/replayed approval. Separately test downstream misclaims from checkpoints and summaries.

## Example trusted preflight (with actual signed artifacts)

```bash
python approval_gate.py \
  --patch-dir /trusted/staging/patch \
  --scope /trusted/pregrant/scope_grant.json \
  --signed-approval /trusted/review/signed_approval.json \
  --trust-policy /trusted/config/policy.json \
  --base-model /trusted/checkpoints/base.safetensors \
  --observed-parent-revision VERIFIED_PARENT_REV \
  --nonce-db /trusted/state/approvals.sqlite3 \
  --action preflight
```

The original packaged `scope_grant.json` is explicitly `RETROSPECTIVE_DEMO` and **cannot** pass this prospective gate. That's intentional: no authentic retrospective authorization is created by signing it after the fact.

## Claim boundary

**Shown by local synthetic tests:** cryptographically checked approval binding, type checks, distinct diagnostic vs pre-apply paths, concurrency-safe single-use nonce reservations and known-answer rejection of self-approval and downgrade attempts.

**Not shown:** live independent reviewer workflow, complete public model reconstruction in v0.3, prospective editor compliance, fully trusted checkpoint application, privilege rebinding or downstream behavioral assurance. Thus *not* a deployed or independently audited authorization system.
