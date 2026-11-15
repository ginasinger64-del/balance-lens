"""A5.1 new tests T13-T15 (Fix 1 full-parent pin, Fix 2 journal-before-reserve, Fix 3 external head anchor). TEST keys only."""
import json, os, signal
from pathlib import Path
import pytest
from a5_testlib import *  # noqa: F401,F403
from test_a5_service import env, apply_, children, chain_ok, three_receipts  # noqa: F401


# ---------------------------------------------------------------- T13 (Fix 1)
def test_13_parent_with_other_tensor_altered_is_denied(env):
    E = env
    t = svc.parse_safetensors(E.parent)["model.norm.weight"]
    alt = bytearray(E.parent); alt[t["start"]] ^= 0xFF; alt = bytes(alt)
    for n in (T15, T19):  # the two permitted tensors are untouched, so the A5 per-tensor check would pass
        s, e = tensor_region(alt, n); assert alt[s:e] == E.parent[s:e]
    man, npz, child, tsha = make_patch(alt)
    a = approval_for(E.grant, alt, man, npz, child, tsha, E.signer, approval_nonce="TEST-NONCE-T13")  # approval names the altered parent
    rc, out = apply_(E, stage(E.tmp / "s13", alt, man, npz, E.signer.sign(a)))
    assert rc == 1 and out["status"] == "DENIED" and "does not match grant" in out["reason"], out
    assert children(E) == [] and svc.nonce_status(svc.State(E.state), "TEST-NONCE-T13") is None


def test_13b_grant_without_parent_pin_is_denied(env):
    E = env
    g = json.loads(json.dumps(E.grant)); del g["authorization"]["parent_checkpoint_sha256"]
    (E.state / "config" / "grant.json").write_bytes(svc.canonical(g))
    a = {**E.approval, "scope_grant_sha256": sha(svc.canonical(g))}
    rc, out = apply_(E, stage(E.tmp / "s13b", E.parent, E.man, E.npz, E.signer.sign(a)))
    assert out["status"] == "DENIED" and "lacks a full parent_checkpoint_sha256" in out["reason"]


# ---------------------------------------------------------------- T14 (Fix 2)
@pytest.mark.parametrize("point", ["after_journal", "during_reserve"])
def test_14_killed_after_journal_before_or_during_reservation(env, point):
    E = env
    proc = spawn("apply", "--state", E.state, "--staging", E.staging, env={"BL_A5_HOOKS": point})
    wait_for(E.hooks / f"{point}.reached")
    journals = list((E.state / "inflight").iterdir()); assert len(journals) == 1  # journal exists before reservation
    os.kill(proc.pid, signal.SIGKILL); proc.wait()
    assert svc.nonce_status(svc.State(E.state), E.approval["approval_nonce"]) is None  # reservation never committed
    assert children(E) == []
    rc, out = cli("recover", "--state", E.state)
    assert rc == 0 and len(out["actions"]) == 1, out
    r = receipts(E.state)
    assert [x["event"] for x in r] == ["CRASHED"] and "had not committed" in r[0]["nonce_note"], r
    assert svc.nonce_status(svc.State(E.state), E.approval["approval_nonce"]) == "BURNED_CRASH" and chain_ok(E)
    assert not list((E.state / "inflight").iterdir()) and not list((E.state / "work").iterdir())
    rc, out = apply_(E)
    assert out["status"] == "DENIED" and "replay" in out["reason"]


def test_14b_kill_after_reservation_commits(env):
    """The committed-reservation path still burns (regression for the reorder)."""
    E = env
    proc = spawn("apply", "--state", E.state, "--staging", E.staging, env={"BL_A5_HOOKS": "mid_write"})
    wait_for(E.hooks / "mid_write.reached"); os.kill(proc.pid, signal.SIGKILL); proc.wait()
    assert svc.nonce_status(svc.State(E.state), E.approval["approval_nonce"]) == "RESERVED"
    cli("recover", "--state", E.state)
    assert "had committed" in receipts(E.state)[0]["nonce_note"]
    assert svc.nonce_status(svc.State(E.state), E.approval["approval_nonce"]) == "BURNED_CRASH"


# ---------------------------------------------------------------- T15 (Fix 3)
def publish(E, name="anchor.jsonl"):
    rc, out = cli("head", "--state", E.state); assert rc == 0
    p = E.tmp / name; p.write_text(json.dumps(out) + "\n"); return p, out


def test_15_consistent_rewrite_detected_against_published_head(env):
    E = env
    child = three_receipts(E)
    anchor, a = publish(E)
    assert a["seq"] == 3 and a["state_id"] == (E.state / "config" / "state_id").read_text()
    assert cli("verify-chain", "--state", E.state, "--anchor", anchor)[0] == 0
    # the A5 11b rewrite: drop the last receipt and rewrite the head consistently
    log = E.state / "receipts.jsonl"; lines = log.read_bytes().splitlines(keepends=True)
    log.write_bytes(lines[0] + lines[1])
    svc.atomic_write(E.state / "receipts.head", svc.canonical({"seq": 2, "sha256": sha(lines[1].rstrip(b"\n"))}))
    assert chain_ok(E)  # still undetectable locally
    rc, out = cli("verify-chain", "--state", E.state, "--anchor", anchor)
    assert rc == 3 and out["status"] == "BROKEN" and "missing" in out["problems"][0], out
    assert cli("is-valid", "--state", E.state, "--child", child, "--anchor", anchor)[1]["status"] == "NOT_VALID"
    # a full consistent rewrite that keeps the length (edit receipt 3 and re-chain + rewrite head)
    r3 = json.loads(lines[2]); r3["reason"] = "rewritten"; l3 = svc.canonical(r3)
    log.write_bytes(lines[0] + lines[1] + l3 + b"\n")
    svc.atomic_write(E.state / "receipts.head", svc.canonical({"seq": 3, "sha256": sha(l3)}))
    assert chain_ok(E)
    rc, out = cli("verify-chain", "--state", E.state, "--anchor", anchor)
    assert rc == 3 and "differs from published anchor" in out["problems"][0], out


def test_15b_legit_append_still_verifies_and_foreign_anchor_rejected(env):
    E = env
    apply_(E, stage(E.tmp / "s15b", E.parent, E.man, E.npz, None))
    anchor, _ = publish(E)
    rc, out = apply_(E); assert out["status"] == "APPLIED"
    assert cli("verify-chain", "--state", E.state, "--anchor", anchor)[0] == 0
    assert cli("is-valid", "--state", E.state, "--child", out["child_path"], "--anchor", anchor)[1]["status"] == "VALID_LOCAL"  # A5.2: applied after the anchor
    foreign = json.loads(anchor.read_text()); foreign["state_id"] = "0" * 32
    fp = E.tmp / "foreign.jsonl"; fp.write_text(json.dumps(foreign) + "\n")
    rc, out = cli("verify-chain", "--state", E.state, "--anchor", fp)
    assert rc == 3 and "another state" in out["problems"][0]
