"""A5 tests 1-11 on synthetic checkpoints (test 12 is in test_a5_os_separation.py). TEST keys only."""
import json, os, shutil, signal, subprocess, threading, time
from pathlib import Path
import numpy as np
import pytest
from a5_testlib import *  # noqa: F401,F403


@pytest.fixture
def env(tmp_path):
    """Fresh service state + parent + valid in-scope patch + valid signed approval."""
    parent = make_parent(tmp_path / "p.safetensors")
    grant = grant_for(parent)
    signer = Signer(); observer = Signer(principal="TEST-observer", key_id="TEST-key-observer", roles=("observer",))
    hooks = tmp_path / "hooks"; hooks.mkdir()
    state = tmp_path / "state"
    svc.init_state(state, grant, policy(signer, observer), {"test_hooks_enabled": True, "hook_dir": str(hooks)})
    man, npz, child, tsha = make_patch(parent)
    a = approval_for(grant, parent, man, npz, child, tsha, signer)
    E = type("E", (), {})()
    E.__dict__.update(tmp=tmp_path, parent=parent, grant=grant, signer=signer, observer=observer, hooks=hooks, state=state,
                      man=man, npz=npz, child=child, tsha=tsha, approval=a, env=signer.sign(a))
    E.staging = stage(tmp_path / "staging", parent, man, npz, E.env)
    return E


def children(E):
    return sorted(p.name for p in (E.state / "children").iterdir())


def apply_(E, staging=None, **kw):
    return cli("apply", "--state", E.state, "--staging", staging or E.staging, **kw)


def chain_ok(E):
    rc, out = cli("verify-chain", "--state", E.state)
    return rc == 0 and out["status"] == "OK"


# ---------------------------------------------------------------- 1
def test_01_successful_apply(env):
    rc, out = apply_(env)
    assert rc == 0 and out["status"] == "APPLIED", out
    final = Path(out["child_path"])
    assert final.read_bytes() == env.child and out["child_sha256"] == sha(env.child)
    assert out["child_tensor_sha256"] == env.tsha
    assert children(env) == [f"{sha(env.child)}.safetensors"]
    assert cli("is-valid", "--state", env.state, "--child", final)[1]["status"] == "VALID"
    r = receipts(env.state)
    assert [x["event"] for x in r] == ["APPLIED"] and r[0]["child_sha256"] == sha(env.child) and r[0]["prev_sha256"] == "0" * 64
    assert chain_ok(env) and svc.nonce_status(svc.State(env.state), env.approval["approval_nonce"]) == "APPLIED"
    assert not list((env.state / "work").iterdir()) and not list((env.state / "inflight").iterdir())


# ---------------------------------------------------------------- 2
def test_02_denials_do_not_apply_or_burn(env):
    E = env; a = E.approval
    bad_sig = json.loads(json.dumps(E.env)); s = bytearray(__import__("base64").b64decode(bad_sig["signature_b64"])); s[0] ^= 1
    bad_sig["signature_b64"] = __import__("base64").b64encode(bytes(s)).decode()
    unknown = Signer(principal=E.signer.principal, key_id="TEST-key-unknown").sign(a)
    wrong_role = E.observer.sign({**a, "approver_principal": E.observer.principal})
    self_appr = E.signer.sign({**a, "proposer_principal": E.signer.principal})
    wrong_grant = E.signer.sign({**a, "scope_grant_sha256": "f" * 64})
    cases = {"missing": None, "malformed": b"{not json", "bad_signature": bad_sig, "unknown_key": unknown,
             "wrong_role": wrong_role, "approver_is_proposer": self_appr, "wrong_grant_hash": wrong_grant,
             "extra_envelope_field": {**E.env, "note": "trust me"}}
    for name, envl in cases.items():
        st = stage(E.tmp / f"s_{name}", E.parent, E.man, E.npz, envl)
        rc, out = apply_(E, st)
        assert rc == 1 and out["status"] == "DENIED", (name, out)
        assert out["nonce_burned"] is False and children(E) == [], name
    assert svc.nonce_status(svc.State(E.state), a["approval_nonce"]) is None
    assert [r["event"] for r in receipts(E.state)] == ["DENIED"] * len(cases) and chain_ok(E)
    rc, out = apply_(E)  # the same valid approval still works afterwards: nonce was not burned
    assert out["status"] == "APPLIED"


# ---------------------------------------------------------------- 3
@pytest.mark.parametrize("what", ["payload", "manifest", "parent"])
def test_03_tampered_patch_or_parent(env, what):
    E = env
    f = {"payload": "edit.npz", "manifest": "manifest.json", "parent": "parent.safetensors"}[what]
    p = E.staging / f; b = bytearray(p.read_bytes())
    if what == "manifest":
        b += b"\n"
    else:
        b[-3] ^= 0x01
    p.write_bytes(bytes(b))
    rc, out = apply_(E)
    assert out["status"] == "DENIED" and "match approval" in out["reason"], out
    assert children(E) == [] and chain_ok(E)


# ---------------------------------------------------------------- 4
def test_04_swap_after_ingest_has_no_effect(env):
    E = env
    parent_b = make_parent(E.tmp / "p2.safetensors", seed=7)
    man2, npz2, child2, _ = make_patch(E.parent, seed=99)  # different in-scope patch
    proc = spawn("apply", "--state", E.state, "--staging", E.staging, env={"BL_A5_HOOKS": "after_ingest"})
    wait_for(E.hooks / "after_ingest.reached")
    (E.staging / "edit.npz").write_bytes(npz2); (E.staging / "manifest.json").write_bytes(man2)
    (E.staging / "parent.safetensors").write_bytes(parent_b)
    (E.hooks / "after_ingest.go").touch()
    out = json.loads(proc.communicate(timeout=120)[0].splitlines()[-1])
    assert out["status"] == "APPLIED" and out["child_sha256"] == sha(E.child) != sha(child2)
    assert Path(out["child_path"]).read_bytes() == E.child


def test_04b_swap_before_ingest_and_symlink_denied(env):
    E = env
    man2, npz2, _, _ = make_patch(E.parent, seed=99)
    (E.staging / "edit.npz").write_bytes(npz2)
    assert apply_(E)[1]["status"] == "DENIED"
    (E.staging / "edit.npz").unlink(); (E.tmp / "real.npz").write_bytes(E.npz); os.symlink(E.tmp / "real.npz", E.staging / "edit.npz")
    rc, out = apply_(E)
    assert out["status"] == "DENIED" and "symlink" in out["reason"] and children(E) == []


# ---------------------------------------------------------------- 5
def signed_variant(E, name, **patch_kw):
    man, npz, child, tsha = make_patch(E.parent, **patch_kw)
    a = approval_for(E.grant, E.parent, man, npz, child, tsha, E.signer, approval_nonce=f"TEST-NONCE-{name}")
    return stage(E.tmp / f"s5_{name}", E.parent, man, npz, E.signer.sign(a))


def test_05_validly_signed_but_out_of_scope_or_wrong_parent(env):
    E = env
    bad_cols = {T15: ALLOWED[T15][:-1] + [61], T19: ALLOWED[T19]}  # column 61 not granted
    variants = {"column_outside_grant": dict(cols=bad_cols), "over_budget": dict(count=BUDGET + 1),
                "third_tensor": dict(extra_tensor=True), "non_permitted_tensor_swapped_in": dict(swap_tensor=True), "int32_indices": dict(idx_dtype="<i4"),
                "float32_values": dict(val_dtype="<f4")}
    for name, kw in variants.items():
        rc, out = apply_(E, signed_variant(E, name, **kw))
        assert out["status"] == "DENIED", (name, out)
    # wrong parent: approval signed for parent B; staging carries parent B; B's tensors are not the grant's base
    pB = make_parent(E.tmp / "pB.safetensors", seed=3)
    man, npz, child, tsha = make_patch(pB)
    stB = stage(E.tmp / "s5_wrong_parent", pB, man, npz, E.signer.sign(approval_for(E.grant, pB, man, npz, child, tsha, E.signer, approval_nonce="TEST-NONCE-wp")))
    rc, out = apply_(E, stB)
    assert out["status"] == "DENIED" and "base_tensor_sha256" in out["reason"], out
    # wrong parent: approval names parent B, staging has the real parent
    a = {**E.approval, "parent_checkpoint_sha256": sha(pB), "approval_nonce": "TEST-NONCE-wp2"}
    rc, out = apply_(E, stage(E.tmp / "s5_wrong_parent2", E.parent, E.man, E.npz, E.signer.sign(a)))
    assert out["status"] == "DENIED" and "parent" in out["reason"]
    assert children(E) == [] and chain_ok(E)
    reasons = [r["reason"] for r in receipts(E.state)]
    assert any("column outside grant" in x for x in reasons) and any("entry budget" in x for x in reasons)
    assert any("too many tensors" in x for x in reasons) and any("tensor set" in x for x in reasons)
    assert any("int64" in x for x in reasons) and any("uint16" in x for x in reasons)


# ---------------------------------------------------------------- 6
def test_06_replay(env):
    assert apply_(env)[1]["status"] == "APPLIED"
    rc, out = apply_(env)
    assert out["status"] == "DENIED" and "replay" in out["reason"]
    assert len(children(env)) == 1 and chain_ok(env)


# ---------------------------------------------------------------- 7
def write_revocations(E, nonces=(), shas=()):
    (E.state / "config" / "revocations.json").write_bytes(svc.canonical({"revoked_approval_nonces": list(nonces), "revoked_approval_sha256": list(shas)}))


def test_07_revoked_before_apply(env):
    E = env
    write_revocations(E, nonces=[E.approval["approval_nonce"]])
    assert apply_(E)[1]["reason"] == "approval revoked"
    write_revocations(E, shas=[sha(svc.DOMAIN + svc.canonical(E.approval))])
    rc, out = apply_(E)
    assert out["status"] == "DENIED" and out["reason"] == "approval revoked" and children(E) == []


def test_07b_revoked_during_apply(env):
    E = env
    proc = spawn("apply", "--state", E.state, "--staging", E.staging, env={"BL_A5_HOOKS": "before_commit"})
    wait_for(E.hooks / "before_commit.reached")
    assert any(p.name.startswith(".partial") for p in (E.state / "children").iterdir())  # written + verified, not yet valid
    write_revocations(E, nonces=[E.approval["approval_nonce"]])
    (E.hooks / "before_commit.go").touch()
    out = json.loads(proc.communicate(timeout=120)[0].splitlines()[-1])
    assert out["status"] == "DENIED" and out["nonce_burned"] is True
    assert children(E) == [] and [r["event"] for r in receipts(E.state)] == ["DENIED"] and chain_ok(E)


# ---------------------------------------------------------------- 8
def test_08_two_concurrent_requests_same_approval(env):
    E = env
    st2 = stage(E.tmp / "staging2", E.parent, E.man, E.npz, E.env)
    p1 = spawn("apply", "--state", E.state, "--staging", E.staging, env={"BL_A5_HOOKS": "before_commit"})
    wait_for(E.hooks / "before_commit.reached")
    p2 = spawn("apply", "--state", E.state, "--staging", st2)  # blocks on the service lock while p1 is mid-apply
    time.sleep(1.0); assert p2.poll() is None
    (E.hooks / "before_commit.go").touch()
    o1 = json.loads(p1.communicate(timeout=120)[0].splitlines()[-1]); o2 = json.loads(p2.communicate(timeout=120)[0].splitlines()[-1])
    assert sorted([o1["status"], o2["status"]]) == ["APPLIED", "DENIED"] and len(children(E)) == 1 and chain_ok(E)


def test_08b_nonce_reservation_race_without_lock(env):
    """The sqlite reservation alone admits exactly one of N simultaneous reservers."""
    st = svc.State(env.state); res = []; barrier = threading.Barrier(8)
    def go(i):
        barrier.wait()
        try:
            svc.reserve_nonce(st, "TEST-RACE-NONCE", "TEST-RACE-SCOPE", "x" * 64, f"r{i}"); res.append("ok")
        except svc.Denied:
            res.append("denied")
    ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert res.count("ok") == 1 and res.count("denied") == 7


# ---------------------------------------------------------------- 9
@pytest.mark.parametrize("point", ["mid_write", "after_rename"])
def test_09_killed_mid_apply_then_recovery(env, point):
    E = env
    proc = spawn("apply", "--state", E.state, "--staging", E.staging, env={"BL_A5_HOOKS": point})
    wait_for(E.hooks / f"{point}.reached")
    os.kill(proc.pid, signal.SIGKILL); proc.wait()
    files = list((E.state / "children").iterdir())
    assert len(files) == 1 and list((E.state / "inflight").iterdir())
    assert cli("is-valid", "--state", E.state, "--child", files[0])[1]["status"] == "NOT_VALID"  # half-written / unreceipted
    rc, out = cli("recover", "--state", E.state)
    assert rc == 0 and len(out["actions"]) == 1
    r = receipts(E.state)
    assert [x["event"] for x in r] == ["CRASHED"] and r[0]["intended_child_sha256"] == sha(E.child)
    assert children(E) == [] and not list((E.state / "inflight").iterdir()) and not list((E.state / "work").iterdir())
    assert svc.nonce_status(svc.State(E.state), E.approval["approval_nonce"]) == "BURNED_CRASH" and chain_ok(E)
    rc, out = apply_(E)
    assert out["status"] == "DENIED" and "replay" in out["reason"]


def test_09b_hooks_need_admin_config(env):
    """R10: an env var alone cannot pause the service when the admin config disables hooks."""
    E = env
    cfg = E.state / "config" / "service_config.json"
    cfg.write_bytes(svc.canonical({"test_hooks_enabled": False, "hook_dir": str(E.hooks)}))
    rc, out = apply_(E, env={"BL_A5_HOOKS": "mid_write,before_commit"})
    assert out["status"] == "APPLIED" and not list(E.hooks.iterdir())


# ---------------------------------------------------------------- 10
def test_10_disk_full_during_write(env):
    E = env
    mnt = E.tmp / "small"; mnt.mkdir()
    subprocess.run(["mount", "-t", "tmpfs", "-o", "size=96k,mode=0700", "tmpfs", str(mnt)], check=True)
    try:
        assert len(E.child) > 96 * 1024
        (E.state / "config" / "service_config.json").write_bytes(svc.canonical({"test_hooks_enabled": False, "children_dir": str(mnt)}))
        rc, out = apply_(E)
        assert rc == 2 and out["status"] == "ERROR" and "No space left" in out["reason"] and out["nonce_burned"] is True, out
        assert list(mnt.iterdir()) == [] and not list((E.state / "inflight").iterdir())
        assert [r["event"] for r in receipts(E.state)] == ["ERROR"] and chain_ok(E)
        assert svc.nonce_status(svc.State(E.state), E.approval["approval_nonce"]) == "BURNED_ERROR"
    finally:
        subprocess.run(["umount", str(mnt)], check=True)


# ---------------------------------------------------------------- 11
def three_receipts(E):
    apply_(E, stage(E.tmp / "s11a", E.parent, E.man, E.npz, None))         # DENIED (missing approval)
    rc, out = apply_(E)                                                     # APPLIED
    apply_(E)                                                               # DENIED (replay)
    return Path(out["child_path"])


def test_11_edited_or_deleted_receipt_detected(env):
    E = env
    child = three_receipts(E)
    log = E.state / "receipts.jsonl"; head = E.state / "receipts.head"; orig_log, orig_head = log.read_bytes(), head.read_bytes()
    lines = orig_log.splitlines(keepends=True)
    tampers = {"edit_middle": b"".join([lines[0], lines[1].replace(b'"APPLIED"', b'"APPLIED" '), lines[2]]),
               "edit_middle_canonical": b"".join([lines[0], lines[1].replace(b'"event":"APPLIED"', b'"event":"APPLIED","x":1'), lines[2]]),
               "delete_middle": lines[0] + lines[2], "delete_first": lines[1] + lines[2], "truncate_tail": lines[0] + lines[1]}
    for name, content in tampers.items():
        log.write_bytes(content)
        rc, out = cli("verify-chain", "--state", E.state)
        assert rc == 3 and out["status"] == "BROKEN", name
        assert cli("is-valid", "--state", E.state, "--child", child)[1]["status"] == "NOT_VALID", name
        rc, out = apply_(E)
        assert rc == 3 and out["status"] == "REFUSED_CHAIN_BROKEN", name   # service refuses to operate
        log.write_bytes(orig_log); head.write_bytes(orig_head)
    assert chain_ok(E) and cli("is-valid", "--state", E.state, "--child", child)[1]["status"] == "VALID"


def test_11b_known_limit_consistent_rewrite_by_service_user(env):
    """Stated limit (A5_SPEC R5): rewriting log AND head consistently is NOT detectable without an external anchor."""
    E = env
    three_receipts(E)
    log = E.state / "receipts.jsonl"; lines = log.read_bytes().splitlines(keepends=True)
    log.write_bytes(lines[0] + lines[1])
    svc.atomic_write(E.state / "receipts.head", svc.canonical({"seq": 2, "sha256": sha(lines[1].rstrip(b"\n"))}))
    assert chain_ok(E)  # undetected locally: needs an external anchor (e.g. published head hash)


def test_09c_crash_between_receipt_append_and_head_update(env):
    """Recovery advances the head over exactly one well-chained unrecorded receipt; two are refused."""
    E = env
    apply_(E, stage(E.tmp / "s9c", E.parent, E.man, E.npz, None))  # one DENIED receipt
    st = svc.State(E.state); head_before = (E.state / "receipts.head").read_bytes()
    svc.append_receipt(st, "DENIED", {"request_id": "TEST-simulated", "reason": "simulated"})
    (E.state / "receipts.head").write_bytes(head_before)  # as if killed after the log append, before the head update
    assert not chain_ok(E)
    rc, out = cli("recover", "--state", E.state)
    assert rc == 0 and any("head advanced" in a for a in out["actions"]) and chain_ok(E)
    log = (E.state / "receipts.jsonl").read_bytes()
    svc.append_receipt(st, "DENIED", {"request_id": "TEST-x1", "reason": "x"}); svc.append_receipt(st, "DENIED", {"request_id": "TEST-x2", "reason": "x"})
    svc.atomic_write(E.state / "receipts.head", svc.canonical({"seq": 2, "sha256": svc.sha(log.splitlines()[-1])}))
    rc, out = cli("recover", "--state", E.state)
    assert rc == 3 and out["status"] == "REFUSED_CHAIN_BROKEN"
