# Mutation Integrity Layer — Case 001 v0.3 hardening extension

This directory contains an **unaltered copy of the 20 checksum-covered files from Case 001 v0.2**, plus an isolated signed pre-apply approval gate and synthetic security regression tests. The original `README.md` and `SPEC.md` still describe the retrospective v0.2 checker, which is not sufficient for a production apply decision.

Read **[HARDENING_v0_3.md](HARDENING_v0_3.md)** before using any new command. The implementation intentionally does **not** apply model weights and is not a prospective agent compliance benchmark.

### Run offline synthetic checks

```bash
pip install -r requirements.txt
pytest -q tests/test_approval_gate.py
python tests/test_synthetic.py
```

Key additions: Ed25519 independent-approver verification; exact SHA-256 binding of scope, parent checkpoint and submitted patch; explicit run/epoch and two nonces; mandatory full-base verification in the gate; strict sparse payload dtypes; atomic SQLite nonce reservation; a CLI that cannot accept scope/diagnostic mode for authorization; and permanent planted negatives.

The signed approval and trusted public-key policy are **not included**: they're produced by the trusted operator during a prospective run. Do not publish any live private keys.
