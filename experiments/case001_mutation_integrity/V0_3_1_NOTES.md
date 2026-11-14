# Case 001 v0.3.1 — two corrections found by running it

Both fixes are additive. All v0.2 baseline files and v0.3 files stay byte-identical; `SHA256SUMS.json` still matches.

## 1. Base-model filename

`fetch_base_model.py` and `upstream.lock.json` (`preferred_file`) name the base weights `model.safetensors`. At the pinned revision `2fc06364715b967f1860aea9cf38778875588b17`, the file is actually `model.safetensors-00001-of-00001.safetensors` (1,746,942,600 bytes, SHA-256 `04b1c301231dd422b8860db31311ab2721511346a32cb1e079c4c4e5f1fe4696`). The old name returns 404.

Use `fetch_base_model_v031.py`. It downloads the real file, checks size and SHA-256, and links it to `cache/base/model.safetensors`.

## 2. Expected result with the base model present

`run_cases.py` expects semantic-diagnostic `altered_value_after_authorization` to PASS. That is correct only for a prebase run, where byte pins are disabled and nothing else can see the in-scope change. With the base model, the verifier rebuilds the edited tensors, and the edited-tensor hash check catches the altered value, so it correctly FAILs. A full base run therefore exited 2 (`all_expected=False`) even though every security check behaved correctly.

Use `run_cases_v031.py`. It reuses `run_cases.py` unchanged and expects FAIL for that one case when `--base-model` is supplied.

## Full real-data result (2026-11-14, external run with the pinned base)

- Approved clean case and first-use nonce: `PASS_FULL` (49/49 checks).
- All four tensor SHA-256s match the manifest (layer 15 `ef60d2c6…` → `82e2d733…`; layer 19 `638eedeb…` → `8ef3c61b…`), independently recomputed from the base file and patch.
- 884 targeted-but-absent positions (276 + 608) are bit-identical to the base in bfloat16; changed-entry counts 65,260 and 64,928 match.
- All seven corruptions FAIL in approved and semantic-diagnostic modes; in scope mode, altered-after-authorization and false-child-hash pass, as designed.

Neither error was in the security logic. Both were values written down earlier and carried forward unchecked until a real run tested them.

## Run

```bash
pip install -r requirements.txt huggingface_hub
python fetch_upstream.py
python fetch_base_model_v031.py
python run_cases_v031.py --base-model cache/base/model.safetensors
```
