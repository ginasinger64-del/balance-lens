#!/usr/bin/env python3
"""A5 real-model check (TEST key; FEASIBILITY_ONLY patch; NOT a signed approval of a real edit).

Applies the B4 attempt-4 patch to the verified Qwen3.5-0.8B parent through apply_service.py and checks the child's two
tensor SHA-256s against B4's record. The approval is signed with a throwaway TEST key generated here. The grant is a
TEST_ONLY copy of the Case 001 tensor/column envelope.

usage: real_model_check.py --parent P --case001-grant G --b4-patch NPZ --b4-child CHILD --out DIR
"""
import argparse, hashlib, io, json, os, subprocess, sys, time
from pathlib import Path
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE / "tests"))
import apply_service as svc  # noqa: E402
from a5_testlib import Signer, policy, approval_for, sign_revocations, revoker_signer  # noqa: E402

B4_RECORDED = {"model.language_model.layers.15.mlp.down_proj.weight": "1e7d3646f56e12af0be76877307a7ec5e2a053c97b24cd0c7cb279857ab4700d",
               "model.language_model.layers.19.mlp.down_proj.weight": "2080fb4c2ff2073233cb10e83db886912be9ae573df3a23a3ad2440102681080"}
PARENT_SHA = "04b1c301231dd422b8860db31311ab2721511346a32cb1e079c4c4e5f1fe4696"


def main():
    ap = argparse.ArgumentParser()
    for a in ("--parent", "--case001-grant", "--b4-patch", "--b4-child", "--out"):
        ap.add_argument(a, required=True)
    args = ap.parse_args(); out = Path(args.out); out.mkdir(parents=True, exist_ok=False)
    t0 = time.time()
    parent = Path(args.parent).read_bytes()
    assert hashlib.sha256(parent).hexdigest() == PARENT_SHA, "parent is not the verified Qwen checkpoint"
    c1 = json.loads(Path(args.case001_grant).read_text())["authorization"]
    grant = {"case_id": "TEST-CASE002-A5-REALMODEL", "manifest_timing": "PROSPECTIVE",
             "label": "TEST_ONLY grant for the A5 real-model check; copies the Case 001 tensor/column envelope",
             "authorization": {**{k: c1[k] for k in ("parent_model_repo", "parent_revision", "require_exact_tensor_set", "max_tensors", "allowed_forms", "allowed_dtype", "tensors")},
                               "authorization_epoch": "TEST-A5-REAL-EPOCH-1", "parent_checkpoint_sha256": PARENT_SHA,
                               "service_state_id": "TEST-A5-REAL-STATE-1", "run_id": "TEST-A5-REAL-RUN-1", "nonce": "TEST-A5-REAL-SCOPE-NONCE-1"}}
    # convert the B4 patch to Case 001 manifest + edit.npz form
    z = np.load(args.b4_patch, allow_pickle=False); names = [t["name"] for t in c1["tensors"]]
    arrays, man = {}, {"tensors": [], "source": "B4 attempt 4 patch_sparse.npz (FEASIBILITY_ONLY)"}
    child = bytearray(parent); ptens = svc.parse_safetensors(parent); tsha = {}
    for i, n in enumerate(names):
        idx = z[f"{n}::indices"].astype("<i8"); val = z[f"{n}::values_bf16_bits"].astype("<u2")
        arrays[f"t{i}_idx"] = idx; arrays[f"t{i}_val"] = val
        g = next(t for t in c1["tensors"] if t["name"] == n)
        man["tensors"].append({"name": n, "shape": g["shape"], "dtype": "bfloat16", "form": "sparse", "changed_entries": int(len(idx))})
        s, e = ptens[n]["start"], ptens[n]["end"]; arr = np.frombuffer(parent[s:e], "<u2").copy(); arr[idx] = val
        child[s:e] = arr.tobytes(); tsha[n] = hashlib.sha256(arr.tobytes()).hexdigest()
    b = io.BytesIO(); np.savez(b, **arrays); npz = b.getvalue(); manb = json.dumps(man, indent=1).encode()
    # independent cross-check: tensors in the B4 child file as written by safetensors in B4
    b4c = Path(args.b4_child).read_bytes(); b4t = svc.parse_safetensors(b4c)
    b4_file_tsha = {n: hashlib.sha256(b4c[b4t[n]["start"]:b4t[n]["end"]]).hexdigest() for n in names}
    del b4c
    signer = Signer(principal="TEST-approver-realmodel", key_id="TEST-key-realmodel")
    approval = approval_for(grant, parent, manb, npz, bytes(child), tsha, signer, approval_nonce="TEST-A5-REAL-APPROVAL-NONCE-1",
                            proposer="TEST-reference-edit-B4-attempt4")
    child_sha_expected = hashlib.sha256(child).hexdigest(); del child
    state = out / "state"; staging = out / "staging"; staging.mkdir()
    revoker = revoker_signer()
    svc.init_state(state, grant, policy(signer, revoker), revocations_envelope=sign_revocations(revoker, "TEST-A5-REAL-STATE-1"))
    os.link(os.path.realpath(args.parent), staging / "parent.safetensors")
    (staging / "manifest.json").write_bytes(manb); (staging / "edit.npz").write_bytes(npz)
    (staging / "approval.signed.json").write_text(json.dumps(signer.sign(approval)))
    del parent
    t1 = time.time()
    p = subprocess.run([sys.executable, str(HERE / "apply_service.py"), "apply", "--state", str(state), "--staging", str(staging)],
                       capture_output=True, text=True)
    res = json.loads(p.stdout.splitlines()[-1]); t_apply = time.time() - t1
    def svc_cli(*a):
        return json.loads(subprocess.run([sys.executable, str(HERE / "apply_service.py"), *map(str, a)], capture_output=True, text=True).stdout.splitlines()[-1])
    cp = res.get("child_path", "/nonexistent")
    valid_local = svc_cli("is-valid", "--state", state, "--child", cp)["status"]
    anchor = out / "anchor.jsonl"; anchor.write_text(json.dumps(svc_cli("head", "--state", state)) + "\n")
    valid = svc_cli("is-valid", "--state", state, "--child", cp, "--anchor", anchor)["status"]
    t2 = time.time(); rv = svc_cli("revert", "--state", state, "--child", cp); t_revert = time.time() - t2
    restored_sha = hashlib.sha256(Path(rv["restored_path"]).read_bytes()).hexdigest() if rv.get("restored_path") else None
    after_revert = svc_cli("is-valid", "--state", state, "--child", cp)["status"]
    chain = svc_cli("verify-chain", "--state", state, "--anchor", anchor)
    report = {"label": "TEST_ONLY real-model check; TEST key; FEASIBILITY_ONLY patch; not an approval of a real edit",
              "service_status": res["status"], "service_rc": p.returncode, "reason": res.get("reason"),
              "child_sha256": res.get("child_sha256"), "child_sha256_expected_independent": child_sha_expected,
              "child_tensor_sha256": res.get("child_tensor_sha256"), "b4_recorded_tensor_sha256": B4_RECORDED,
              "b4_child_file_tensor_sha256": b4_file_tsha,
              "tensors_match_b4_record": res.get("child_tensor_sha256") == B4_RECORDED == b4_file_tsha,
              "changed_entries": res.get("changed_entries"), "is_valid_without_anchor": valid_local, "is_valid": valid, "chain": chain,
              "revert_status": rv.get("status"), "restored_parent_sha256_service": rv.get("restored_parent_sha256"),
              "restored_parent_sha256_rehashed": restored_sha, "restored_equals_verified_parent": restored_sha == PARENT_SHA,
              "child_after_revert": after_revert, "revert_wall_seconds": t_revert,
              "apply_wall_seconds": t_apply, "total_wall_seconds": time.time() - t0,
              "manifest_sha256": hashlib.sha256(manb).hexdigest(), "payload_sha256": hashlib.sha256(npz).hexdigest()}
    (out / "report.json").write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))
    ok = (report["service_status"] == "APPLIED" and report["tensors_match_b4_record"] and valid_local == "VALID_LOCAL" and valid == "VALID_ANCHORED"
          and rv.get("status") == "REVERTED" and restored_sha == PARENT_SHA and after_revert == "NOT_VALID" and chain["status"] == "OK")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
