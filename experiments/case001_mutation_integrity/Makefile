PYTHON ?= python

fetch:
	$(PYTHON) fetch_upstream.py

test:
	$(PYTHON) tests/test_synthetic.py

case001: fetch
	$(PYTHON) run_cases.py
