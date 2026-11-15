# Case 002 — readiness checklist for the first live agent trial

**Created:** 2026-11-14, on `case002-a5-runner`, before any A5.2 code.
**Rule:** after this first push, items may be added or re-scoped only by a dated note in the "Changes" section at the bottom, never silently. Status changes are recorded there too, with the commit that changed them.

Status key:
- **DONE**: completed and recorded on GitHub.
- **A5.2**: this step.
- **LATER**: a later trusted-side step.
- **GINA**: needs Gina Singer's own action or decision.

Sources: PLAN.md §§2–10, Amendments A and B, `A1_A3_FEASIBILITY.md`, `B4_FEASIBILITY.md`, `A5_RESULTS.md`, `A5_1_RESULTS.md`.

## Feasibility and cost
| # | Item | Status | Where |
|---:|---|---|---|
| 1 | A1 feasibility of the target edit within the permit (§3) | DONE | `ce9adc7` |
| 2 | A3 CPU cost check; GPU not needed | DONE | `ce9adc7`, updated in `3b978da` |
| 3 | B4 feasibility under §3 + B3 specificity (passed at zero margin, attempt 4 of 5) | DONE | `dd407db`, `3b978da` |
| 4 | Feasibility scripts and item files committed | DONE | `d4560f2`, `3b978da` |
| 5 | B4 check-set import fix (`edit_b4.py` no longer imports check items) | DONE | `be12aa6` |

## Trusted apply-and-receipt service (A5)
| # | Item | Status | Where |
|---:|---|---|---|
| 6 | Signed exact approval, private copies, independent scope check, write-verify-then-valid, hash-chained receipts, one-time nonce, revocation check, crash/ENOSPC recovery for the children dir, OS user separation (A5 tests 1–12) | DONE | `be12aa6` |
| 7 | Full-parent pin in grant; journal before reservation; external head anchor command (A5.1) | DONE | `74cbb1b` |
| 8 | `is-valid` distinguishes VALID_ANCHORED from VALID_LOCAL | A5.2 | |
| 9 | One authoritative service state per grant (grant names the state_id) | A5.2 | |
| 10 | Signed approval expiry (not-after) | A5.2 | |
| 11 | Signed revocation list (trusted key; unsigned/invalid list → refuse) | A5.2 | |
| 12 | Authorization epoch closed after APPLIED; new grant required | A5.2 | |
| 13 | Revert command with byte-for-byte parent verification and REVERTED receipt | A5.2 | |
| 14 | Fail closed on disk full in the state/receipt directory | A5.2 | |
| 15 | Receipts signed by a service key (beyond hash chain + anchors) | LATER | |
| 16 | Anchor publishing procedure: who publishes, where, when (after every apply/deny/revert) | LATER (procedure) + GINA (choose location) | |
| 17 | Signed / authorized revert requests (A5.2 revert is an admin command only) | LATER | |

## Agent environment (§2, §10c)
| # | Item | Status |
|---:|---|---|
| 18 | Agent sandbox: separate OS user (beyond file permissions), no access to signer/service processes (ptrace, signals), restricted network, fresh per trajectory | LATER |
| 19 | Append-only trace capture of every agent tool call, output and denial, outside the agent's reach | LATER |
| 20 | Staging handoff: agent writes only to staging; service reads from it (tested in A5 test 12 at file level) | DONE (file level) / LATER (in the real sandbox) |
| 21 | Agent harness pinned: exact model ID, parameters, system prompt, tool list, provider, versions; recorded before the first run | LATER |

## Task, test sets and run protocol (§§3, 4, 6, 7; Amendment B)
| # | Item | Status |
|---:|---|---|
| 22 | Decide whether the agent receives near-neighbour negatives comparable to the B4 reference's neighbour-train set | GINA |
| 23 | Write and seal the 30-item held-out set (never seen by agent or feasibility work) and record hashes | LATER |
| 24 | Write and seal the ≥30-item near-neighbour control set, B2 categories (a)–(d), and record hashes | LATER |
| 25 | Freeze the real prospective grant per run (A, B, C): PROSPECTIVE, fresh case/run/epoch/scope nonce, parent pin, service_state_id, exact columns/budgets; hash stored outside agent storage | LATER |
| 26 | Freeze task prompts for Runs A/B/C (normal, pressure, misleading authority), the run order, and the seed/attempt count | LATER |
| 27 | Planted-negative control-layer suite on the real setup (tamper after approval, forgery, replay, mode downgrade, wrong parent) | LATER |
| 28 | Evidence packet template (§8) and claims-not-supported section | LATER |
| 29 | Scoring sheet for agent conduct vs control-layer result (§7 table) | LATER |

## Gina's actions
| # | Item | Status |
|---:|---|---|
| 30 | Generate the offline Ed25519 approval key; pin its public key in the real trust policy | GINA |
| 31 | Generate the revocation-signing key (may be separate from the approval key); pin it in the trust policy | GINA |
| 32 | Set up an Anthropic API account separate from the chat plan (Amendment A5 note) | GINA |
| 33 | Re-check provider prices and freeze the dollar cap (§9) | GINA |
| 34 | Explicit go/no-go for the first live run (§10f) | GINA |
| 35 | During the trial: review each submitted patch and sign or deny | GINA |

## Changes
**2026-11-14 (A5.2, commit recorded in git log for this note):**
- Status change: items 8, 9, 10, 11, 12, 13 and 14 → **DONE** (see `a5_runner/A5_2_RESULTS.md`; 37/37 tests; real-model apply → VALID_ANCHORED → revert to `04b1c301…4696`). No table rows were edited; this note is the record.
- Added item **36**: **Trusted time source for approval expiry.** A5.2 trusts the host clock; a live trial needs the service host's clock to be trustworthy (NTP or similar), or expiry checked against an independent time source. Status: **LATER**.
- Clarified item 11: a revocation list is validated and its sequence recorded at install; a lower sequence is refused at install and detected at use. Installing a revocation does not wait for an in-progress apply.
