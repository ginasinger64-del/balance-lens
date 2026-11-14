#!/usr/bin/env python3
"""Case 001 v0.3: signed Phase-2 pre-apply authorization gate.

This is a testable demo gate, NOT a patch application engine. A trusted runner
must pin these exact checked artifacts through application, recording the
checkpoint child; otherwise a later substitution remains possible.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from verify_patch import verify

DOMAIN = b"BALANCE-LENS-CASE001-PHASE2-APPROVAL/v0.3\x00"
HEX64 = re.compile(r"^[a-f0-9]{64}$")


class Denied(Exception):
    """Expected deny result; returned as structured JSON instead of traceback."""


def no_duplicate_keys(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise Denied(f"duplicate JSON key: {k}")
        d[k] = v
    return d


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def require_equal(found, expected, name):
    if found != expected:
        raise Denied(f"{name}: mismatch")


def require_hex64(value, name):
    if not isinstance(value, str) or HEX64.fullmatch(value) is None:
        raise Denied(f"{name}: expected lowercase SHA-256 hex")


def verify_signature(envelope, policy, proposer):
    if set(envelope) != {"approval", "key_id", "algorithm", "signature_b64"}:
        raise Denied("signed envelope: incorrect fields")
    if envelope.get("algorithm") != "Ed25519":
        raise Denied("signature algorithm not Ed25519")
    key_id = envelope.get("key_id")
    matches = [k for k in policy.get("trusted_signers", []) if k.get("key_id") == key_id]
    if len(matches) != 1:
        raise Denied("signer key is absent or duplicated in the trusted policy")
    key = matches[0]
    if not key.get("enabled", False) or "patch_approver" not in key.get("roles", []):
        raise Denied("signer lacks patch_approver role")
    principal = key.get("principal")
    if not principal or principal == proposer:
        raise Denied("approval not independent of proposer")
    if envelope["approval"].get("approver_principal") != principal:
        raise Denied("claimed approver does not match trusted policy")
    try:
        raw_public = base64.b64decode(key["public_key_b64"], validate=True)
        sig = base64.b64decode(envelope["signature_b64"], validate=True)
        Ed25519PublicKey.from_public_bytes(raw_public).verify(sig, DOMAIN + canonical(envelope["approval"]))
    except (InvalidSignature, ValueError, KeyError, TypeError) as exc:
        raise Denied("invalid independent approval signature") from exc
    return principal


def enforce_payload_types(patch_dir):
    manifest = load_json(patch_dir / "manifest.json")
    tensors = manifest.get("tensors")
    if not isinstance(tensors, list) or not tensors:
        raise Denied("patch manifest has no tensor list")
    expected = set()
    with np.load(patch_dir / "edit.npz", allow_pickle=False) as z:
        for i, _ in enumerate(tensors):
            ik, vk = f"t{i}_idx", f"t{i}_val"
            expected.update((ik, vk))
            if ik not in z.files or vk not in z.files:
                raise Denied(f"missing payload arrays for tensor {i}")
            idx, val = z[ik], z[vk]
            if idx.dtype != np.dtype("<i8") or idx.ndim != 1:
                raise Denied(f"{ik}: require flat signed int64 array, no coercion")
            if val.dtype != np.dtype("<u2") or val.ndim != 1:
                raise Denied(f"{vk}: require flat uint16 BF16 bit-pattern array, no coercion")
        if set(z.files) != expected:
            raise Denied("unexpected or duplicate payload arrays")


def consume_nonce_sqlite(db_path, scope_nonce, approval_nonce, approval_digest, run_id):
    """Concurrent-safe one-time reservation; no external application performed."""
    db_path = Path(db_path)
    if db_path.is_symlink():
        raise Denied("nonce DB may not be a symlink")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=15, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("CREATE TABLE IF NOT EXISTS consumed (scope_nonce TEXT UNIQUE NOT NULL, approval_nonce TEXT PRIMARY KEY, approval_sha256 TEXT NOT NULL, run_id TEXT NOT NULL)")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("INSERT INTO consumed(scope_nonce, approval_nonce, approval_sha256, run_id) VALUES(?,?,?,?)", (scope_nonce, approval_nonce, approval_digest, run_id))
            conn.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            conn.execute("ROLLBACK")
            raise Denied("approval nonce replay") from exc
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def check_gate(args):
    patch_dir = Path(args.patch_dir).resolve(strict=True)
    scope_path = Path(args.scope).resolve(strict=True)
    base_path = Path(args.base_model).resolve(strict=True)
    signed_path = Path(args.signed_approval).resolve(strict=True)
    policy_path = Path(args.trust_policy).resolve(strict=True)
    db_raw = Path(args.nonce_db)
    if db_raw.is_symlink():
        raise Denied("nonce DB may not be a symlink")
    db_path = db_raw.resolve()
    if policy_path == signed_path or policy_path == scope_path or policy_path.is_relative_to(patch_dir):
        raise Denied("trusted policy must be independent of agent-controlled artifacts")
    if db_path.is_relative_to(patch_dir):
        raise Denied("nonce DB must be outside the agent-controlled patch directory")

    scope = load_json(scope_path)
    envelope = load_json(signed_path)
    policy = load_json(policy_path)
    if policy.get("schema_version") != "0.3":
        raise Denied("wrong trusted-policy schema")
    approval = envelope.get("approval")
    if not isinstance(approval, dict):
        raise Denied("missing signed approval")
    s = scope.get("authorization", {})
    if scope.get("manifest_timing") != "PROSPECTIVE" or approval.get("manifest_timing") != "PROSPECTIVE":
        raise Denied("retrospective scope is not eligible for prospective application")
    if approval.get("phase") != "POST_SUBMISSION_PATCH_APPROVAL":
        raise Denied("wrong approval phase")
    proposer = approval.get("proposer_principal")
    if not isinstance(proposer, str) or not proposer:
        raise Denied("missing proposer identity")
    approver = verify_signature(envelope, policy, proposer)
    require_equal(approval.get("case_id"), scope.get("case_id"), "case_id")
    require_equal(approval.get("scope_grant_sha256"), sha256_file(scope_path), "scope hash")
    require_equal(approval.get("scope_nonce"), s.get("nonce"), "scope nonce")
    require_equal(approval.get("run_id"), s.get("run_id"), "scope run ID")
    require_equal(approval.get("authorization_epoch"), s.get("authorization_epoch"), "epoch")
    require_equal(approval.get("parent_revision"), s.get("parent_revision"), "parent revision")
    require_equal(args.observed_parent_revision, s.get("parent_revision"), "observed parent revision")
    require_equal(approval.get("parent_model_repo"), s.get("parent_model_repo"), "parent repository")
    require_hex64(approval.get("parent_checkpoint_sha256"), "parent checkpoint")
    require_equal(approval["parent_checkpoint_sha256"], sha256_file(base_path), "parent checkpoint bytes")
    run_id = approval.get("run_id")
    nonce = approval.get("approval_nonce")
    if not isinstance(run_id, str) or not run_id or not isinstance(nonce, str) or not nonce:
        raise Denied("run ID and approval nonce are mandatory")
    if nonce == s.get("nonce"):
        raise Denied("scope and approval nonces must be distinct")
    p = approval.get("patch")
    if not isinstance(p, dict):
        raise Denied("missing patch binding")
    for name, file in (("manifest_sha256", patch_dir / "manifest.json"), ("payload_sha256", patch_dir / "edit.npz")):
        require_hex64(p.get(name), name)
        require_equal(p[name], sha256_file(file), name)

    # Canonical raw encodings must be checked before calling legacy v0.2 code:
    # the original verifier casts index dtype, accepting some malformed arrays.
    enforce_payload_types(patch_dir)

    # Legacy v0.2 approved verifier expects a plain approval JSON path.
    # Do NOT write an untrusted approval file to disk; instead execute the same
    # deterministic verifier checks with a temporary trusted copy.
    import tempfile
    with tempfile.TemporaryDirectory(prefix="case001-approved-") as d:
        approval_path = Path(d) / "authenticated_approval.json"
        approval_path.write_bytes(canonical(approval))
        checks = verify(SimpleNamespace(
            patch_dir=str(patch_dir), scope=str(scope_path), approval=str(approval_path),
            mode="approved", parent_revision=args.observed_parent_revision,
            base_model=str(base_path), nonce_db=None, consume_nonce=False,
        ))
    if checks.get("status") != "PASS_FULL" or checks.get("summary", {}).get("failures"):
        failing = [x.get("id") for x in checks.get("checks", []) if x.get("status") == "FAIL"]
        raise Denied(f"approved full-base integrity checks failed: {failing}")

    # Pin patch and model once more immediately before the atomic reservation.
    # A trusted runner must maintain this identity through the actual apply.
    require_equal(p["manifest_sha256"], sha256_file(patch_dir / "manifest.json"), "manifest changed during verification")
    require_equal(p["payload_sha256"], sha256_file(patch_dir / "edit.npz"), "payload changed during verification")
    require_equal(approval["parent_checkpoint_sha256"], sha256_file(base_path), "base model changed during verification")
    require_equal(approval["scope_grant_sha256"], sha256_file(scope_path), "scope changed during verification")
    digest = hashlib.sha256(DOMAIN + canonical(approval)).hexdigest()
    if args.action == "reserve":
        consume_nonce_sqlite(db_path, s["nonce"], nonce, digest, run_id)
        status = "PASS_PREAPPLY_RESERVATION"
    else:
        status = "PASS_PREFLIGHT_NONAPPLY"
    return {
        "status": status,
        "case_id": scope["case_id"],
        "run_id": run_id,
        "approver": approver,
        "approval_sha256": digest,
        "parent_checkpoint_sha256": approval["parent_checkpoint_sha256"],
        "manifest_sha256": p["manifest_sha256"],
        "payload_sha256": p["payload_sha256"],
        "nonce_reserved": args.action == "reserve",
        "checkpoint_applied": False,
        "note": "This check does not itself apply the patch. Trusted runner must use the same pinned bytes and record child identity.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--patch-dir", required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--signed-approval", required=True)
    parser.add_argument("--trust-policy", required=True)
    parser.add_argument("--base-model", required=True)
    parser.add_argument("--observed-parent-revision", required=True)
    parser.add_argument("--nonce-db", required=True)
    parser.add_argument("--action", choices=("preflight", "reserve"), default="preflight")
    args = parser.parse_args()
    try:
        output = check_gate(args)
    except (Denied, OSError, ValueError, KeyError, TypeError, IndexError, sqlite3.Error) as exc:
        output = {"status": "DENY", "reason": str(exc), "checkpoint_applied": False}
    print(json.dumps(output, sort_keys=True, indent=2))
    return 0 if output["status"].startswith("PASS_") else 1


if __name__ == "__main__":
    sys.exit(main())
