#!/usr/bin/env python3
"""Case 002 A5 — trusted apply-and-receipt service (v0.1.1 = A5.1 hardening: full-parent pin, journal-before-reserve, external head anchors).

Applies one exact, signed, in-scope sparse BF16 patch to one exact parent checkpoint, from private
copies, and records a hash-chained receipt for every APPLIED / DENIED / CRASHED / ERROR outcome.
See A5_SPEC.md for requirements R1-R10.

Adapted from Case 001 v0.3 approval_gate.py @ 1bf50296 (canonical JSON, duplicate-key rejection,
Ed25519 envelope check, proposer != approver, strict payload typing, sqlite one-time reservation).
The signing domain is new, so Case 001 signatures cannot be replayed here.

Service state directory (owned by the service OS user, mode 0700):
  config/grant.json  config/trust_policy.json  config/revocations.json  config/service_config.json
  nonces.sqlite  lock  receipts.jsonl  receipts.head  inflight/  work/  children/ (or config'd path)
Staging directory (agent-writable): parent.safetensors  manifest.json  edit.npz  approval.signed.json
"""
from __future__ import annotations

import argparse, base64, errno, fcntl, hashlib, io, json, os, re, shutil, sqlite3, stat, struct, sys, time, uuid
from pathlib import Path

import numpy as np
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

VERSION = "case002-a5-apply-service/v0.1.1"
DOMAIN = b"BALANCE-LENS-CASE002-A5-APPLY/v0.1\x00"
APPROVAL_SCHEMA = "case002-a5-approval/v0.1"
HEX64 = re.compile(r"^[a-f0-9]{64}$")
STAGING_FILES = ("parent.safetensors", "manifest.json", "edit.npz", "approval.signed.json")
GENESIS = "0" * 64


class Denied(Exception):
    """Request refused before anything was reserved or written."""


class ChainBroken(Exception):
    """Receipt chain failed verification; the service refuses to operate."""


# ---------------------------------------------------------------- canonical JSON / hashing (Case 001)
def no_duplicate_keys(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise Denied(f"duplicate JSON key: {k}")
        d[k] = v
    return d


def loads(b: bytes):
    try:
        return json.loads(b.decode("utf-8"), object_pairs_hook=no_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise Denied(f"malformed JSON: {e.__class__.__name__}") from e


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha(b) -> str:
    return hashlib.sha256(b).hexdigest()


def sha_path(p, chunk=1 << 24) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(chunk), b""):
            h.update(blk)
    return h.hexdigest()


def req_hex(v, name):
    if not isinstance(v, str) or not HEX64.fullmatch(v):
        raise Denied(f"{name}: expected lowercase SHA-256 hex")


def fsync_dir(d):
    fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes):
    tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data); os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path); fsync_dir(path.parent)


# ---------------------------------------------------------------- service state
class State:
    def __init__(self, root):
        self.root = Path(root)
        self.cfg_dir = self.root / "config"
        self.receipts = self.root / "receipts.jsonl"
        self.head = self.root / "receipts.head"
        self.inflight = self.root / "inflight"
        self.work = self.root / "work"
        self.db = self.root / "nonces.sqlite"
        cfg = self.service_config()
        self.children = Path(cfg.get("children_dir") or (self.root / "children"))
        self.hooks_enabled = bool(cfg.get("test_hooks_enabled", False))
        self.hook_dir = Path(cfg["hook_dir"]) if cfg.get("hook_dir") else None

    def service_config(self):
        p = self.root / "config" / "service_config.json"
        return loads(p.read_bytes()) if p.exists() else {}

    def read_cfg(self, name) -> bytes:
        return (self.cfg_dir / name).read_bytes()


def init_state(root, grant: dict, policy: dict, service_config: dict | None = None):
    """Admin action: create a fresh state directory (mode 0700) with frozen config."""
    root = Path(root)
    for d in (root, root / "config", root / "inflight", root / "work"):
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
    (root / "config" / "grant.json").write_bytes(canonical(grant))
    (root / "config" / "trust_policy.json").write_bytes(canonical(policy))
    (root / "config" / "revocations.json").write_bytes(canonical({"revoked_approval_nonces": [], "revoked_approval_sha256": []}))
    (root / "config" / "service_config.json").write_bytes(canonical(service_config or {"test_hooks_enabled": False}))
    if not (root / "config" / "state_id").exists():
        (root / "config" / "state_id").write_text(uuid.uuid4().hex)
    st = State(root)
    st.children.mkdir(mode=0o700, parents=True, exist_ok=True)
    conn = sqlite3.connect(str(st.db))
    conn.execute("CREATE TABLE IF NOT EXISTS approvals (approval_nonce TEXT PRIMARY KEY, scope_nonce TEXT UNIQUE NOT NULL, "
                 "approval_sha256 TEXT NOT NULL, request_id TEXT NOT NULL, status TEXT NOT NULL, updated REAL NOT NULL)")
    conn.commit(); conn.close()
    if not st.receipts.exists():
        st.receipts.write_bytes(b"")
        atomic_write(st.head, canonical({"seq": 0, "sha256": GENESIS}))
    return st


# ---------------------------------------------------------------- hooks (test only, admin-enabled)
def hook(st: State, point: str, timeout=120):
    if not st.hooks_enabled or st.hook_dir is None:
        return
    if point not in [x for x in os.environ.get("BL_A5_HOOKS", "").split(",") if x]:
        return
    (st.hook_dir / f"{point}.reached").write_text(str(os.getpid()))
    t = time.time()
    while not (st.hook_dir / f"{point}.go").exists():
        if time.time() - t > timeout:
            raise RuntimeError(f"hook {point} timed out")
        time.sleep(0.05)


# ---------------------------------------------------------------- receipts (hash chain)
def read_chain(st: State):
    """Return (receipts, problems). problems == [] means the chain and head agree."""
    problems, recs, prev = [], [], GENESIS
    raw = st.receipts.read_bytes() if st.receipts.exists() else b""
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines = lines[:-1]
    elif raw:
        problems.append("receipt log does not end with newline")
    for i, line in enumerate(lines, 1):
        try:
            r = json.loads(line, object_pairs_hook=no_duplicate_keys)
        except Exception:
            problems.append(f"receipt {i}: unparseable"); break
        if canonical(r) != line:
            problems.append(f"receipt {i}: not canonical (edited?)")
        if r.get("seq") != i:
            problems.append(f"receipt {i}: seq {r.get('seq')} != {i} (deleted or reordered?)")
        if r.get("prev_sha256") != prev:
            problems.append(f"receipt {i}: prev_sha256 mismatch (edited or deleted predecessor)")
        prev = sha(line); recs.append(r)
    try:
        head = loads(st.head.read_bytes())
    except Exception:
        return recs, problems + ["head file missing or unreadable"]
    if head.get("seq") != len(recs) or head.get("sha256") != prev:
        problems.append(f"head mismatch: head seq {head.get('seq')} vs log {len(recs)} (truncated tail or unrecorded append)")
    return recs, problems


def require_chain(st: State):
    recs, problems = read_chain(st)
    if problems:
        raise ChainBroken("; ".join(problems))
    return recs


def append_receipt(st: State, event: str, body: dict):
    recs = require_chain(st)
    prev = sha(canonical(recs[-1])) if recs else GENESIS
    r = {"seq": len(recs) + 1, "prev_sha256": prev, "event": event, "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
         "service_version": VERSION, **body}
    line = canonical(r)
    fd = os.open(st.receipts, os.O_WRONLY | os.O_APPEND)
    try:
        os.write(fd, line + b"\n"); os.fsync(fd)
    finally:
        os.close(fd)
    atomic_write(st.head, canonical({"seq": r["seq"], "sha256": sha(line)}))
    return r


def repair_head_lag(st: State):
    """Crash between log append and head update: advance head by exactly one well-chained entry."""
    recs, problems = read_chain(st)
    if len(problems) == 1 and problems[0].startswith("head mismatch"):
        head = loads(st.head.read_bytes())
        if head.get("seq") == len(recs) - 1 and (len(recs) == 1 and head.get("sha256") == GENESIS or
                                                  len(recs) > 1 and head.get("sha256") == sha(canonical(recs[-2]))):
            atomic_write(st.head, canonical({"seq": len(recs), "sha256": sha(canonical(recs[-1]))}))
            return True
    return False


# ---------------------------------------------------------------- nonce DB
def db(st):
    if st.db.is_symlink():
        raise ChainBroken("nonce DB is a symlink")
    c = sqlite3.connect(str(st.db), timeout=30, isolation_level=None)
    c.execute("PRAGMA busy_timeout=30000")
    return c


def nonce_status(st, approval_nonce):
    c = db(st)
    try:
        row = c.execute("SELECT status FROM approvals WHERE approval_nonce=?", (approval_nonce,)).fetchone()
        return row[0] if row else None
    finally:
        c.close()


def reserve_nonce(st, approval_nonce, scope_nonce, approval_sha, req):
    c = db(st)
    try:
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute("INSERT INTO approvals VALUES (?,?,?,?,?,?)", (approval_nonce, scope_nonce, approval_sha, req, "RESERVED", time.time()))
            hook(st, "during_reserve")  # test-only kill point inside the uncommitted transaction
            c.execute("COMMIT")
        except sqlite3.IntegrityError as e:
            c.execute("ROLLBACK")
            raise Denied("approval or scope nonce already reserved/used (replay)") from e
    finally:
        c.close()


def nonce_row(st, approval_nonce):
    c = db(st)
    try:
        return c.execute("SELECT status, request_id FROM approvals WHERE approval_nonce=?", (approval_nonce,)).fetchone()
    finally:
        c.close()


def burn_after_crash(st, info):
    """Fix 2: make the approval unusable whether or not its reservation committed. Returns a note for the receipt."""
    row = nonce_row(st, info["approval_nonce"])
    if row is None:
        c = db(st)
        try:
            c.execute("INSERT INTO approvals VALUES (?,?,?,?,?,?)", (info["approval_nonce"], info.get("scope_nonce") or f"UNKNOWN-{info['request_id']}",
                                                                     info["approval_sha256"], info["request_id"], "BURNED_CRASH", time.time()))
            return "reservation had not committed; approval nonce now recorded as BURNED_CRASH"
        except sqlite3.IntegrityError:
            return "reservation had not committed; scope nonce already consumed by another approval, so this approval cannot reserve"
        finally:
            c.close()
    if row[1] == info["request_id"]:
        set_nonce(st, info["approval_nonce"], "BURNED_CRASH")
        return "reservation had committed; nonce burned"
    return f"approval nonce owned by another request ({row[0]}); left unchanged"


def set_nonce(st, approval_nonce, status):
    c = db(st)
    try:
        c.execute("UPDATE approvals SET status=?, updated=? WHERE approval_nonce=?", (status, time.time(), approval_nonce))
    finally:
        c.close()


# ---------------------------------------------------------------- parsing / checks
def parse_safetensors(b: bytes):
    if len(b) < 8:
        raise Denied("parent: too short")
    n = struct.unpack("<Q", b[:8])[0]
    if n > len(b) - 8 or n > 100_000_000:
        raise Denied("parent: bad header length")
    head = loads(b[8:8 + n]); base = 8 + n
    out = {}
    for k, v in head.items():
        if k == "__metadata__":
            continue
        a, e = v["data_offsets"]
        if not (0 <= a <= e <= len(b) - base):
            raise Denied(f"parent: tensor {k} offsets out of range")
        out[k] = {"dtype": v["dtype"], "shape": v["shape"], "start": base + a, "end": base + e}
    return out


def verify_envelope(env: dict, policy: dict):
    if set(env) != {"approval", "key_id", "algorithm", "signature_b64"}:
        raise Denied("signed envelope: incorrect fields")
    if env["algorithm"] != "Ed25519":
        raise Denied("signature algorithm not Ed25519")
    if policy.get("schema") != "case002-a5-trust-policy/v0.1":
        raise Denied("wrong trust-policy schema")
    keys = [k for k in policy.get("trusted_signers", []) if k.get("key_id") == env["key_id"]]
    if len(keys) != 1:
        raise Denied("signer key absent or duplicated in trust policy")
    k = keys[0]
    if not k.get("enabled", False) or "patch_approver" not in k.get("roles", []):
        raise Denied("signer lacks enabled patch_approver role")
    a = env["approval"]
    if not isinstance(a, dict):
        raise Denied("approval is not an object")
    if a.get("approver_principal") != k.get("principal"):
        raise Denied("claimed approver does not match trust policy principal")
    if not a.get("proposer_principal") or a.get("proposer_principal") == k.get("principal"):
        raise Denied("approval not independent of proposer")
    try:
        pub = base64.b64decode(k["public_key_b64"], validate=True)
        sig = base64.b64decode(env["signature_b64"], validate=True)
        Ed25519PublicKey.from_public_bytes(pub).verify(sig, DOMAIN + canonical(a))
    except (InvalidSignature, ValueError, KeyError, TypeError) as e:
        raise Denied("invalid approval signature") from e
    return k["principal"]


def check_revoked(st: State, approval_nonce, approval_sha):
    r = loads(st.read_cfg("revocations.json"))
    if approval_nonce in r.get("revoked_approval_nonces", []) or approval_sha in r.get("revoked_approval_sha256", []):
        raise Denied("approval revoked")


def build_child(parent: bytes, manifest: dict, npz: bytes, grant: dict):
    """Independent scope check (R3) and in-memory child construction."""
    S = grant["authorization"]
    allowed = {t["name"]: t for t in S["tensors"]}
    if S.get("allowed_dtype") != "bfloat16" or "sparse" not in S.get("allowed_forms", []):
        raise Denied("grant must allow sparse bfloat16")
    tensors = manifest.get("tensors")
    if not isinstance(tensors, list) or not tensors:
        raise Denied("manifest has no tensor list")
    names = [t.get("name") for t in tensors]
    if len(names) != len(set(names)):
        raise Denied("duplicate tensor in manifest")
    if len(names) > S["max_tensors"]:
        raise Denied("too many tensors")
    if S.get("require_exact_tensor_set") and set(names) != set(allowed):
        raise Denied(f"tensor set {sorted(names)} != grant {sorted(allowed)}")
    ptens = parse_safetensors(parent)
    try:
        z = np.load(io.BytesIO(npz), allow_pickle=False)
    except Exception as e:
        raise Denied(f"payload is not a valid npz: {e.__class__.__name__}") from e
    expected = {f"t{i}_{s}" for i in range(len(tensors)) for s in ("idx", "val")}
    if set(z.files) != expected:
        raise Denied("unexpected or missing payload arrays")
    child = bytearray(parent)
    plan = {}
    for i, t in enumerate(tensors):
        name = t["name"]; g = allowed.get(name)
        if g is None:
            raise Denied(f"tensor not permitted: {name}")
        if t.get("dtype") != "bfloat16" or t.get("form") != "sparse":
            raise Denied(f"{name}: manifest dtype/form must be bfloat16/sparse")
        pt = ptens.get(name)
        if pt is None or pt["dtype"] != "BF16" or list(pt["shape"]) != list(g["shape"]) or list(t.get("shape", [])) != list(g["shape"]):
            raise Denied(f"{name}: parent/manifest/grant shape or dtype mismatch")
        idx, val = z[f"t{i}_idx"], z[f"t{i}_val"]
        if idx.dtype != np.dtype("<i8") or idx.ndim != 1:
            raise Denied(f"{name}: indices must be flat little-endian int64")
        if val.dtype != np.dtype("<u2") or val.ndim != 1:
            raise Denied(f"{name}: values must be flat uint16 BF16 bit patterns")
        if len(idx) != len(val) or len(idx) != t.get("changed_entries"):
            raise Denied(f"{name}: payload length / manifest changed_entries mismatch")
        rows, cols = g["shape"]
        if len(idx) and (idx.min() < 0 or idx.max() >= rows * cols):
            raise Denied(f"{name}: index out of range")
        if len(np.unique(idx)) != len(idx):
            raise Denied(f"{name}: duplicate indices")
        ucols = np.unique(idx % cols)
        if not set(ucols.tolist()) <= set(g["allowed_columns"]):
            raise Denied(f"{name}: column outside grant")
        if len(ucols) > g.get("max_columns", len(g["allowed_columns"])):
            raise Denied(f"{name}: too many columns")
        if len(idx) > g["max_targeted_entries"]:
            raise Denied(f"{name}: entry budget exceeded")
        region = parent[pt["start"]:pt["end"]]
        if sha(region) != g["base_tensor_sha256"]:
            raise Denied(f"{name}: parent tensor hash != grant base_tensor_sha256")
        arr = np.frombuffer(region, dtype="<u2").copy()
        arr[idx] = val
        child[pt["start"]:pt["end"]] = arr.tobytes()
        plan[name] = {"start": pt["start"], "end": pt["end"], "idx": np.sort(idx), "child_sha256": sha(arr.tobytes()),
                      "changed_entries": int(len(idx))}
    return child, plan


def verify_written(path: Path, parent: bytes, plan: dict, expect_sha: str, chunk=1 << 24):
    """R4: re-read the written child; hash must match; bytes outside permitted tensors identical; changes ⊆ approved indices."""
    h = hashlib.sha256(); off = 0
    allowed_bytes = []
    for p in plan.values():
        b = p["start"] + 2 * p["idx"]
        allowed_bytes.append(np.concatenate([b, b + 1]))
    allowed_bytes = np.unique(np.concatenate(allowed_bytes)) if allowed_bytes else np.array([], dtype=np.int64)
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(chunk), b""):
            h.update(blk)
            ref = parent[off:off + len(blk)]
            if len(ref) != len(blk):
                raise RuntimeError("child length differs from parent")
            if blk != ref:
                d = np.nonzero(np.frombuffer(blk, np.uint8) != np.frombuffer(ref, np.uint8))[0] + off
                if not np.all(np.isin(d, allowed_bytes, assume_unique=False)):
                    raise RuntimeError("written child differs from parent outside approved entries")
            off += len(blk)
    if off != len(parent):
        raise RuntimeError("child length differs from parent")
    if h.hexdigest() != expect_sha:
        raise RuntimeError("written child hash mismatch")


# ---------------------------------------------------------------- ingest (R2)
def ingest(staging: Path, workdir: Path):
    workdir.mkdir(mode=0o700)
    data, hashes = {}, {}
    for name in STAGING_FILES:
        try:
            fd = os.open(staging / name, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            raise Denied(f"missing staging file: {name}")
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise Denied(f"staging file is a symlink: {name}")
            raise
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise Denied(f"staging file not regular: {name}")
            out = os.open(workdir / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                while True:
                    blk = os.read(fd, 1 << 24)
                    if not blk:
                        break
                    os.write(out, blk)
                os.fsync(out)
            finally:
                os.close(out)
        finally:
            os.close(fd)
    fsync_dir(workdir)
    for name in STAGING_FILES:  # everything downstream uses these bytes, read back from the private copy
        data[name] = (workdir / name).read_bytes(); hashes[name] = sha(data[name])
    return data, hashes


# ---------------------------------------------------------------- apply / recover
def remove_quiet(p: Path):
    try:
        p.unlink()
    except FileNotFoundError:
        pass


def recover(st: State):
    """Called under the service lock. Any journal left behind belongs to a dead process (kernel released its lock)."""
    actions = []
    if repair_head_lag(st):
        actions.append("head advanced over one well-chained unrecorded receipt")
    recs = require_chain(st)
    applied_reqs = {r.get("request_id") for r in recs if r.get("event") == "APPLIED"}
    for j in sorted(st.inflight.glob("*.json")):
        info = loads(j.read_bytes())
        if info["request_id"] in applied_reqs:  # crashed after receipt: child is valid, only tidy up
            set_nonce(st, info["approval_nonce"], "APPLIED")
        else:
            for p in (info["partial_path"], info["final_path"]):
                remove_quiet(Path(p))
            note = burn_after_crash(st, info)
            append_receipt(st, "CRASHED", {"request_id": info["request_id"], "approval_sha256": info["approval_sha256"], "nonce_note": note,
                                           "approval_nonce": info["approval_nonce"], "parent_sha256": info["parent_sha256"],
                                           "manifest_sha256": info["manifest_sha256"], "payload_sha256": info["payload_sha256"],
                                           "intended_child_sha256": info["child_sha256"],
                                           "reason": "in-flight apply found at recovery; partial/unreceipted child removed; nonce burned"})
        shutil.rmtree(st.work / info["request_id"], ignore_errors=True)
        remove_quiet(j); fsync_dir(st.inflight)
        actions.append(f"recovered {info['request_id']}")
    for w in st.work.iterdir():  # work copies without a journal: crashed before reservation
        shutil.rmtree(w, ignore_errors=True)
    return actions


class Lock:
    def __init__(self, st):
        self.p = st.root / "lock"

    def __enter__(self):
        self.fd = os.open(self.p, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        fcntl.flock(self.fd, fcntl.LOCK_UN); os.close(self.fd)


def apply(state_root, staging) -> dict:
    st = State(state_root); staging = Path(staging)
    with Lock(st):
        recover(st)
        require_chain(st)
        req = uuid.uuid4().hex
        base = {"request_id": req}
        reserved = False; journal = None; partial = final = None; info = {}
        try:
            # ---- phase 1: ingest + checks (denials here do not burn the nonce)
            data, hashes = ingest(staging, st.work / req)
            base.update({"parent_sha256": hashes["parent.safetensors"], "manifest_sha256": hashes["manifest.json"],
                         "payload_sha256": hashes["edit.npz"], "signed_approval_file_sha256": hashes["approval.signed.json"]})
            hook(st, "after_ingest")
            grant_b = st.read_cfg("grant.json"); grant = loads(grant_b); policy = loads(st.read_cfg("trust_policy.json"))
            env = loads(data["approval.signed.json"])
            if not isinstance(env, dict):
                raise Denied("signed approval is not an object")
            approver = verify_envelope(env, policy)
            a = env["approval"]; S = grant["authorization"]
            asha = sha(DOMAIN + canonical(a))
            base.update({"approval_sha256": asha, "approval_nonce": a.get("approval_nonce"), "approver": approver,
                         "proposer": a.get("proposer_principal")})
            if a.get("schema") != APPROVAL_SCHEMA or a.get("phase") != "POST_SUBMISSION_PATCH_APPROVAL":
                raise Denied("wrong approval schema/phase")
            if grant.get("manifest_timing") != "PROSPECTIVE":
                raise Denied("grant is not PROSPECTIVE")
            for k, v in (("case_id", grant.get("case_id")), ("run_id", S.get("run_id")), ("authorization_epoch", S.get("authorization_epoch")),
                         ("scope_nonce", S.get("nonce")), ("scope_grant_sha256", sha(grant_b))):
                if a.get(k) != v:
                    raise Denied(f"approval {k} does not match grant")
            for k in ("approval_nonce",):
                if not isinstance(a.get(k), str) or not a[k] or a[k] == S.get("nonce"):
                    raise Denied("approval nonce missing or equal to scope nonce")
            gp = S.get("parent_checkpoint_sha256")
            if not isinstance(gp, str) or not HEX64.fullmatch(gp):
                raise Denied("grant lacks a full parent_checkpoint_sha256 pin")
            if hashes["parent.safetensors"] != gp:
                raise Denied("parent checkpoint does not match grant (full-file pin)")
            req_hex(a.get("parent_checkpoint_sha256"), "parent_checkpoint_sha256")
            if a["parent_checkpoint_sha256"] != hashes["parent.safetensors"]:
                raise Denied("parent checkpoint does not match approval (tampered or wrong parent)")
            p = a.get("patch") or {}
            if p.get("manifest_sha256") != hashes["manifest.json"] or p.get("payload_sha256") != hashes["edit.npz"]:
                raise Denied("patch does not match approval (tampered)")
            check_revoked(st, a["approval_nonce"], asha)
            if nonce_status(st, a["approval_nonce"]) is not None:
                raise Denied(f"approval nonce already {nonce_status(st, a['approval_nonce'])} (replay)")
            child, plan = build_child(data["parent.safetensors"], loads(data["manifest.json"]), data["edit.npz"], grant)
            child_sha = sha(child)
            claims = a.get("claimed_child_tensor_sha256") or {}
            if claims != {n: v["child_sha256"] for n, v in plan.items()}:
                raise Denied("claimed child tensor hashes do not match computed child")
            if a.get("claimed_child_checkpoint_sha256") != child_sha:
                raise Denied("claimed child checkpoint hash does not match computed child")
            base.update({"child_sha256": child_sha, "child_tensor_sha256": {n: v["child_sha256"] for n, v in plan.items()},
                         "changed_entries": {n: v["changed_entries"] for n, v in plan.items()}})
            # ---- phase 2: journal FIRST (A5.1 Fix 2), then reserve; from the journal on, a crash is always recoverable
            partial = st.children / f".partial-{req}"; final = st.children / f"{child_sha}.safetensors"
            if final.exists():
                raise Denied("a child with this hash already exists")
            info = {"request_id": req, "approval_nonce": a["approval_nonce"], "scope_nonce": a["scope_nonce"], "approval_sha256": asha,
                    "partial_path": str(partial), "final_path": str(final), "child_sha256": child_sha, "parent_sha256": base["parent_sha256"],
                    "manifest_sha256": base["manifest_sha256"], "payload_sha256": base["payload_sha256"]}
            journal = st.inflight / f"{req}.json"; atomic_write(journal, canonical(info))
            hook(st, "after_journal")
            reserve_nonce(st, a["approval_nonce"], a["scope_nonce"], asha, req); reserved = True
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                mv = memoryview(child); half = len(mv) // 2
                for part, pt in ((mv[:half], "mid_write"), (mv[half:], None)):
                    o = 0
                    while o < len(part):
                        o += os.write(fd, part[o:o + (1 << 24)])
                    if pt:
                        hook(st, pt)
                os.fsync(fd)
            finally:
                os.close(fd)
            verify_written(partial, data["parent.safetensors"], plan, child_sha)
            hook(st, "before_commit")
            check_revoked(st, a["approval_nonce"], asha)  # R7: re-check right before marking valid
            os.rename(partial, final); fsync_dir(st.children)
            hook(st, "after_rename")
            r = append_receipt(st, "APPLIED", {**base, "child_path": str(final)})
            set_nonce(st, a["approval_nonce"], "APPLIED")
            remove_quiet(journal); fsync_dir(st.inflight)
            shutil.rmtree(st.work / req, ignore_errors=True)
            return {"status": "APPLIED", "receipt_seq": r["seq"], "child_path": str(final), **base}
        except ChainBroken:
            raise
        except Exception as e:
            kind = "DENIED" if isinstance(e, Denied) else "ERROR"
            reason = str(e) if isinstance(e, Denied) else f"{e.__class__.__name__}: {e}"
            if reserved:
                for q in (partial, final):
                    if q is not None:
                        remove_quiet(q)
                set_nonce(st, info["approval_nonce"], f"BURNED_{kind}")
            r = append_receipt(st, kind, {**base, "reason": reason, "nonce_burned": reserved})
            if journal is not None:
                remove_quiet(journal)
            shutil.rmtree(st.work / req, ignore_errors=True)
            return {"status": kind, "reason": reason, "receipt_seq": r["seq"], "nonce_burned": reserved, **base}


ANCHOR_SCHEMA = "case002-a5-receipt-anchor/v0.1"


def state_id(st: State) -> str:
    return (st.cfg_dir / "state_id").read_text().strip()


def head_anchor(st: State) -> dict:
    """Fix 3: a publishable statement of the current head (commit it somewhere the service cannot rewrite)."""
    recs = require_chain(st)
    return {"anchor_schema": ANCHOR_SCHEMA, "state_id": state_id(st), "seq": len(recs),
            "head_sha256": sha(canonical(recs[-1])) if recs else GENESIS,
            "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "service_version": VERSION}


def load_anchors(path) -> list:
    out = []
    for line in Path(path).read_text().splitlines():
        if line.strip():
            a = json.loads(line, object_pairs_hook=no_duplicate_keys)
            if a.get("anchor_schema") != ANCHOR_SCHEMA:
                raise ChainBroken("unknown anchor schema")
            out.append(a)
    if not out:
        raise ChainBroken("anchor file is empty")
    return out


def check_anchors(st: State, recs: list, anchors: list):
    """The current log must contain every published head exactly. Raises ChainBroken otherwise."""
    sid = state_id(st)
    for a in anchors:
        if a.get("state_id") != sid:
            raise ChainBroken(f"anchor is for another state ({a.get('state_id')})")
        n = a.get("seq")
        if not isinstance(n, int) or n < 0 or n > len(recs):
            raise ChainBroken(f"anchored seq {n} missing from log of {len(recs)} (log rewritten or truncated)")
        got = GENESIS if n == 0 else sha(canonical(recs[n - 1]))
        if got != a.get("head_sha256"):
            raise ChainBroken(f"receipt {n} differs from published anchor (log rewritten)")


def is_valid_child(state_root, child_path, anchors=None) -> bool:
    st = State(state_root)
    try:
        recs = require_chain(st)
        if anchors is not None:
            check_anchors(st, recs, anchors)
    except ChainBroken:
        return False
    p = Path(child_path)
    if not p.is_file() or p.is_symlink() or p.name.startswith(".partial"):
        return False
    h = sha_path(p)
    return any(r.get("event") == "APPLIED" and r.get("child_sha256") == h and Path(r.get("child_path", "")).resolve() == p.resolve()
               for r in recs)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("apply"); a.add_argument("--state", required=True); a.add_argument("--staging", required=True)
    r = sub.add_parser("recover"); r.add_argument("--state", required=True)
    v = sub.add_parser("verify-chain"); v.add_argument("--state", required=True); v.add_argument("--anchor")
    c = sub.add_parser("is-valid"); c.add_argument("--state", required=True); c.add_argument("--child", required=True); c.add_argument("--anchor")
    h = sub.add_parser("head", help="print a publishable receipt-head anchor (one JSON line)"); h.add_argument("--state", required=True)
    args = ap.parse_args()
    try:
        if args.cmd == "apply":
            out = apply(args.state, args.staging); rc = {"APPLIED": 0, "DENIED": 1}.get(out["status"], 2)
        elif args.cmd == "recover":
            st = State(args.state)
            with Lock(st):
                out = {"status": "RECOVERED", "actions": recover(st)}; rc = 0
        elif args.cmd == "verify-chain":
            st = State(args.state); recs, problems = read_chain(st)
            if not problems and args.anchor:
                try:
                    check_anchors(st, recs, load_anchors(args.anchor))
                except ChainBroken as e:
                    problems = [str(e)]
            out = {"status": "OK" if not problems else "BROKEN", "receipts": len(recs), "problems": problems, "anchored": bool(args.anchor)}
            rc = 0 if not problems else 3
        elif args.cmd == "head":
            st = State(args.state)
            with Lock(st):
                out = head_anchor(st)
            print(canonical(out).decode()); return 0
        else:
            try:
                anchors = load_anchors(args.anchor) if args.anchor else None
            except ChainBroken:
                anchors = None; ok = False
            else:
                ok = is_valid_child(args.state, args.child, anchors)
            out = {"status": "VALID" if ok else "NOT_VALID"}; rc = 0 if ok else 1
    except ChainBroken as e:
        out = {"status": "REFUSED_CHAIN_BROKEN", "reason": str(e)}; rc = 3
    print(json.dumps(out, sort_keys=True, default=str))
    return rc


if __name__ == "__main__":
    sys.exit(main())
