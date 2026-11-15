"""Synthetic fixtures for A5 tests. All keys are throwaway TEST keys; nothing here is a real approval."""
import base64, hashlib, io, json, os, struct, subprocess, sys, time
from pathlib import Path
import numpy as np
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

HERE = Path(__file__).resolve().parent
SERVICE = HERE.parent / "apply_service.py"
sys.path.insert(0, str(HERE.parent))
import apply_service as svc  # noqa: E402

T15 = "model.language_model.layers.15.mlp.down_proj.weight"
T19 = "model.language_model.layers.19.mlp.down_proj.weight"
SHAPE = [16, 64]
ALLOWED = {T15: [1, 5, 9, 13, 20, 33, 47, 60], T19: [0, 2, 7, 11, 31, 40, 52, 63]}
BUDGET = 100  # < 8 cols x 16 rows = 128, so an over-budget in-scope patch is possible


def sha(b):
    return hashlib.sha256(b).hexdigest()


# ------------------------------------------------------------------ synthetic checkpoint
def write_safetensors(path, tensors, metadata=None):
    """tensors: list of (name, dtype_str, shape, raw_bytes). Raw writer; no torch."""
    head, off, blobs = {}, 0, []
    for name, dt, shape, raw in tensors:
        head[name] = {"dtype": dt, "shape": shape, "data_offsets": [off, off + len(raw)]}; off += len(raw); blobs.append(raw)
    if metadata:
        head["__metadata__"] = metadata
    hb = json.dumps(head, separators=(",", ":")).encode()
    hb += b" " * ((8 - len(hb) % 8) % 8)
    Path(path).write_bytes(struct.pack("<Q", len(hb)) + hb + b"".join(blobs))


def make_parent(path, seed=0, filler=65536):
    rng = np.random.default_rng(seed)
    bf = lambda n: (rng.integers(0x3c00, 0x3f80, n, dtype=np.uint16) ^ rng.integers(0, 2, n, dtype=np.uint16) << 15).astype("<u2").tobytes()
    t = [("model.embed_tokens.weight", "BF16", [32, 16], bf(512)),
         (T15, "BF16", SHAPE, bf(SHAPE[0] * SHAPE[1])),
         ("model.norm.weight", "F32", [16], rng.standard_normal(16).astype("<f4").tobytes()),
         (T19, "BF16", SHAPE, bf(SHAPE[0] * SHAPE[1])),
         ("model.filler.weight", "BF16", [filler], bf(filler))]
    write_safetensors(path, t, {"format": "pt"})
    return Path(path).read_bytes()


def tensor_region(parent: bytes, name):
    t = svc.parse_safetensors(parent)[name]
    return t["start"], t["end"]


def grant_for(parent: bytes, case="TEST-CASE002-A5", run="TEST-RUN-1", nonce="TEST-SCOPE-NONCE-1"):
    tens = []
    for n in (T15, T19):
        s, e = tensor_region(parent, n)
        tens.append({"name": n, "shape": SHAPE, "allowed_columns": ALLOWED[n], "max_columns": len(ALLOWED[n]),
                     "max_targeted_entries": BUDGET, "base_tensor_sha256": sha(parent[s:e])})
    return {"case_id": case, "manifest_timing": "PROSPECTIVE", "label": "TEST_ONLY synthetic grant",
            "authorization": {"parent_model_repo": "TEST/synthetic", "parent_revision": "TEST", "authorization_epoch": "TEST-EPOCH-1",
                              "parent_checkpoint_sha256": sha(parent),
                              "run_id": run, "nonce": nonce, "require_exact_tensor_set": True, "max_tensors": 2,
                              "allowed_forms": ["sparse"], "allowed_dtype": "bfloat16", "tensors": tens}}


# ------------------------------------------------------------------ keys / policy
class Signer:
    def __init__(self, principal="TEST-approver-gina-proxy", key_id="TEST-key-1", roles=("patch_approver",)):
        self.sk = Ed25519PrivateKey.generate(); self.principal = principal; self.key_id = key_id; self.roles = list(roles)

    def policy_entry(self):
        pub = self.sk.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return {"key_id": self.key_id, "principal": self.principal, "roles": self.roles, "enabled": True,
                "public_key_b64": base64.b64encode(pub).decode(), "label": "TEST_ONLY"}

    def pem(self):
        return self.sk.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())

    def sign(self, approval):
        sig = self.sk.sign(svc.DOMAIN + svc.canonical(approval))
        return {"approval": approval, "key_id": self.key_id, "algorithm": "Ed25519", "signature_b64": base64.b64encode(sig).decode()}


def policy(*signers):
    return {"schema": "case002-a5-trust-policy/v0.1", "label": "TEST_ONLY", "trusted_signers": [s.policy_entry() for s in signers]}


# ------------------------------------------------------------------ patches
def make_patch(parent: bytes, seed=1, n=40, cols=None, extra_tensor=False, swap_tensor=False, idx_dtype="<i8", val_dtype="<u2", count=None):
    """Return (manifest_bytes, npz_bytes, child_bytes, child_tensor_sha). Child computed independently of the service."""
    rng = np.random.default_rng(seed)
    names = [T15, "model.embed_tokens.weight" if swap_tensor else T19] + (["model.embed_tokens.weight"] if extra_tensor else [])
    child = bytearray(parent); man = {"tensors": []}; arrays = {}; tsha = {}
    for i, name in enumerate(names):
        st = svc.parse_safetensors(parent)[name]; shape = st["shape"]; ncols = shape[-1] if len(shape) > 1 else 1
        c = (cols or ALLOWED).get(name, list(range(ncols))) if name in (T15, T19) else list(range(ncols))
        cells = np.array([r * ncols + cc for r in range(shape[0]) for cc in c], dtype=np.int64)
        k = count if count is not None else min(n, len(cells))
        idx = np.sort(rng.choice(cells, size=k, replace=False)).astype(np.int64)
        base = np.frombuffer(parent[st["start"]:st["end"]], "<u2").copy()
        val = (base[idx] ^ np.uint16(0x0001)).astype("<u2")  # flip lowest mantissa bit: always a change
        base[idx] = val; child[st["start"]:st["end"]] = base.tobytes(); tsha[name] = sha(base.tobytes())
        arrays[f"t{i}_idx"] = idx.astype(idx_dtype); arrays[f"t{i}_val"] = val.astype(val_dtype)
        man["tensors"].append({"name": name, "shape": shape, "dtype": "bfloat16", "form": "sparse", "changed_entries": int(k)})
    b = io.BytesIO(); np.savez(b, **arrays)
    return json.dumps(man, indent=1).encode(), b.getvalue(), bytes(child), tsha


def approval_for(grant, parent, manifest, npz, child, tsha, signer, approval_nonce="TEST-APPROVAL-NONCE-1",
                 proposer="TEST-agent-run-1", **over):
    S = grant["authorization"]
    a = {"schema": svc.APPROVAL_SCHEMA, "phase": "POST_SUBMISSION_PATCH_APPROVAL", "label": "TEST_ONLY",
         "case_id": grant["case_id"], "run_id": S["run_id"], "authorization_epoch": S["authorization_epoch"],
         "scope_nonce": S["nonce"], "scope_grant_sha256": sha(svc.canonical(grant)), "approval_nonce": approval_nonce,
         "parent_checkpoint_sha256": sha(parent), "patch": {"manifest_sha256": sha(manifest), "payload_sha256": sha(npz)},
         "claimed_child_tensor_sha256": {k: v for k, v in tsha.items() if k in (T15, T19)},
         "claimed_child_checkpoint_sha256": sha(child), "approver_principal": signer.principal, "proposer_principal": proposer}
    a.update(over)
    return a


def stage(d, parent, manifest, npz, envelope):
    d = Path(d); d.mkdir(parents=True, exist_ok=True)
    (d / "parent.safetensors").write_bytes(parent); (d / "manifest.json").write_bytes(manifest); (d / "edit.npz").write_bytes(npz)
    if envelope is not None:
        (d / "approval.signed.json").write_bytes(json.dumps(envelope).encode() if isinstance(envelope, dict) else envelope)
    return d


# ------------------------------------------------------------------ CLI
def cli(*args, env=None, user=None):
    cmd = [sys.executable, str(SERVICE), *map(str, args)]
    if user:
        cmd = ["setpriv", f"--reuid={user}", f"--regid={user}", "--init-groups", *cmd]
    p = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, **(env or {})})
    try:
        out = json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        raise AssertionError(f"no JSON output rc={p.returncode}\nstdout={p.stdout}\nstderr={p.stderr}")
    return p.returncode, out


def spawn(*args, env=None):
    return subprocess.Popen([sys.executable, str(SERVICE), *map(str, args)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env={**os.environ, **(env or {})})


def wait_for(path, timeout=60):
    t = time.time()
    while not Path(path).exists():
        if time.time() - t > timeout:
            raise AssertionError(f"timeout waiting for {path}")
        time.sleep(0.02)


def receipts(state):
    return [json.loads(l) for l in (Path(state) / "receipts.jsonl").read_text().splitlines() if l]
