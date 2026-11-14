# External run report — user supplied

This file records a **user-supplied external execution report**. It was not independently rerun in the build container and is not represented as third-party peer review.

Reported results against the actual Experiment 8 patch:

- archive SHA-256 matched the v0.1 release (`ed389b41…`);
- all four pinned GitHub files matched their locked Git blob hashes;
- the synthetic test passed;
- clean and first nonce use returned `PASS_PREBASE`;
- all planted corruptions failed in the original byte-pinned suite for their intended checks;
- raw payload recomputation found 130,188 unique changed entries, 64 columns per tensor, all in scope;
- 884 of 131,072 targeted positions are absent from the sparse payload; the upstream explanation that they rounded back to unchanged bfloat16 bits remains base-checkpoint-dependent;
- when exact artifact byte pins were neutralized to isolate semantic checks, the semantic/scope checks still rejected the intended scope corruptions, while `altered_value_after_authorization` passed.

Design consequence incorporated in v0.2: prospective authorization is two phase. The pre-edit scope grant authorizes an envelope; the post-submission approval pins the exact patch before application.

Hugging Face was blocked in that external workspace, so base-checkpoint reconstruction and `PASS_FULL` remained open there.
