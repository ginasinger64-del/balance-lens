#!/usr/bin/env python3
"""v0.3.1: base-aware expected results for the v0.2 case suite.

run_cases.py (kept byte-identical) expects semantic-diagnostic
altered_value_after_authorization to PASS. That holds only without the base
checkpoint. With --base-model, the verifier rebuilds the edited tensors and the
edited-tensor hash check catches the altered value, so FAIL is correct.
Observed on the real p50c patch with the pinned base: clean approved = PASS_FULL,
semantic-diagnostic altered value = FAIL. This wrapper reuses run_cases.py and
changes only that one expectation when a base model is supplied.
"""
import sys
import run_cases

_orig_expected = run_cases.expected
_BASE = '--base-model' in sys.argv


def expected(mode):
    exp = dict(_orig_expected(mode))
    if _BASE and mode == 'semantic-diagnostic':
        exp['altered_value_after_authorization'] = ['FAIL']
    return exp


run_cases.expected = expected

if __name__ == '__main__':
    run_cases.main()
