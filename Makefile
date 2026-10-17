SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

export HOST_REPO ?= $(CURDIR)
.PHONY: up test drill record evals clean

up:
	docker compose up -d --wait postgres
	docker compose run --rm migrate

test: up
	mkdir -p .audit
	set -o pipefail; docker compose run --rm test 2>&1 | tee .audit/make-test.txt
	test -s .audit/make-test.txt
	set -o pipefail; bash tests/run_sandbox.sh 2>&1 | tee .audit/make-sandbox.txt
	test -s .audit/make-sandbox.txt

drill: up
	mkdir -p .audit recordings/steps
	docker build -t swarm-agent -f Dockerfile.agent .
	PROXY_MODE=replay docker compose up -d --wait --force-recreate proxy-a proxy-b
	docker compose --profile tools run --rm drill
	test -s docs/drill-output.txt

record: up
	mkdir -p recordings/steps
	@key="$$(security find-generic-password -s MERGE_GATEWAY_API_KEY -a merge-gateway -w)"; \
	export PROXY_MODE=record PROVIDER_KEY="$$key"; \
	docker compose up -d --wait --force-recreate proxy-a proxy-b; \
	set -o pipefail; docker compose --profile tools run --rm record 2>&1 | tee .audit/make-record.txt

capture: up
	mkdir -p recordings/evals
	docker compose --profile tools run --rm --entrypoint bash record -c "pip install -q --require-hashes -r requirements/services.lock && python -m evals.capture"

evals: up
	mkdir -p .audit
	set -o pipefail; docker compose run --rm --entrypoint bash test -c "pip install -q --require-hashes -r requirements/services.lock && python -m evals --agent daily-brief --candidate agents/t-a/daily_brief.yaml" 2>&1 | tee .audit/make-evals.txt
	test -s .audit/make-evals.txt

clean:
	docker compose down -v
