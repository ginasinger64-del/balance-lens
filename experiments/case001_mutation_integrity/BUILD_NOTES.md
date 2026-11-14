# Build notes — v0.2

- v0.1 was preserved as a separate archive; this directory is an append-only v0.2 revision.
- Added explicit two-phase authorization: `scope_grant.json` and `patch_approval.json`.
- Added `scope`, `approved`, and `semantic-diagnostic` verifier modes.
- `semantic-diagnostic` permanently disables artifact byte pins so deeper checks are exercised independently.
- True `scope` mode intentionally cannot detect an in-scope post-submission byte change or a false child-state claim without Phase 2/base evidence.
- Added a user-supplied external run report documenting the real-data v0.1 replay that motivated the design change.
- Full Hugging Face parent/child reconstruction remains environment-dependent.
