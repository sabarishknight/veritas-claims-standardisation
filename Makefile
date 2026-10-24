# Veritas Claims standardisation pipeline
#
# `make demo` is the entry point. It needs Python 3.10+ and PyYAML; nothing else.

PY       ?= python3
PYTHONPATH_ := PYTHONPATH=src
DB       ?= data/veritas.db
PORT     ?= 8080
FILES    ?= 25000
WORKERS  ?= 0

.DEFAULT_GOAL := help
.PHONY: help demo run serve test lint verify bench cost ddl onboard export edge clean profile check

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	 | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[1m%-12s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "  Start here:  make demo  then  make serve"

demo:  ## Run the pipeline over all sample data and summarise the findings
	@$(PYTHONPATH_) $(PY) -m veritas --db $(DB) demo

serve:  ## Start the operations UI at http://127.0.0.1:$(PORT)
	@$(PYTHONPATH_) $(PY) -m veritas --db $(DB) serve --port $(PORT)

run:  ## Run the pipeline over a path:  make run IN=path/to/files
	@$(PYTHONPATH_) $(PY) -m veritas --db $(DB) run $(IN) --workers $(WORKERS)

test:  ## Run the test suite (stdlib unittest, no install needed)
	@$(PYTHONPATH_) $(PY) -m unittest discover -s tests -t . -v 2>&1 | tail -25

verify:  ## Prove idempotency by processing the same input twice (NFR-3.2)
	@$(PYTHONPATH_) $(PY) -m veritas --db data/verify.db verify

bench:  ## Throughput benchmark and capacity projection (NFR-1.1)
	@$(PYTHONPATH_) $(PY) -m veritas bench --files $(FILES) --workers $(WORKERS)

cost:  ## GenAI cost model: per row vs the unmapped tail
	@$(PYTHONPATH_) $(PY) -m veritas cost

profile:  ## Reproduce the data-profile findings straight from the sample files
	@$(PY) scripts/profile_samples.py

onboard:  ## Field-coverage report for a clinic file:  make onboard F=file.json
	@$(PYTHONPATH_) $(PY) -m veritas onboard $(F)

ddl:  ## Print the BigQuery DDL generated from the schema definitions
	@$(PYTHONPATH_) $(PY) -m veritas ddl --dialect bigquery --views

export:  ## Export the target 78-column view to out/ideal_output.csv
	@$(PYTHONPATH_) $(PY) -m veritas --db $(DB) export \
	   --view v_ideal_output_schema --out out/ideal_output.csv

edge:  ## Regenerate the edge-case sample corpus
	@$(PY) scripts/make_edge_cases.py

lint:  ## Lint (requires ruff)
	@ruff check src tests scripts || echo "ruff not installed: pip install ruff"

check: test verify  ## Everything CI runs

clean:  ## Remove generated artefacts
	@rm -rf data out .pytest_cache **/__pycache__ bench/smoke.json
	@find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@echo "cleaned"
