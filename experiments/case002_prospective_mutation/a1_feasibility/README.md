# A1/A3 feasibility scripts (FEASIBILITY_ONLY)

Trusted-side scripts and item files behind `../A1_A3_FEASIBILITY.md`. They are committed byte-identical to the versions that were run, so the SHA-256 values in that file match. No weights or patches are included.

Files:
- `items_FEASIBILITY_ONLY.json`: 12 training prompts, 20 check paraphrases, 20 controls, scoring rule. Not the sealed held-out set; do not reuse as such.
- `items_retention_FEASIBILITY_ONLY.json`: 20 retention prompts, used only in run 2's KL term.
- `common.py`: loading, prompt rendering, greedy generation (`max_new_tokens=12`), scoring.
- `eval.py <model_dir> <out.json>`: 40-item evaluation plus forward-pass timing and peak RSS.
- `edit.py <out_dir>`: masked reference edit (env: `STEPS`, `LR`, `KLW`, `L2`). Writes the child checkpoint, `patch_sparse.npz` and `edit_log.json`.
- `diffcheck.py <child_dir>`: bytewise child-vs-parent check against Case 001 `scope_grant.json`.
- `probe2.py`: 6-prompt off-target probe (descriptive only).

Setup as run (paths are hardcoded; adjust if your layout differs):
1. In a checkout of `case001-v0.3-approval-hardening`, run `experiments/case001_mutation_integrity/fetch_base_model_v031.py`. `common.py` reads `scope_grant.json` from `/home/claude/balance-lens/experiments/case001_mutation_integrity/`.
2. Put the non-weight files of `Qwen/Qwen3.5-0.8B` @ `2fc06364715b967f1860aea9cf38778875588b17` (config, tokenizer, chat template) in `./hfmeta/`, and symlink the verified `model.safetensors-00001-of-00001.safetensors` into it.
3. `pip install torch transformers safetensors psutil numpy` (versions as run: torch 2.15.0, transformers 5.23.0, Python 3.13).

Commands as run:
```
python eval.py hfmeta baseline.json
STEPS=40 LR=2e-3 python edit.py child_r1           # run 1 (failed controls)
STEPS=40 LR=2e-3 KLW=1.0 python edit.py child_r2   # run 2 (passed)
python diffcheck.py child_r2 && python eval.py child_r2 after_r2.json
python probe2.py
```
