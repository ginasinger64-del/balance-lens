"""A5 test 12: OS-enforced separation between signer, service and agent users.
Must run as root (creates system users and sets ownership). TEST keys and synthetic checkpoints only."""
import json, os, pwd, shutil, subprocess, sys
from pathlib import Path
import pytest
from a5_testlib import *  # noqa: F401,F403

ROOT = Path("/srv/bl_a5_test")
USERS = {"signer": "bl_a5_signer", "svc": "bl_a5_svc", "agent": "bl_a5_agent"}

SIGN_PY = r'''
import base64, json, sys
from cryptography.hazmat.primitives.serialization import load_pem_private_key
sys.path.insert(0, sys.argv[4]); import apply_service as svc
key = load_pem_private_key(open(sys.argv[1], "rb").read(), password=None)
a = json.load(open(sys.argv[2]))
sig = key.sign(svc.DOMAIN + svc.canonical(a))
open(sys.argv[3], "w").write(json.dumps({"approval": a, "key_id": sys.argv[5], "algorithm": "Ed25519", "signature_b64": base64.b64encode(sig).decode()}))
'''

ATTACK_PY = r'''
import json, os, sys
ops = json.loads(sys.argv[1]); res = {}
for name, (kind, path, *rest) in ops.items():
    try:
        if kind == "read": open(path, "rb").read(1)
        elif kind == "write": open(path, "r+b").write(b"x")
        elif kind == "append": open(path, "ab").write(b"x")
        elif kind == "create": open(path, "xb").write(b"x")
        elif kind == "list": os.listdir(path)
        elif kind == "replace": os.replace(rest[0], path)
        elif kind == "unlink": os.unlink(path)
        res[name] = "SUCCEEDED"
    except OSError as e:
        res[name] = f"{e.__class__.__name__}:{e.errno}"
print(json.dumps(res))
'''


def run_as(user, *cmd):
    return subprocess.run(["setpriv", f"--reuid={user}", f"--regid={user}", "--init-groups", *cmd], capture_output=True, text=True)


def chown_tree(p, user, dmode=0o700, fmode=0o600):
    u = pwd.getpwnam(user)
    for d, ds, fs in os.walk(p):
        os.chown(d, u.pw_uid, u.pw_gid); os.chmod(d, dmode)
        for f in fs:
            fp = os.path.join(d, f); os.chown(fp, u.pw_uid, u.pw_gid); os.chmod(fp, fmode)


@pytest.fixture(scope="module")
def world():
    if os.geteuid() != 0:
        pytest.skip("test 12 needs root to create users")
    for u in USERS.values():
        if subprocess.run(["id", u], capture_output=True).returncode:
            subprocess.run(["useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin", u], check=True)
    shutil.rmtree(ROOT, ignore_errors=True); ROOT.mkdir(mode=0o755)
    bin_ = ROOT / "bin"; bin_.mkdir(mode=0o755)
    shutil.copy(SERVICE, bin_ / "apply_service.py"); (bin_ / "sign_test.py").write_text(SIGN_PY); (bin_ / "attack.py").write_text(ATTACK_PY)
    for f in bin_.iterdir(): f.chmod(0o755)
    parent = make_parent(ROOT / "tmp_parent"); (ROOT / "tmp_parent").unlink()
    grant = grant_for(parent); signer = Signer(); revoker = revoker_signer()
    # signer: private key 0600 in a 0700 dir; handoff dir is signer-writable, world-readable
    sdir = ROOT / "signer"; sdir.mkdir(); (sdir / "key.pem").write_bytes(signer.pem()); chown_tree(sdir, USERS["signer"])
    handoff = ROOT / "handoff"; handoff.mkdir(); chown_tree(handoff, USERS["signer"], 0o755, 0o644)
    # service: all state 0700/0600, owned by the service user
    state = ROOT / "state"
    svc.init_state(state, grant, policy(signer, revoker), revocations_envelope=sign_revocations(revoker, grant["authorization"]["service_state_id"]))
    chown_tree(state, USERS["svc"])
    # agent: owns only the staging area (readable by the service)
    man, npz, child, tsha = make_patch(parent)
    staging = stage(ROOT / "staging", parent, man, npz, None); chown_tree(staging, USERS["agent"], 0o755, 0o644)
    a = approval_for(grant, parent, man, npz, child, tsha, signer)
    (handoff / "approval.json").write_text(json.dumps(a)); chown_tree(handoff, USERS["signer"], 0o755, 0o644)
    p = run_as(USERS["signer"], sys.executable, str(bin_ / "sign_test.py"), str(sdir / "key.pem"), str(handoff / "approval.json"),
               str(handoff / "approval.signed.json"), str(bin_), signer.key_id)
    assert p.returncode == 0, p.stderr
    p = run_as(USERS["agent"], "cp", str(handoff / "approval.signed.json"), str(staging / "approval.signed.json"))
    assert p.returncode == 0, p.stderr
    return dict(bin=bin_, sdir=sdir, handoff=handoff, state=state, staging=staging, child=child)


def attack(user, w, ops):
    p = run_as(user, sys.executable, str(w["bin"] / "attack.py"), json.dumps(ops))
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def test_12_os_separation(world):
    w = world; st = w["state"]
    # service applies the agent's staged patch, running as the service user
    p = run_as(USERS["svc"], sys.executable, str(w["bin"] / "apply_service.py"), "apply", "--state", str(st), "--staging", str(w["staging"]))
    out = json.loads(p.stdout.splitlines()[-1]); assert out["status"] == "APPLIED", (out, p.stderr)
    child = out["child_path"]
    (w["staging"] / "fake.safetensors").write_bytes(b"forged"); chown_tree(w["staging"], USERS["agent"], 0o755, 0o644)
    ops = {"read_signing_key": ["read", str(w["sdir"] / "key.pem")],
           "list_signer_dir": ["list", str(w["sdir"])],
           "forge_handoff_approval": ["write", str(w["handoff"] / "approval.signed.json")],
           "list_service_state": ["list", str(st)],
           "create_child_checkpoint": ["create", str(st / "children" / "evil.safetensors")],
           "overwrite_valid_child": ["write", child],
           "replace_valid_child": ["replace", child, str(w["staging"] / "fake.safetensors")],
           "delete_valid_child": ["unlink", child],
           "write_grant": ["write", str(st / "config" / "grant.json")],
           "write_trust_policy": ["write", str(st / "config" / "trust_policy.json")],
           "write_revocations": ["write", str(st / "config" / "revocations.json")],
           "write_service_config": ["write", str(st / "config" / "service_config.json")],
           "write_nonce_db": ["write", str(st / "nonces.sqlite")],
           "append_receipts": ["append", str(st / "receipts.jsonl")],
           "write_receipt_head": ["write", str(st / "receipts.head")]}
    res = attack(USERS["agent"], w, ops)
    assert all(v.startswith("PermissionError:13") or v.startswith("PermissionError:1") for v in res.values()), res
    # the service user cannot read the signing key either (signer and service are separate)
    res2 = attack(USERS["svc"], w, {"svc_reads_signing_key": ["read", str(w["sdir"] / "key.pem")]})
    assert res2["svc_reads_signing_key"].startswith("PermissionError"), res2
    # the valid child is still valid and unchanged
    p = run_as(USERS["svc"], sys.executable, str(w["bin"] / "apply_service.py"), "is-valid", "--state", str(st), "--child", child)
    assert json.loads(p.stdout)["status"] == "VALID_LOCAL"
    assert Path(child).read_bytes() == w["child"]
    (ROOT / "os_results.json").write_text(json.dumps({"agent": res, "service": res2}, indent=1))
