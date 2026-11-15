"""A5.2 new tests T16-T22. TEST keys and synthetic checkpoints only."""
import json, os, signal, subprocess, time
from pathlib import Path
import pytest
from a5_testlib import *  # noqa: F401,F403
from test_a5_service import env, apply_, children, chain_ok, write_revocations  # noqa: F401


def status(E, child, anchor=None):
    args = ["is-valid", "--state", E.state, "--child", child] + (["--anchor", anchor] if anchor else [])
    return cli(*args)


def publish(E, name="anchor.jsonl"):
    rc, out = cli("head", "--state", E.state); assert rc == 0
    p = E.tmp / name; p.write_text(json.dumps(out) + "\n"); return p


def new_approval(E, nonce, grant=None, parent=None, seed=1, **over):
    g = grant or E.grant; pa = parent or E.parent
    man, npz, child, tsha = make_patch(pa, seed=seed)
    a = approval_for(g, pa, man, npz, child, tsha, E.signer, approval_nonce=nonce, **over)
    return stage(E.tmp / f"s_{nonce}", pa, man, npz, E.signer.sign(a)), a


# ---------------------------------------------------------------- T16 anchored vs local
def test_16_anchored_vs_local(env):
    E = env
    old = publish(E, "old.jsonl")  # seq 0, before the apply
    rc, out = apply_(E); child = out["child_path"]
    rc, s = status(E, child); assert rc == 2 and s["status"] == "VALID_LOCAL"
    rc, s = status(E, child, old); assert rc == 2 and s["status"] == "VALID_LOCAL"  # receipt is after the anchor
    new = publish(E, "new.jsonl")
    rc, s = status(E, child, new); assert rc == 0 and s["status"] == "VALID_ANCHORED", s
    both = E.tmp / "both.jsonl"; both.write_text(old.read_text() + new.read_text())
    assert status(E, child, both)[1]["status"] == "VALID_ANCHORED"


# ---------------------------------------------------------------- T17 one state per grant
def test_17_grant_bound_to_one_state(env):
    E = env
    other = E.tmp / "other_state"
    svc.init_state(other, E.grant, policy(E.signer, E.revoker), state_id_="TEST-OTHER-STATE",
                   revocations_envelope=sign_revocations(E.revoker, "TEST-OTHER-STATE"))
    rc, out = cli("apply", "--state", other, "--staging", E.staging)
    assert out["status"] == "DENIED" and "another service state" in out["reason"], out
    g = json.loads(json.dumps(E.grant)); del g["authorization"]["service_state_id"]
    (E.state / "config" / "grant.json").write_bytes(svc.canonical(g))
    st, _ = new_approval(E, "TEST-NONCE-17", grant=g)
    assert apply_(E, st)[1]["reason"].startswith("grant is bound to another service state")
    assert children(E) == []


# ---------------------------------------------------------------- T18 expiry
def test_18_expiry(env):
    E = env
    st, _ = new_approval(E, "TEST-NONCE-18a", not_after_utc=utc_in(-60))
    rc, out = apply_(E, st); assert out["status"] == "DENIED" and out["reason"] == "approval expired"
    st, _ = new_approval(E, "TEST-NONCE-18b", not_after_utc=None)
    rc, out = apply_(E, st); assert out["status"] == "DENIED" and "not_after_utc" in out["reason"]
    st, _ = new_approval(E, "TEST-NONCE-18c", not_after_utc="2026-11-14 23:00:00")
    assert apply_(E, st)[1]["status"] == "DENIED"
    # expires while the apply is paused before commit
    st, a = new_approval(E, "TEST-NONCE-18d", not_after_utc=utc_in(3))
    proc = spawn("apply", "--state", E.state, "--staging", st, env={"BL_A5_HOOKS": "before_commit"})
    wait_for(E.hooks / "before_commit.reached"); time.sleep(max(0, svc.parse_utc(a["not_after_utc"]) - time.time()) + 1.2)
    (E.hooks / "before_commit.go").touch()
    out = json.loads(proc.communicate(timeout=60)[0].splitlines()[-1])
    assert out["status"] == "DENIED" and out["reason"] == "approval expired" and out["nonce_burned"] is True
    assert children(E) == [] and chain_ok(E)


# ---------------------------------------------------------------- T19 signed revocations
def test_19_revocation_list_must_be_signed(env):
    E = env
    cfg = E.state / "config" / "revocations.json"
    bad_sig = sign_revocations(E.revoker, E.sid, seq=1); bad_sig["revocations"]["sequence"] = 2  # signed content altered
    cases = {"unsigned": {"revoked_approval_nonces": [], "revoked_approval_sha256": []},
             "bad_signature": bad_sig,
             "wrong_domain": sign_revocations(E.revoker, E.sid, seq=1, domain=svc.DOMAIN),
             "approver_key_lacks_role": {**sign_revocations(E.signer, E.sid, seq=1), "key_id": E.signer.key_id},
             "unknown_key": sign_revocations(Signer(principal="x", key_id="TEST-key-unknown-rev", roles=("revocation_signer",)), E.sid, seq=1),
             "other_state": sign_revocations(E.revoker, "TEST-SOMEONE-ELSE", seq=1)}
    for name, envl in cases.items():
        cfg.write_bytes(svc.canonical(envl))
        rc, out = apply_(E)
        assert out["status"] == "DENIED" and out["reason"].startswith("revocation list invalid"), (name, out)
        assert out["nonce_burned"] is False, name
    # rollback: install seq 5 (recorded at install), then an older validly signed seq 3
    svc.install_revocations(E.state, sign_revocations(E.revoker, E.sid, seq=5))
    with pytest.raises(svc.Denied, match="rollback"):
        svc.install_revocations(E.state, sign_revocations(E.revoker, E.sid, seq=3))  # refused by the admin command
    cfg.write_bytes(svc.canonical(sign_revocations(E.revoker, E.sid, seq=3)))      # written behind the service's back
    rc, out = apply_(E); assert out["status"] == "DENIED" and "rollback" in out["reason"], out
    with pytest.raises(svc.Denied, match="revocation list invalid"):
        svc.install_revocations(E.state, {"revoked_approval_nonces": []})  # unsigned list refused at install
    assert children(E) == []
    svc.install_revocations(E.state, sign_revocations(E.revoker, E.sid, seq=6))
    assert apply_(E)[1]["status"] == "APPLIED"


# ---------------------------------------------------------------- T20 epoch closure
def test_20_epoch_closed_after_apply(env):
    E = env
    st2, _ = new_approval(E, "TEST-NONCE-20b", seed=2)  # a second, otherwise valid approval under the same grant
    assert apply_(E)[1]["status"] == "APPLIED"
    rc, out = apply_(E, st2)
    assert out["status"] == "DENIED" and "epoch TEST-EPOCH-1 is closed" in out["reason"]
    with pytest.raises(svc.Denied):
        svc.install_grant(E.state, E.grant)  # reinstalling the closed epoch is refused
    g2 = grant_for(E.parent, run="TEST-RUN-2", nonce="TEST-SCOPE-NONCE-2", state_id=E.sid, epoch="TEST-EPOCH-2")
    svc.install_grant(E.state, g2)
    st3, _ = new_approval(E, "TEST-NONCE-20c", grant=g2, seed=3)
    rc, out = apply_(E, st3)
    assert out["status"] == "APPLIED" and out["authorization_epoch"] == "TEST-EPOCH-2"
    assert len(children(E)) == 2 and chain_ok(E)


# ---------------------------------------------------------------- T21 revert
def test_21_revert(env):
    E = env
    rc, out = apply_(E); child = out["child_path"]
    rc, rv = cli("revert", "--state", E.state, "--child", child)
    assert rc == 0 and rv["status"] == "REVERTED" and rv["restored_parent_sha256"] == sha(E.parent), rv
    assert Path(rv["restored_path"]).read_bytes() == E.parent
    r = receipts(E.state)[-1]
    assert r["event"] == "REVERTED" and r["reverted_child_sha256"] == sha(E.child) and r["applied_receipt_seq"] == out["receipt_seq"]
    assert status(E, child)[1]["status"] == "NOT_VALID" and "reverted" in status(E, child)[1]["reason"]
    assert status(E, rv["restored_path"])[1]["status"] == "NOT_VALID"
    rc, again = cli("revert", "--state", E.state, "--child", child)
    assert again["status"] == "DENIED" and "already reverted" in again["reason"]
    stray = E.tmp / "stray.safetensors"; stray.write_bytes(E.child)
    assert cli("revert", "--state", E.state, "--child", stray)[1]["status"] == "DENIED"
    assert chain_ok(E)


def test_21b_revert_tampered_child_and_crash(env):
    E = env
    rc, out = apply_(E); child = Path(out["child_path"])
    good = child.read_bytes(); b = bytearray(good); b[-1] ^= 1; child.write_bytes(bytes(b))  # as root: tamper the applied child
    rc, rv = cli("revert", "--state", E.state, "--child", child)
    assert rv["status"] == "DENIED" and "no APPLIED receipt" in rv["reason"]
    assert not any(p.name.startswith("restored-") for p in (E.state / "children").iterdir())
    child.write_bytes(good)
    proc = spawn("revert", "--state", E.state, "--child", child, env={"BL_A5_HOOKS": "revert_mid_write"})
    wait_for(E.hooks / "revert_mid_write.reached"); os.kill(proc.pid, signal.SIGKILL); proc.wait()
    rc, rec = cli("recover", "--state", E.state)
    r = receipts(E.state)[-1]
    assert r["event"] == "CRASHED" and r["action"] == "revert", r
    assert not any(p.name.startswith((".partial-revert", "restored-")) for p in (E.state / "children").iterdir())
    assert status(E, child)[1]["status"] == "VALID_LOCAL"  # crashed revert leaves the child valid
    assert cli("revert", "--state", E.state, "--child", child)[1]["status"] == "REVERTED"


# ---------------------------------------------------------------- T22 disk full in the state dir
@pytest.fixture
def small_state(tmp_path):
    """Service state on a 2 MB tmpfs; children elsewhere."""
    mnt = tmp_path / "statefs"; mnt.mkdir()
    subprocess.run(["mount", "-t", "tmpfs", "-o", "size=2m,mode=0700", "tmpfs", str(mnt)], check=True)
    parent = make_parent(tmp_path / "p.safetensors"); grant = grant_for(parent)
    signer = Signer(); revoker = revoker_signer(); sid = grant["authorization"]["service_state_id"]
    hooks = tmp_path / "hooks"; hooks.mkdir(); kids = tmp_path / "children"
    state = mnt / "state"
    svc.init_state(state, grant, policy(signer, revoker), {"test_hooks_enabled": True, "hook_dir": str(hooks), "children_dir": str(kids)},
                   revocations_envelope=sign_revocations(revoker, sid))
    man, npz, child, tsha = make_patch(parent); a = approval_for(grant, parent, man, npz, child, tsha, signer)
    E = type("E", (), {})(); E.__dict__.update(tmp=tmp_path, mnt=mnt, state=state, hooks=hooks, kids=kids, parent=parent, child=child, approval=a,
                                               staging=stage(tmp_path / "staging", parent, man, npz, signer.sign(a)))
    yield E
    subprocess.run(["umount", str(mnt)], check=True)


def fill(mnt):
    p = mnt / "filler"; f = open(p, "wb")
    try:
        while True:
            f.write(b"\0" * 4096); f.flush()
    except OSError:
        pass
    finally:
        try:
            f.close()
        except OSError:
            pass
    return p


def test_22a_state_disk_full_at_receipt_write(small_state):
    E = small_state
    proc = spawn("apply", "--state", E.state, "--staging", E.staging, env={"BL_A5_HOOKS": "after_rename"})
    wait_for(E.hooks / "after_rename.reached")
    final = E.kids / f"{sha(E.child)}.safetensors"; assert final.exists()  # renamed, but no receipt yet
    filler = fill(E.mnt)
    (E.hooks / "after_rename.go").touch()
    out = json.loads(proc.communicate(timeout=60)[0].splitlines()[-1])
    assert out["status"] == "FAILED_CLOSED" and "No space left" in out["reason"] + out.get("secondary_error", ""), out
    assert not final.exists() and list(E.kids.iterdir()) == []
    assert cli("verify-chain", "--state", E.state)[1]["status"] == "OK"  # no partial receipt line left behind
    assert [r["event"] for r in receipts(E.state)] == []
    filler.unlink()
    rc, rec = cli("recover", "--state", E.state)
    assert rc == 0 and [r["event"] for r in receipts(E.state)] == ["CRASHED"], rec
    assert svc.nonce_status(svc.State(E.state), E.approval["approval_nonce"]).startswith("BURNED")
    assert cli("apply", "--state", E.state, "--staging", E.staging)[1]["status"] == "DENIED"


def test_22b_state_disk_full_before_ingest(small_state):
    E = small_state
    filler = fill(E.mnt)
    rc, out = cli("apply", "--state", E.state, "--staging", E.staging)
    assert out["status"] in ("FAILED_CLOSED", "ERROR"), out
    assert list(E.kids.iterdir()) == []
    assert cli("verify-chain", "--state", E.state)[1]["status"] == "OK"
    assert svc.nonce_status(svc.State(E.state), E.approval["approval_nonce"]) is None  # nothing reserved
    filler.unlink()
    assert cli("apply", "--state", E.state, "--staging", E.staging)[1]["status"] == "APPLIED"
