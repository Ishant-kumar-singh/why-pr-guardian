.PHONY: help up check ingest extract serve eval clean

help:
	@echo "Why? - PR Guardian Makefile"
	@echo ""
	@echo "Setup:"
	@echo "  make up         - start FalkorDB (docker compose up -d)"
	@echo "  make check      - verify LLM endpoint works"
	@echo ""
	@echo "Build the graph:"
	@echo "  make ingest     - pull PRs from GitHub into FalkorDB"
	@echo "  make extract    - extract decisions from PR discussions"
	@echo ""
	@echo "Run:"
	@echo "  make serve      - start the web server (uvicorn)"
	@echo ""
	@echo "Test:"
	@echo "  make eval       - run evaluation questions"
	@echo ""
	@echo "Cleanup:"
	@echo "  make clean      - stop FalkorDB and remove cache"

up:
	docker compose up -d
	@echo "Waiting for FalkorDB to be ready..."
	@timeout 30 bash -c 'until docker exec falkordb redis-cli ping 2>/dev/null | grep -q PONG; do sleep 1; done' || true

check:
	python check_llm.py

ingest:
	python ingest.py

extract:
	python extract.py

serve:
	uvicorn server:app --host 0.0.0.0 --port 8000 --reload

eval:
	python tests/run_eval.py

clean:
	docker compose down
	rm -rf .cache
