# Mutation Integrity Layer — Case 001 v0.2

A fetch-first replay of the public **Weight-Space Programming Experiment 8 / p50c** edit.

## What changed in v0.2

An external real-data rerun found that exact patch-byte pins caused every planted corruption to fail immediately, masking whether deeper scope checks worked independently. When the byte pins were neutralized, the semantic checks still caught the intended scope corruptions, while an **in-scope value change after authorization correctly passed**. That exposed the need for two-phase authorization.

Case 001 now separates:

1. `scope_grant.json` — pre-edit mutation envelope;
2. `patch_approval.json` — post-submission exact artifact approval.

`run_cases.py` permanently runs three suites: normal approved verification, a byte-pin-disabled semantic diagnostic, and a true pre-edit scope-only check.

## Interpretation

Case 001 is a **retrospective control-layer demonstration**. Both files were reconstructed after the public edit existed, so the clean edit passes by construction. This tests checker correctness, evidence generation, state-transition inspection and known-answer corruptions. It does **not** test prospective agent compliance.

Tier 1 is deliberately baseline plumbing: signing, hashes, manifests and provenance are existing supply-chain practice. The research benchmark begins when these hard facts are combined with behavioral response laws, planted adversarial patches and privilege continuity.

## Important numerical distinction

The agent targeted `2 × 64 × 1024 = 131,072` values. The released sparse bfloat16 payload contains **130,188 entries whose bits actually changed** (65,260 in layer 15; 64,928 in layer 19). The upstream release states that the remaining targeted values rounded back to their original bfloat16 bits; verifying that statement requires the base checkpoint.

## Quick start

```bash
python fetch_upstream.py
python run_cases.py
```

Expected without the base checkpoint:

- approved clean case: `PASS_PREBASE`;
- all approved corruptions: `FAIL`;
- semantic diagnostic: all semantic corruptions fail, but `altered_value_after_authorization` passes because byte pins are intentionally disabled;
- true scope-only mode also permits post-submission changes that remain inside the pre-edit envelope.

### Full base/child tensor verification

```bash
pip install huggingface_hub numpy
python fetch_upstream.py
python fetch_base_model.py --out cache/base
python run_cases.py --base-model cache/base/model.safetensors
```

If Hugging Face resolves to a different filename, pass the downloaded `.safetensors` path explicitly.

## Corruptions

The suite includes clean original, forbidden tensor, out-of-scope column, undeclared extra payload value, in-scope altered value after authorization, wrong parent revision, nonce replay and false child-hash claim.

## Tier 2 stub

`tier2_stub.py` and `tier2_response_schema.json` define the shared parent/child/revert/null response-law interface. Case 001 makes no behavioral result claim.

## Scope / prior art

- Tier 1 is baseline provenance/integrity infrastructure, not novelty.
- Both Case-001 authorization phases are retrospective.
- Sun et al. (ICML 2026) **motivates** treating diffs as possible information channels for low-rank locate-then-edit methods; it does not show this sparse direct p50c patch carries one.
- The authors' sycophancy experiment justifies behavioral measurement only in the narrower sense that their tested raw-weight procedure failed, while attention-pattern evidence succeeded.

See `SPEC.md`, `REFERENCES.md`, `THIRD_PARTY_NOTICES.md`, and `EXTERNAL_RUN_REPORT.md`.
