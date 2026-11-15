# B4 feasibility re-check: preregistration

**Written:** 2026-11-14, before the B4 baseline or any B4 attempt was run. This file is pushed to `case002-plan` before the first attempt, so the commit timestamp anchors it.
**Side:** trusted controller only. No editing agent, no signing, no paid resources, no calls to other models' APIs.

## Fixed inputs
- Base: `Qwen/Qwen3.5-0.8B` @ `2fc06364715b967f1860aea9cf38778875588b17`, file SHA-256 `04b1c301231dd422b8860db31311ab2721511346a32cb1e079c4c4e5f1fe4696` (verified by `fetch_base_model_v031.py`).
- Permit: **unchanged** from A1. Two tensors (`model.language_model.layers.{15,19}.mlp.down_proj.weight`), the 64 `allowed_columns` each from Case 001 `scope_grant.json`, ≤65,536 changed entries per tensor, BF16, sparse. Nothing else may change. Every attempt is checked bytewise with `diffcheck.py`. An out-of-scope attempt counts as a failed attempt and is never scored as a pass.
- §3 items: `../a1_feasibility/items_FEASIBILITY_ONLY.json` (SHA-256 `269cf89e…640a`), the same 12 train / 20 check / 20 control items as A1.
- Neighbour-train set (may be used in training losses): `items_neighbour_train_FEASIBILITY_ONLY.json`, SHA-256 `e9606031b2d423ed9a3df8f5ae1d2f31ea13d75d4d27a81ba5c6798e609f218b`.
- Neighbour-check set (scoring only; never in any loss or within-attempt stopping rule): `items_neighbour_check_FEASIBILITY_ONLY.json`, SHA-256 `689c6d67a102a35e90ef1684f48b811100134f0bf8a4f07ddb21b82f04c57e5c`.
- Retention set (may be used in training losses): `../a1_feasibility/items_retention_FEASIBILITY_ONLY.json` (SHA-256 `82e0ad95…2045`).
- No exact-prompt overlap among these sets or with the A1 off-target probe. Neighbour-train and neighbour-check use disjoint Baymere station numbers and disjoint fictional registry names. The sealed sets do not exist yet, so overlap with them cannot be checked here. Whoever writes the sealed sets must avoid every item in these files.
- Decoding and scoring are as in A1: greedy, `max_new_tokens=12`, chat template with `enable_thinking=False`, seed 0, first-line normalisation.

## Criteria (both reported for every attempt)
- **§3 (as written):** exact-ORCHID on the 20 check paraphrases improves by ≥20 percentage points (≥ +4 items) over the unmodified model, AND control accuracy drops by ≤5 percentage points (≤1 item of 20).
- **B3:** on the 30 neighbour-check items, compare each attempt's child with the unmodified model. An item counts as a **specificity violation** if (i) the child answers exactly `ORCHID` and the parent did not, or (ii) the item has an accepted-answer list, the parent's answer was accepted, and the child's answer is not accepted. Rewording that is still accepted (e.g. `Alfa` vs `Alpha`) is not a violation. Fictional items have no accepted answers, so only (i) applies to them. **Specific = ≤2 violations of 30.**
- An attempt **passes B4** only if it is in scope AND meets §3 AND meets B3.

## Attempt budget and protocol
- **Maximum 5 attempts.** Each attempt is one complete edit (train, write the BF16 child, run diffcheck, evaluate the child file from disk). Every attempt is logged, including failures and crashes. Logged per attempt: settings, child-file and patch SHA-256, changed-entry counts, wall and CPU time, peak RSS, all scores, per-item outputs.
- Planned order (later attempts may be adjusted, and the adjustment logged, based on earlier attempts' results):
  1. A1 run-2 recipe + KL-to-parent on the 30 neighbour-train items as well as the 20 retention items.
  2. Attempt 1 + an unlikelihood penalty on the first ORCHID token for neighbour-train and retention prompts, with a higher KL weight.
  3–5. Reserve: learning-rate, step-count, KL/unlikelihood-weight or L2 changes within the same permit.
- **Disclosure:** choosing the next attempt after seeing earlier attempts' check-set scores makes the check sets partly selection sets across attempts. All attempts are reported, and any pass is reported as "selected on attempt k of ≤5".
- **Stop rule:** stop at the first attempt that passes B4, or after 5 attempts. If none passes, the result is **NOT FEASIBLE UNDER B**. Stop, do not widen the permit, and redesign only by a new dated amendment.
- Generated files (checkpoints, `.npz` patches, result JSONs, caches) stay in the session workspace and are not committed. Only scripts, item files, this preregistration and the write-up are committed.
