#!/usr/bin/env python3
"""Offline signing helper. Run only within an independent approver boundary.

NEVER give the editing agent this helper's private key, approval signing service
credentials, or the ability to alter the trusted public-key policy.
"""
import argparse
import base64
import json
from pathlib import Path
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from approval_gate import canonical, DOMAIN, load_json


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--approval", required=True)
    p.add_argument("--key-id", required=True)
    p.add_argument("--private-key", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args()
    approval = load_json(args.approval)
    key = load_pem_private_key(Path(args.private_key).read_bytes(), password=None)
    if key.__class__.__name__ != "Ed25519PrivateKey":
        raise SystemExit("only Ed25519 private keys are accepted")
    sig = key.sign(DOMAIN + canonical(approval))
    envelope = {"approval": approval, "key_id": args.key_id, "algorithm": "Ed25519", "signature_b64": base64.b64encode(sig).decode("ascii")}
    Path(args.out).write_bytes(json.dumps(envelope, indent=2, sort_keys=True).encode() + b"\n")
    print("signed approval written; signature alone does not establish independent human review")


if __name__ == "__main__":
    main()
