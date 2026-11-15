#!/usr/bin/env python3
"""Case 002 A5 — trusted apply-and-receipt service (v0.2 = A5.2: anchored validity, state-bound grants, approval
expiry, signed revocations, epoch closure, revert receipts, fail-closed receipt writes).

Applies one exact, signed, in-scope sparse BF16 patch to one exact, grant-pinned parent checkpoint, from private
copies, and records a hash-chained receipt for every APPLIED / DENIED / CRASHED / ERROR / REVERTED outcome.
See A5_SPEC.md (R1-R10), A5_1_SPEC.md and A5_2_SPEC.md.

Adapted from Case 001 v0.3 approval_gate.py @ 1bf50296 (canonical JSON, duplicate-key rejection, Ed25519 envelope
check, proposer != approver, strict payload typing, sqlite one-time reservation). Signing domains are new.

Service state directory (owned by the service OS user, mode 0700):
  config/{grant,trust_policy,revocations,service_config}.json  config/state_id
  nonces.sqlite  lock  receipts.jsonl  receipts.head  inflight/  work/  reverts/  children/ (or config'd path)
Staging directory (agent-writable): parent.safetensors  manifest.json  edit.npz  approval.signed.json
"""
from __future__ import annotations

import argparse, base64, calendar, errno, fcntl, hashlib, io, json, os, re, shutil, sqlite3, stat, struct, sys, time, uuid
from pathlib import Path

import numpy as np
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

VERSION = "case002-a5-apply-service/v0.2"
DOMAIN = b"BALANCE-LENS-CASE002-A5-APPLY/v0.1\x00"
REV_DOMAIN = b"BALANCE-LENS-CASE002-A5-REVOCATIONS/v0.1\x00"
APPROVAL_SCHEMA = "case002-a5-approval/v0.1"
REV_SCHEMA = "case002-a5-revocations/v0.1"
POLICY_SCHEMA = "case002-a5-trust-policy/v0.1"
ANCHOR_SCHEMA = "case002-a5-receipt-anchor/v0.1"
HEX64 = re.compile(r"^[a-f0-9]{64}$")
UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
STAGING_FILES = ("parent.safetensors", "manifest.json", "edit.npz", "approval.signed.json")
GENESIS = "0" * 64


class Denied(Exception):
    """Request refused (before reservation: nonce untouched; after: nonce burned)."""


class ChainBroken(Exception):
    """Receipt chain or anchor failed verification; the service refuses to operate."""


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


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def parse_utc(s):
    if not isinstance(s, str) or not UTC_RE.fullmatch(s):
        raise Denied("not_after_utc missing or not YYYY-MM-DDTHH:MM:SSZ")
    return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))


def fsync_dir(d):
    fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes):
    path = Path(path); tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            o = 0
            while o < len(data):
                o += os.write(fd, data[o:])
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path); fsync_dir(path.parent)
    except BaseException:
        remove_quiet(tmp)
        raise


def remove_quiet(p):
    try:
        Path(p).unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- service state
class State:
    def __init__(self, root):
        self.root = Path(root)
        self.cfg_dir = self.root / "config"
        self.receipts = self.root / "receipts.jsonl"
        self.head = self.root / "receipts.head"
        self.inflight = self.root / "inflight"
        self.work = self.root / "work"
        self.reverts = self.root / "reverts"
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


def state_id(st: State) -> str:
    return (st.cfg_dir / "state_id").read_text().strip()


def init_state(root, grant: dict, policy: dict, service_config: dict | None = None, revocations_envelope: dict | None = None,
               state_id_: str | None = None):
    """Admin action: create a fresh state directory (mode 0700) with frozen config. The state ID comes from the grant."""
    root = Path(root)
    for d in (root, root / "config", root / "inflight", root / "work", root / "reverts"):
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
    sid = state_id_ or grant.get("authorization", {}).get("service_state_id") or uuid.uuid4().hex
    if not (root / "config" / "state_id").exists():
        (root / "config" / "state_id").write_text(sid)
    (root / "config" / "grant.json").write_bytes(canonical(grant))
    (root / "config" / "trust_policy.json").write_bytes(canonical(policy))
    (root / "config" / "revocations.json").write_bytes(canonical(revocations_envelope if revocations_envelope is not None else {"unsigned": True}))
    (root / "config" / "service_config.json").write_bytes(canonical(service_config or {"test_hooks_enabled": False}))
    st = State(root)
    st.children.mkdir(mode=0o700, parents=True, exist_ok=True)
    conn = sqlite3.connect(str(st.db))
    conn.execute("CREATE TABLE IF NOT EXISTS approvals (approval_nonce TEXT PRIMARY KEY, scope_nonce TEXT UNIQUE NOT NULL, "
                 "approval_sha256 TEXT NOT NULL, request_id TEXT NOT NULL, status TEXT NOT NULL, updated REAL NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS closed_epochs (epoch TEXT PRIMARY KEY, grant_sha256 TEXT NOT NULL, request_id TEXT NOT NULL, closed REAL NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    conn.commit(); conn.close()
    if not st.receipts.exists():
        st.receipts.write_bytes(b"")
        atomic_write(st.head, canonical({"seq": 0, "sha256": GENESIS}))
    return st


def install_grant(root, grant: dict):
    """Admin action: replace the frozen grant. Refused if its epoch is already closed (A5.2 item 12)."""
    st = State(root)
    with Lock(st):
        ep = grant.get("authorization", {}).get("authorization_epoch")
        if epoch_closed(st, ep):
            raise Denied(f"authorization epoch {ep} is closed; a new grant needs a new epoch")
        atomic_write(st.cfg_dir / "grant.json", canonical(grant))


def install_revocations(root, envelope: dict):
    """Admin action: validate a signed revocation list, record its sequence (refusing rollback), then install it."""
    # Deliberately NOT under the service lock: a revocation must reach an apply that is already in progress
    # (re-checked before commit). Sequence recording is atomic in sqlite; the file replace is atomic.
    st = State(root)
    policy = loads(st.read_cfg("trust_policy.json"))
    r = validate_revocations(st, policy, envelope)
    record_revocation_sequence(st, r["sequence"])
    atomic_write(st.cfg_dir / "revocations.json", canonical(envelope))


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
    """All-or-nothing: a failed log write or head update rolls the log back to its previous length (A5.2 item 14)."""
    recs = require_chain(st)
    prev = sha(canonical(recs[-1])) if recs else GENESIS
    r = {"seq": len(recs) + 1, "prev_sha256": prev, "event": event, "time_utc": utc_now(), "service_version": VERSION, **body}
    line = canonical(r) + b"\n"
    size = st.receipts.stat().st_size
    fd = os.open(st.receipts, os.O_WRONLY | os.O_APPEND)
    try:
        try:
            o = 0
            while o < len(line):
                o += os.write(fd, line[o:])
            os.fsync(fd)
            atomic_write(st.head, canonical({"seq": r["seq"], "sha256": sha(line[:-1])}))
        except BaseException:
            os.ftruncate(fd, size); os.fsync(fd)
            raise
    finally:
        os.close(fd)
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


# ---------------------------------------------------------------- anchors (A5.1 Fix 3; A5.2 item 8)
def head_anchor(st: State) -> dict:
    recs = require_chain(st)
    return {"anchor_schema": ANCHOR_SCHEMA, "state_id": state_id(st), "seq": len(recs),
            "head_sha256": sha(canonical(recs[-1])) if recs else GENESIS, "time_utc": utc_now(), "service_version": VERSION}


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


def check_anchors(st: State, recs: list, anchors: list) -> int:
    """Every anchor must be contained exactly in the log. Returns the highest anchored seq."""
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
    return max(a["seq"] for a in anchors)


# ---------------------------------------------------------------- nonce / epoch DB
def db(st):
    if st.db.is_symlink():
        raise ChainBroken("nonce DB is a symlink")
    c = sqlite3.connect(str(st.db), timeout=30, isolation_level=None)
    c.execute("PRAGMA busy_timeout=30000")
    return c


def nonce_row(st, approval_nonce):
    c = db(st)
    try:
        return c.execute("SELECT status, request_id FROM approvals WHERE approval_nonce=?", (approval_nonce,)).fetchone()
    finally:
        c.close()


def nonce_status(st, approval_nonce):
    r = nonce_row(st, approval_nonce)
    return r[0] if r else None


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


def set_nonce(st, approval_nonce, status):
    c = db(st)
    try:
        c.execute("UPDATE approvals SET status=?, updated=? WHERE approval_nonce=?", (status, time.time(), approval_nonce))
    finally:
        c.close()


def finalize_applied(st, approval_nonce, epoch, grant_sha, req):
    """Mark the nonce APPLIED and close the epoch in one transaction (A5.2 item 12)."""
    c = db(st)
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("UPDATE approvals SET status='APPLIED', updated=? WHERE approval_nonce=?", (time.time(), approval_nonce))
        c.execute("INSERT OR IGNORE INTO closed_epochs VALUES (?,?,?,?)", (epoch, grant_sha, req, time.time()))
        c.execute("COMMIT")
    finally:
        c.close()


def epoch_closed(st, epoch) -> bool:
    c = db(st)
    try:
        return c.execute("SELECT 1 FROM closed_epochs WHERE epoch=?", (epoch,)).fetchone() is not None
    finally:
        c.close()


def burn_after_crash(st, info):
    """A5.1 Fix 2: make the approval unusable whether or not its reservation committed. Returns a note for the receipt."""
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


# ---------------------------------------------------------------- signatures / policy
def policy_key(policy, key_id, role):
    if policy.get("schema") != POLICY_SCHEMA:
        raise Denied("wrong trust-policy schema")
    keys = [k for k in policy.get("trusted_signers", []) if k.get("key_id") == key_id]
    if len(keys) != 1:
        raise Denied("signer key absent or duplicated in trust policy")
    k = keys[0]
    if not k.get("enabled", False) or role not in k.get("roles", []):
        raise Denied(f"signer lacks enabled {role} role")
    return k


def check_sig(k, sig_b64, domain, obj, what):
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(k["public_key_b64"], validate=True)).verify(
            base64.b64decode(sig_b64, validate=True), domain + canonical(obj))
    except (InvalidSignature, ValueError, KeyError, TypeError) as e:
        raise Denied(f"invalid {what} signature") from e


def verify_envelope(env: dict, policy: dict):
    if set(env) != {"approval", "key_id", "algorithm", "signature_b64"}:
        raise Denied("signed envelope: incorrect fields")
    if env["algorithm"] != "Ed25519":
        raise Denied("signature algorithm not Ed25519")
    k = policy_key(policy, env["key_id"], "patch_approver")
    a = env["approval"]
    if not isinstance(a, dict):
        raise Denied("approval is not an object")
    if a.get("approver_principal") != k.get("principal"):
        raise Denied("claimed approver does not match trust policy principal")
    if not a.get("proposer_principal") or a.get("proposer_principal") == k.get("principal"):
        raise Denied("approval not independent of proposer")
    check_sig(k, env["signature_b64"], DOMAIN, a, "approval")
    return k["principal"]


def validate_revocations(st: State, policy: dict, env) -> dict:
    """Signature, signer role, schema, state binding and field types of a revocation envelope."""
    try:
        if not isinstance(env, dict) or set(env) != {"revocations", "key_id", "algorithm", "signature_b64"} or env["algorithm"] != "Ed25519":
            raise Denied("unsigned or malformed")
        k = policy_key(policy, env["key_id"], "revocation_signer")
        check_sig(k, env["signature_b64"], REV_DOMAIN, env["revocations"], "revocation-list")
        r = env["revocations"]
        if r.get("schema") != REV_SCHEMA:
            raise Denied("wrong schema")
        if r.get("service_state_id") != state_id(st):
            raise Denied("list is for another service state")
        seq = r.get("sequence")
        if not isinstance(seq, int) or seq < 0 or not isinstance(r.get("revoked_approval_nonces"), list) or not isinstance(r.get("revoked_approval_sha256"), list):
            raise Denied("bad fields")
    except Denied as e:
        msg = str(e)
        raise Denied(msg if msg.startswith("revocation list invalid") else f"revocation list invalid: {msg}") from e
    return r


def record_revocation_sequence(st: State, seq: int):
    """Monotonic: refuse a sequence lower than the highest ever installed or seen (rollback)."""
    c = db(st)
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT v FROM meta WHERE k='max_revocation_sequence'").fetchone()
        seen = int(row[0]) if row else -1
        if seq < seen:
            c.execute("ROLLBACK")
            raise Denied(f"revocation list invalid: sequence {seq} < previously seen {seen} (rollback)")
        if seq > seen:
            c.execute("INSERT OR REPLACE INTO meta VALUES ('max_revocation_sequence', ?)", (str(seq),))
        c.execute("COMMIT")
    finally:
        c.close()


def load_revocations(st: State, policy: dict) -> dict:
    """A5.2 item 11: the installed list must be validly signed, for this state, and not rolled back."""
    try:
        env = loads(st.read_cfg("revocations.json"))
    except FileNotFoundError:
        raise Denied("revocation list invalid: missing")
    except Denied as e:
        raise Denied(f"revocation list invalid: {e}") from e
    r = validate_revocations(st, policy, env)
    record_revocation_sequence(st, r["sequence"])
    return r


def check_live(st: State, policy: dict, a: dict, asha: str, epoch: str):
    """Checks repeated immediately before commit: revocation (signed list), expiry, epoch still open."""
    r = load_revocations(st, policy)
    if a["approval_nonce"] in r["revoked_approval_nonces"] or asha in r["revoked_approval_sha256"]:
        raise Denied("approval revoked")
    if time.time() > parse_utc(a.get("not_after_utc")):
        raise Denied("approval expired")
    if epoch_closed(st, epoch):
        raise Denied(f"authorization epoch {epoch} is closed")


# ---------------------------------------------------------------- parsing / checks
def parse_safetensors(b: bytes):
    if len(b) < 8:
        raise Denied("checkpoint: too short")
    n = struct.unpack("<Q", b[:8])[0]
    if n > len(b) - 8 or n > 100_000_000:
        raise Denied("checkpoint: bad header length")
    head = loads(b[8:8 + n]); base = 8 + n
    out = {}
    for k, v in head.items():
        if k == "__metadata__":
            continue
        a, e = v["data_offsets"]
        if not (0 <= a <= e <= len(b) - base):
            raise Denied(f"checkpoint: tensor {k} offsets out of range")
        out[k] = {"dtype": v["dtype"], "shape": v["shape"], "start": base + a, "end": base + e}
    return out


def build_child(parent: bytes, manifest: dict, npz: bytes, grant: dict):
    """Independent scope check (R3) and in-memory child construction. Also returns the inverse (revert) arrays."""
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
    child = bytearray(parent); plan = {}; inverse = {}
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
        inverse[f"t{i}_idx"] = idx.astype("<i8"); inverse[f"t{i}_orig"] = arr[idx].astype("<u2")
        arr[idx] = val
        child[pt["start"]:pt["end"]] = arr.tobytes()
        plan[name] = {"start": pt["start"], "end": pt["end"], "idx": np.sort(idx), "child_sha256": sha(arr.tobytes()),
                      "changed_entries": int(len(idx)), "i": i}
    return child, plan, inverse


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


def file_matches(path: Path, buf, expect_sha: str, chunk=1 << 24) -> bool:
    """Stream the file back and compare it byte-for-byte with buf; also require its SHA-256 (no extra full copies)."""
    mv = memoryview(buf); h = hashlib.sha256(); off = 0
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(chunk), b""):
            if mv[off:off + len(blk)] != blk:
                return False
            h.update(blk); off += len(blk)
    return off == len(mv) and h.hexdigest() == expect_sha


def write_exclusive(path: Path, data, st: State, mid_hook=None):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        mv = memoryview(data); half = len(mv) // 2
        for part, pt in ((mv[:half], mid_hook), (mv[half:], None)):
            o = 0
            while o < len(part):
                o += os.write(fd, part[o:o + (1 << 24)])
            if pt:
                hook(st, pt)
        os.fsync(fd)
    finally:
        os.close(fd)


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
                    o = 0
                    while o < len(blk):
                        o += os.write(out, blk[o:])
                os.fsync(out)
            finally:
                os.close(out)
        finally:
            os.close(fd)
    fsync_dir(workdir)
    for name in STAGING_FILES:  # everything downstream uses these bytes, read back from the private copy
        data[name] = (workdir / name).read_bytes(); hashes[name] = sha(data[name])
    return data, hashes


# ---------------------------------------------------------------- recovery / lock
def recover(st: State):
    """Called under the service lock. Any journal left behind belongs to a dead or failed-closed process."""
    actions = []
    if repair_head_lag(st):
        actions.append("head advanced over one well-chained unrecorded receipt")
    recs = require_chain(st)
    done = {(r.get("event"), r.get("request_id")) for r in recs}
    for j in sorted(st.inflight.glob("*.json")):
        info = loads(j.read_bytes()); req = info["request_id"]; kind = info.get("kind", "apply")
        if kind == "apply" and ("APPLIED", req) in done:  # crashed after the receipt: child is valid, finish bookkeeping
            finalize_applied(st, info["approval_nonce"], info["epoch"], info["grant_sha256"], req)
        elif kind == "revert" and ("REVERTED", req) in done:
            pass
        elif kind == "apply":
            for p in (info["partial_path"], info["final_path"], info["revert_payload_path"]):
                remove_quiet(p)
            note = burn_after_crash(st, info)
            append_receipt(st, "CRASHED", {"action": "apply", "request_id": req, "approval_sha256": info["approval_sha256"], "nonce_note": note,
                                           "approval_nonce": info["approval_nonce"], "parent_sha256": info["parent_sha256"],
                                           "manifest_sha256": info["manifest_sha256"], "payload_sha256": info["payload_sha256"],
                                           "intended_child_sha256": info["child_sha256"],
                                           "reason": "in-flight apply found at recovery; partial/unreceipted child removed; approval unusable"})
        else:
            for p in (info["partial_path"], info["final_path"]):
                remove_quiet(p)
            append_receipt(st, "CRASHED", {"action": "revert", "request_id": req, "reverted_child_sha256": info["child_sha256"],
                                           "intended_restored_sha256": info["parent_sha256"],
                                           "reason": "in-flight revert found at recovery; partial/unreceipted restored parent removed"})
        shutil.rmtree(st.work / req, ignore_errors=True)
        remove_quiet(j); fsync_dir(st.inflight)
        actions.append(f"recovered {kind} {req}")
    for w in st.work.iterdir():  # work copies without a journal: failed before the journal; nothing reserved
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


def fail_closed(st, kind, reason, base, reserved, journal, files, applied_recorded):
    """Common failure path. Never removes a child whose APPLIED receipt exists. If the failure receipt cannot be written
    (e.g. state disk full), leave the journal for recovery and report FAILED_CLOSED."""
    if not applied_recorded:
        for q in files:
            if q is not None:
                remove_quiet(q)
    try:
        if reserved and not applied_recorded:
            set_nonce(st, base["approval_nonce"], f"BURNED_{kind}")
        r = append_receipt(st, kind, {**base, "reason": reason, "nonce_burned": reserved and not applied_recorded})
    except Exception as e2:
        if journal is not None and not reserved:
            remove_quiet(journal)
        shutil.rmtree(st.work / base["request_id"], ignore_errors=True)
        return {"status": "FAILED_CLOSED", "reason": reason, "secondary_error": f"{e2.__class__.__name__}: {e2}",
                "journal_left_for_recovery": bool(journal is not None and reserved), **base}
    if journal is not None:
        remove_quiet(journal)
    shutil.rmtree(st.work / base["request_id"], ignore_errors=True)
    return {"status": kind, "reason": reason, "receipt_seq": r["seq"], "nonce_burned": reserved and not applied_recorded, **base}


# ---------------------------------------------------------------- apply
def apply(state_root, staging) -> dict:
    st = State(state_root); staging = Path(staging)
    with Lock(st):
        recover(st)
        require_chain(st)
        req = uuid.uuid4().hex
        base = {"action": "apply", "request_id": req}
        reserved = applied_recorded = False; journal = partial = final = revpath = None
        try:
            # ---- phase 1: ingest + checks (denials here do not burn the nonce)
            data, hashes = ingest(staging, st.work / req)
            base.update({"parent_sha256": hashes["parent.safetensors"], "manifest_sha256": hashes["manifest.json"],
                         "payload_sha256": hashes["edit.npz"], "signed_approval_file_sha256": hashes["approval.signed.json"]})
            hook(st, "after_ingest")
            grant_b = st.read_cfg("grant.json"); grant = loads(grant_b); policy = loads(st.read_cfg("trust_policy.json"))
            gsha = sha(grant_b); S = grant["authorization"]; epoch = S.get("authorization_epoch")
            base.update({"grant_sha256": gsha, "authorization_epoch": epoch, "service_state_id": state_id(st)})
            load_revocations(st, policy)  # refuse to operate on an unsigned/invalid/rolled-back list
            if grant.get("manifest_timing") != "PROSPECTIVE":
                raise Denied("grant is not PROSPECTIVE")
            if S.get("service_state_id") != state_id(st):
                raise Denied("grant is bound to another service state (or names none)")
            if not isinstance(epoch, str) or not epoch:
                raise Denied("grant lacks an authorization epoch")
            if epoch_closed(st, epoch):
                raise Denied(f"authorization epoch {epoch} is closed")
            gp = S.get("parent_checkpoint_sha256")
            if not isinstance(gp, str) or not HEX64.fullmatch(gp):
                raise Denied("grant lacks a full parent_checkpoint_sha256 pin")
            if hashes["parent.safetensors"] != gp:
                raise Denied("parent checkpoint does not match grant (full-file pin)")
            env = loads(data["approval.signed.json"])
            if not isinstance(env, dict):
                raise Denied("signed approval is not an object")
            approver = verify_envelope(env, policy)
            a = env["approval"]
            asha = sha(DOMAIN + canonical(a))
            base.update({"approval_sha256": asha, "approval_nonce": a.get("approval_nonce"), "approver": approver,
                         "proposer": a.get("proposer_principal"), "not_after_utc": a.get("not_after_utc")})
            if a.get("schema") != APPROVAL_SCHEMA or a.get("phase") != "POST_SUBMISSION_PATCH_APPROVAL":
                raise Denied("wrong approval schema/phase")
            for k, v in (("case_id", grant.get("case_id")), ("run_id", S.get("run_id")), ("authorization_epoch", epoch),
                         ("scope_nonce", S.get("nonce")), ("scope_grant_sha256", gsha)):
                if a.get(k) != v:
                    raise Denied(f"approval {k} does not match grant")
            if not isinstance(a.get("approval_nonce"), str) or not a["approval_nonce"] or a["approval_nonce"] == S.get("nonce"):
                raise Denied("approval nonce missing or equal to scope nonce")
            req_hex(a.get("parent_checkpoint_sha256"), "parent_checkpoint_sha256")
            if a["parent_checkpoint_sha256"] != hashes["parent.safetensors"]:
                raise Denied("parent checkpoint does not match approval (tampered or wrong parent)")
            p = a.get("patch") or {}
            if p.get("manifest_sha256") != hashes["manifest.json"] or p.get("payload_sha256") != hashes["edit.npz"]:
                raise Denied("patch does not match approval (tampered)")
            check_live(st, policy, a, asha, epoch)  # revoked / expired / epoch closed
            if nonce_status(st, a["approval_nonce"]) is not None:
                raise Denied(f"approval nonce already {nonce_status(st, a['approval_nonce'])} (replay)")
            child, plan, inverse = build_child(data["parent.safetensors"], loads(data["manifest.json"]), data["edit.npz"], grant)
            child_sha = sha(child)
            if (a.get("claimed_child_tensor_sha256") or {}) != {n: v["child_sha256"] for n, v in plan.items()}:
                raise Denied("claimed child tensor hashes do not match computed child")
            if a.get("claimed_child_checkpoint_sha256") != child_sha:
                raise Denied("claimed child checkpoint hash does not match computed child")
            base.update({"child_sha256": child_sha, "child_tensor_sha256": {n: v["child_sha256"] for n, v in plan.items()},
                         "changed_entries": {n: v["changed_entries"] for n, v in plan.items()}})
            # ---- phase 2: journal FIRST, then reserve
            partial = st.children / f".partial-{req}"; final = st.children / f"{child_sha}.safetensors"
            revpath = st.reverts / f"{child_sha}.npz"
            if final.exists():
                raise Denied("a child with this hash already exists")
            info = {"kind": "apply", "request_id": req, "approval_nonce": a["approval_nonce"], "scope_nonce": a["scope_nonce"],
                    "approval_sha256": asha, "partial_path": str(partial), "final_path": str(final), "revert_payload_path": str(revpath),
                    "child_sha256": child_sha, "parent_sha256": base["parent_sha256"], "manifest_sha256": base["manifest_sha256"],
                    "payload_sha256": base["payload_sha256"], "epoch": epoch, "grant_sha256": gsha}
            journal = st.inflight / f"{req}.json"; atomic_write(journal, canonical(info))
            hook(st, "after_journal")
            reserve_nonce(st, a["approval_nonce"], a["scope_nonce"], asha, req); reserved = True
            write_exclusive(partial, child, st, "mid_write")
            verify_written(partial, data["parent.safetensors"], plan, child_sha)
            hook(st, "before_commit")
            check_live(st, policy, a, asha, epoch)  # re-check right before marking valid
            b = io.BytesIO(); np.savez(b, parent_sha256=np.frombuffer(bytes.fromhex(base["parent_sha256"]), np.uint8),
                                       names=np.array(list(plan), dtype=object).astype("U"), **inverse)
            revb = b.getvalue(); atomic_write(revpath, revb)
            os.rename(partial, final); fsync_dir(st.children)
            hook(st, "after_rename")
            r = append_receipt(st, "APPLIED", {**base, "child_path": str(final), "revert_payload_sha256": sha(revb)})
            applied_recorded = True
            finalize_applied(st, a["approval_nonce"], epoch, gsha, req)
            remove_quiet(journal); fsync_dir(st.inflight)
            shutil.rmtree(st.work / req, ignore_errors=True)
            return {"status": "APPLIED", "receipt_seq": r["seq"], "child_path": str(final), **base}
        except ChainBroken:
            raise
        except Exception as e:
            kind = "DENIED" if isinstance(e, Denied) else "ERROR"
            reason = str(e) if isinstance(e, Denied) else f"{e.__class__.__name__}: {e}"
            base.setdefault("approval_nonce", None)
            if applied_recorded:  # bookkeeping failed after APPLIED was recorded: keep the child, leave journal for recovery
                return {"status": "APPLIED", "warning": f"post-receipt bookkeeping failed ({reason}); recovery will finish it",
                        "child_path": str(final), **base}
            return fail_closed(st, kind, reason, base, reserved, journal, (partial, final, revpath), applied_recorded)


# ---------------------------------------------------------------- revert (A5.2 item 13)
def revert(state_root, child_path) -> dict:
    st = State(state_root); cp = Path(child_path)
    with Lock(st):
        recover(st)
        recs = require_chain(st)
        req = uuid.uuid4().hex; base = {"action": "revert", "request_id": req, "child_path": str(cp)}
        journal = partial = final = None
        try:
            if not cp.is_file() or cp.is_symlink():
                raise Denied("child file missing or not a regular file")
            child = cp.read_bytes(); h = sha(child); base["reverted_child_sha256"] = h
            ap = [r for r in recs if r.get("event") == "APPLIED" and r.get("child_sha256") == h and Path(r.get("child_path", "")).resolve() == cp.resolve()]
            if len(ap) != 1:
                raise Denied("no APPLIED receipt for this exact child file")
            ap = ap[0]; base.update({"applied_receipt_seq": ap["seq"], "parent_sha256": ap["parent_sha256"]})
            if any(r.get("event") == "REVERTED" and r.get("reverted_child_sha256") == h for r in recs):
                raise Denied("child already reverted")
            revp = st.reverts / f"{h}.npz"; revb = revp.read_bytes()
            if sha(revb) != ap.get("revert_payload_sha256"):
                raise Denied("revert payload does not match APPLIED receipt")
            z = np.load(io.BytesIO(revb), allow_pickle=False)
            if bytes(z["parent_sha256"]).hex() != ap["parent_sha256"]:
                raise Denied("revert payload names another parent")
            tens = parse_safetensors(child); restored = bytearray(child)
            for i, name in enumerate(z["names"].tolist()):
                t = tens[name]; arr = np.frombuffer(child[t["start"]:t["end"]], "<u2").copy()
                arr[z[f"t{i}_idx"]] = z[f"t{i}_orig"]; restored[t["start"]:t["end"]] = arr.tobytes()
            del child
            rsha = sha(restored)
            if rsha != ap["parent_sha256"]:
                raise RuntimeError("restored bytes do not hash to the receipted parent")
            partial = st.children / f".partial-revert-{req}"; final = st.children / f"restored-{rsha}-{req}.safetensors"
            journal = st.inflight / f"{req}.json"
            atomic_write(journal, canonical({"kind": "revert", "request_id": req, "partial_path": str(partial), "final_path": str(final),
                                             "child_sha256": h, "parent_sha256": rsha}))
            write_exclusive(partial, restored, st, "revert_mid_write")
            if not file_matches(partial, restored, ap["parent_sha256"]):
                raise RuntimeError("written restored parent is not byte-identical to the receipted parent")
            os.rename(partial, final); fsync_dir(st.children)
            r = append_receipt(st, "REVERTED", {**base, "restored_parent_sha256": rsha, "restored_path": str(final)})
            remove_quiet(journal); fsync_dir(st.inflight)
            return {"status": "REVERTED", "receipt_seq": r["seq"], "restored_path": str(final), "restored_parent_sha256": rsha, **base}
        except ChainBroken:
            raise
        except Exception as e:
            kind = "DENIED" if isinstance(e, Denied) else "ERROR"
            reason = str(e) if isinstance(e, Denied) else f"{e.__class__.__name__}: {e}"
            for q in (partial, final):
                if q is not None:
                    remove_quiet(q)
            try:
                r = append_receipt(st, kind, {**base, "reason": reason})
            except Exception as e2:
                return {"status": "FAILED_CLOSED", "reason": reason, "secondary_error": str(e2), **base}
            if journal is not None:
                remove_quiet(journal)
            return {"status": kind, "reason": reason, "receipt_seq": r["seq"], **base}


# ---------------------------------------------------------------- validity (A5.2 item 8)
def child_status(state_root, child_path, anchors=None):
    """Return (status, reason): VALID_ANCHORED / VALID_LOCAL / NOT_VALID."""
    st = State(state_root)
    try:
        recs = require_chain(st)
        top = check_anchors(st, recs, anchors) if anchors is not None else None
    except ChainBroken as e:
        return "NOT_VALID", f"chain/anchor: {e}"
    p = Path(child_path)
    if not p.is_file() or p.is_symlink() or p.name.startswith(".partial"):
        return "NOT_VALID", "missing, symlink or partial"
    h = sha_path(p)
    ap = [r for r in recs if r.get("event") == "APPLIED" and r.get("child_sha256") == h and Path(r.get("child_path", "")).resolve() == p.resolve()]
    if not ap:
        if any(r.get("event") == "REVERTED" and r.get("restored_parent_sha256") == h for r in recs):
            return "NOT_VALID", "restored parent (revert output), not an applied child"
        return "NOT_VALID", "no APPLIED receipt for this file"
    if any(r.get("event") == "REVERTED" and r.get("reverted_child_sha256") == h for r in recs):
        return "NOT_VALID", "child was reverted"
    if top is not None and ap[0]["seq"] <= top:
        return "VALID_ANCHORED", f"APPLIED receipt {ap[0]['seq']} <= anchored seq {top}"
    return "VALID_LOCAL", "chain verifies locally; APPLIED receipt not covered by a supplied anchor"


def is_valid_child(state_root, child_path, anchors=None) -> bool:
    return child_status(state_root, child_path, anchors)[0] != "NOT_VALID"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("apply"); a.add_argument("--state", required=True); a.add_argument("--staging", required=True)
    rv = sub.add_parser("revert"); rv.add_argument("--state", required=True); rv.add_argument("--child", required=True)
    r = sub.add_parser("recover"); r.add_argument("--state", required=True)
    v = sub.add_parser("verify-chain"); v.add_argument("--state", required=True); v.add_argument("--anchor")
    c = sub.add_parser("is-valid"); c.add_argument("--state", required=True); c.add_argument("--child", required=True); c.add_argument("--anchor")
    h = sub.add_parser("head", help="print a publishable receipt-head anchor (one JSON line)"); h.add_argument("--state", required=True)
    args = ap.parse_args()
    try:
        if args.cmd == "apply":
            out = apply(args.state, args.staging); rc = {"APPLIED": 0, "DENIED": 1}.get(out["status"], 2)
        elif args.cmd == "revert":
            out = revert(args.state, args.child); rc = {"REVERTED": 0, "DENIED": 1}.get(out["status"], 2)
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
                status, reason = child_status(args.state, args.child, anchors)
            except ChainBroken as e:
                status, reason = "NOT_VALID", f"anchor: {e}"
            out = {"status": status, "reason": reason}; rc = {"VALID_ANCHORED": 0, "VALID_LOCAL": 2}.get(status, 1)
    except ChainBroken as e:
        out = {"status": "REFUSED_CHAIN_BROKEN", "reason": str(e)}; rc = 3
    print(json.dumps(out, sort_keys=True, default=str))
    return rc


if __name__ == "__main__":
    sys.exit(main())
