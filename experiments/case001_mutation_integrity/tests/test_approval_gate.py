"""Known-answer synthetic authorization checks, no actual model editing agent."""
from __future__ import annotations
import base64
import copy
import hashlib
import json
import sqlite3
import struct
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from approval_gate import DOMAIN, canonical, sha256_file
from verify_patch import git_blob_sha1


def dump(path, obj):
    Path(path).write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")


def one_tensor(name, b):
    return {"name": name, "shape": list(b.shape), "base_tensor_sha256": hashlib.sha256(b.tobytes()).hexdigest(), "allowed_columns": [1], "max_targeted_entries": 2, "max_columns": 1}


def safetensors(path, tensors):
    offset = 0
    header = {}
    chunks = []
    for name, arr in tensors.items():
        raw = arr.tobytes()
        header[name] = {"dtype": "BF16", "shape": list(arr.shape), "data_offsets": [offset, offset + len(raw)]}
        chunks.append(raw)
        offset += len(raw)
    h = json.dumps(header, separators=(",", ":")).encode()
    path.write_bytes(struct.pack("<Q", len(h)) + h + b"".join(chunks))


@pytest.fixture
def fixture_case(tmp_path):
    agent = tmp_path / "agent_workspace"
    agent.mkdir()
    patch_dir = agent / "patch"
    patch_dir.mkdir()
    trusted = tmp_path / "trusted_runner"
    trusted.mkdir()
    # Different actor identities, trusted policy owned by runner.
    approver = Ed25519PrivateKey.generate()
    attacker = Ed25519PrivateKey.generate()
    pub = approver.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    policy = {"schema_version": "0.3", "trusted_signers": [{"key_id": "reviewer-key", "principal": "independent-reviewer", "enabled": True, "roles": ["patch_approver"], "public_key_b64": base64.b64encode(pub).decode()}]}
    policy_path = trusted / "trust_policy.json"
    dump(policy_path, policy)

    bases = {"tensor.a": np.array([[5, 6, 7], [8, 9, 10]], dtype=np.uint16), "tensor.b": np.array([[11, 12, 13], [14, 15, 16]], dtype=np.uint16)}
    base_model = trusted / "base.safetensors"
    safetensors(base_model, bases)
    scope = {"schema_version": "0.3", "case_id": "WSP-CASE-001-SYNTH", "manifest_timing": "PROSPECTIVE", "phase": "PRE_EDIT_SCOPE_GRANT", "authorization": {"parent_model_repo": "synthetic/test", "parent_revision": "trusted-rev-1", "authorization_epoch": "epoch-01", "run_id": "synthetic-run-01", "nonce": "scope-001", "max_tensors": 2, "require_exact_tensor_set": True, "allowed_dtype": "bfloat16", "allowed_forms": ["sparse"], "tensors": [one_tensor(n, arr) for n, arr in bases.items()]}}
    scope_path = agent / "scope_grant.json"
    dump(scope_path, scope)

    idx = np.array([1], dtype=np.int64)
    vals = {"tensor.a": np.array([66], dtype=np.uint16), "tensor.b": np.array([77], dtype=np.uint16)}
    np.savez(patch_dir / "edit.npz", t0_idx=idx, t0_val=vals["tensor.a"], t1_idx=idx, t1_val=vals["tensor.b"])
    manifest = {"tensors": []}
    approved_tensors = []
    for name, arr in bases.items():
        edited = arr.copy().reshape(-1)
        edited[1] = vals[name][0]
        edited_hash = hashlib.sha256(edited.tobytes()).hexdigest()
        manifest["tensors"].append({"name": name, "shape": list(arr.shape), "form": "sparse", "dtype": "bfloat16", "changed_entries": 1, "changed_columns": 1, "changed_rows": 1, "base_sha256": hashlib.sha256(arr.tobytes()).hexdigest(), "edited_sha256": edited_hash})
        approved_tensors.append({"name": name, "expected_changed_entries": 1, "edited_tensor_sha256": edited_hash})
    manifest_path = patch_dir / "manifest.json"
    dump(manifest_path, manifest)
    approval = {"schema_version": "0.3", "case_id": scope["case_id"], "manifest_timing": "PROSPECTIVE", "phase": "POST_SUBMISSION_PATCH_APPROVAL", "scope_grant_sha256": sha256_file(scope_path), "scope_nonce": scope["authorization"]["nonce"], "authorization_epoch": scope["authorization"]["authorization_epoch"], "parent_revision": scope["authorization"]["parent_revision"], "parent_model_repo": scope["authorization"]["parent_model_repo"], "parent_checkpoint_sha256": sha256_file(base_model), "run_id": "synthetic-run-01", "proposer_principal": "agent-proposer", "approver_principal": "independent-reviewer", "approval_nonce": "approval-001", "patch": {"manifest_sha256": sha256_file(manifest_path), "payload_sha256": sha256_file(patch_dir / "edit.npz"), "manifest_git_blob_sha1": git_blob_sha1(manifest_path.read_bytes()), "payload_git_blob_sha1": git_blob_sha1((patch_dir / "edit.npz").read_bytes()), "tensors": approved_tensors}}
    envelope_path = trusted / "signed_approval.json"
    def sign(data=None, key=None, key_id="reviewer-key"):
        obj = copy.deepcopy(data if data is not None else approval)
        used_key = key if key is not None else approver
        sig = used_key.sign(DOMAIN + canonical(obj))
        dump(envelope_path, {"approval": obj, "key_id": key_id, "algorithm": "Ed25519", "signature_b64": base64.b64encode(sig).decode()})
    sign()
    def cli(action="preflight", extra=()):
        cmd = [sys.executable, str(ROOT / "approval_gate.py"), "--patch-dir", str(patch_dir), "--scope", str(scope_path), "--signed-approval", str(envelope_path), "--trust-policy", str(policy_path), "--base-model", str(base_model), "--observed-parent-revision", "trusted-rev-1", "--nonce-db", str(trusted / "nonce.sqlite3"), "--action", action, *extra]
        p = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))
        try: body = json.loads(p.stdout)
        except json.JSONDecodeError: body = {"status": "HARNESS_ERROR", "stderr": p.stderr}
        return p.returncode, body
    return dict(cli=cli, sign=sign, approval=approval, attacker=attacker, patch_dir=patch_dir, scope_path=scope_path, base_model=base_model, policy_path=policy_path, envelope_path=envelope_path)


def test_valid_preflight_requires_full_base(fixture_case):
    code, obj = fixture_case["cli"]()
    assert code == 0, obj
    assert obj["status"] == "PASS_PREFLIGHT_NONAPPLY" and obj["checkpoint_applied"] is False


def test_valid_reservation_and_replay(fixture_case):
    code, obj = fixture_case["cli"]("reserve")
    assert code == 0 and obj["status"] == "PASS_PREAPPLY_RESERVATION", obj
    code, obj = fixture_case["cli"]("reserve")
    assert code != 0 and obj["status"] == "DENY" and "replay" in obj["reason"], obj


def test_concurrent_single_use(fixture_case):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: fixture_case["cli"]("reserve"), range(2)))
    assert sum(code == 0 for code, obj in results) == 1, results
    assert sum(code != 0 for code, obj in results) == 1, results


def test_altered_value_after_approval_denied(fixture_case):
    patch = fixture_case["patch_dir"] / "edit.npz"
    with np.load(patch) as z:
        arrays = {k: z[k].copy() for k in z.files}
    arrays["t0_val"][0] += np.uint16(1)
    np.savez(patch, **arrays)
    code, obj = fixture_case["cli"]()
    assert code != 0 and "payload_sha256" in obj["reason"], obj


def test_attacker_self_issued_replacement_approval_denied(fixture_case):
    altered = copy.deepcopy(fixture_case["approval"])
    # A new approval may match the exact patch. An agent with its own key still cannot authorize it.
    altered["approver_principal"] = "agent-proposer"
    fixture_case["sign"](altered, key=fixture_case["attacker"], key_id="attacker-key")
    code, obj = fixture_case["cli"]()
    assert code != 0 and "signer key" in obj["reason"], obj


def test_tampered_signed_approval_denied(fixture_case):
    obj = json.loads(fixture_case["envelope_path"].read_text())
    obj["approval"]["parent_revision"] = "different-parent"
    dump(fixture_case["envelope_path"], obj)
    code, body = fixture_case["cli"]()
    assert code != 0 and "signature" in body["reason"], body


def test_mode_downgrade_unavailable(fixture_case):
    code, obj = fixture_case["cli"](extra=["--mode", "scope"])
    assert code != 0 and obj["status"] == "HARNESS_ERROR", obj


def test_noncanonical_arrays_rejected_even_if_reapproved(fixture_case):
    patch = fixture_case["patch_dir"] / "edit.npz"
    with np.load(patch) as z:
        arrays = {k: z[k].copy() for k in z.files}
    arrays["t0_idx"] = arrays["t0_idx"].astype(np.float64)
    arrays["t0_val"] = arrays["t0_val"].astype(np.float64)
    np.savez(patch, **arrays)
    altered = copy.deepcopy(fixture_case["approval"])
    altered["patch"]["payload_sha256"] = sha256_file(patch)
    altered["patch"]["payload_git_blob_sha1"] = git_blob_sha1(patch.read_bytes())
    fixture_case["sign"](altered)
    code, obj = fixture_case["cli"]()
    assert code != 0 and "signed int64" in obj["reason"], obj


def test_retrospective_scope_not_eligible(fixture_case):
    scope = json.loads(fixture_case["scope_path"].read_text())
    scope["manifest_timing"] = "RETROSPECTIVE_DEMO"
    dump(fixture_case["scope_path"], scope)
    altered = copy.deepcopy(fixture_case["approval"])
    altered["manifest_timing"] = "RETROSPECTIVE_DEMO"
    altered["scope_grant_sha256"] = sha256_file(fixture_case["scope_path"])
    fixture_case["sign"](altered)
    code, obj = fixture_case["cli"]("reserve")
    assert code != 0 and "retrospective" in obj["reason"], obj


def test_wrong_parent_checkpoint_denied(fixture_case):
    parent = fixture_case["base_model"]
    parent.write_bytes(parent.read_bytes() + b"changed")
    code, obj = fixture_case["cli"]()
    assert code != 0 and "parent checkpoint" in obj["reason"], obj


def test_missing_base_checkpoint_fails_closed(fixture_case):
    fixture_case["base_model"].unlink()
    code, obj = fixture_case["cli"]()
    assert code != 0 and obj["status"] == "DENY", obj


def test_same_scope_nonce_cannot_authorize_second_approval(fixture_case):
    code, first = fixture_case["cli"]("reserve")
    assert code == 0, first
    modified = copy.deepcopy(fixture_case["approval"])
    modified["approval_nonce"] = "approval-002"
    fixture_case["sign"](modified)
    code, obj = fixture_case["cli"]("reserve")
    assert code != 0 and "replay" in obj["reason"], obj


def test_failed_approval_does_not_consume_nonce(fixture_case):
    modified = copy.deepcopy(fixture_case["approval"])
    modified["parent_checkpoint_sha256"] = "0" * 64
    fixture_case["sign"](modified)
    code, obj = fixture_case["cli"]("reserve")
    assert code != 0, obj
    fixture_case["sign"]()
    code, obj = fixture_case["cli"]("reserve")
    assert code == 0 and obj["nonce_reserved"], obj


def test_approver_must_differ_from_proposer(fixture_case):
    modified = copy.deepcopy(fixture_case["approval"])
    modified["proposer_principal"] = "independent-reviewer"
    fixture_case["sign"](modified)
    code, obj = fixture_case["cli"]()
    assert code != 0 and "independent" in obj["reason"], obj


def test_scope_run_id_mismatch_rejected(fixture_case):
    modified = copy.deepcopy(fixture_case["approval"])
    modified["run_id"] = "unrelated-run"
    fixture_case["sign"](modified)
    code, obj = fixture_case["cli"]()
    assert code != 0 and "scope run ID" in obj["reason"], obj
