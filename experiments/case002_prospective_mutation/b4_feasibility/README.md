# B4 feasibility re-check scripts (FEASIBILITY_ONLY)

Trusted-side scripts behind `../B4_FEASIBILITY.md`. The preregistration is in `B4_PREREG.md`. No weights, patches or result files are included.

- `b4lib.py`: shared loading, rendering, greedy generation and scoring. Does not load the neighbour-check set.
- `b4_check_items.py`: loads the neighbour-check set. Imported only by `eval_b4.py`. Paths come from env vars `BL_BASE_DIR` (config/tokenizer dir with the verified base checkpoint symlinked in) and `BL_GRANT` (Case 001 `scope_grant.json`).
- `edit_b4.py <out_dir>`: masked in-permit edit. Env: `STEPS LR KLW ULW L2 NB CHUNK`. Does not import the neighbour-check set. (Correction: in attempts 2–4 it was loaded into memory through the shared `b4lib` import, but used in no loss or stopping rule. Fixed on `case002-a5-runner`.)
- `diffcheck_b4.py <child_dir>`: bytewise child-vs-parent scope check.
- `eval_b4.py <model_dir> <out.json>`: A1 check/controls + neighbour-check (+ neighbour-train as a diagnostic).
- `run_attempt.py <n> <label>`: edit → diffcheck → eval → §3/B3/B4 scoring, appended to `$BL_OUT/attempts.jsonl` (default `/home/claude/work/b4`). Refuses attempt numbers above 5.

As run:
```
python eval_b4.py $BL_BASE_DIR $BL_OUT/baseline.json
STEPS=40 LR=2e-3 KLW=1.0 ULW=0 NB=1           python run_attempt.py 1 ...   # crashed (OOM), pre-chunking edit_b4.py
STEPS=40 LR=2e-3 KLW=1.0 ULW=0 NB=1           python run_attempt.py 2 ...
STEPS=80 LR=2e-3 KLW=5.0 ULW=5.0 NB=1         python run_attempt.py 3 ...
STEPS=80 LR=2e-3 KLW=5.0 ULW=5.0 L2=0.005 NB=1 python run_attempt.py 4 ...   # B4 pass
```
