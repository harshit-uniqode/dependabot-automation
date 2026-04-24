# Dependabot Vulnerability Tracker — Makefile
# The dashboard (served by `make wizard-server`) is the primary UI.
# CLI targets below are for power-users and CI.
#
# Quick start:
#   make restart                — ONE COMMAND: fresh wizard + Floci + dashboards
#   make stop-all               — stop everything
#   make wizard-server          — start the dashboard + API on :8787 (foreground)
#   make emulator-up            — start Floci AWS emulator on :4566
#   make emulator-down          — stop the emulator
#   make refresh-lambda         — regenerate the Lambda dashboard
#   make refresh-angular        — regenerate the Angular dashboard
#
# Lambda testing (dashboard-driven flow below also exposed as CLI):
#   make lambda-deploy DIR=<path> HANDLER=<file.fn> LANG=<node|python>
#   make lambda-invoke NAME=<fn-name> EVENT=<path/to/event.json>
#   make lambda-list
#   make lambda-logs   NAME=<fn-name>
#   make lambda-clean  NAME=<fn-name>

EMULATOR_URL     := http://localhost:4566
FLOCI_COMPOSE    := docker/floci-emulator.compose.yml
LS_COMPOSE       := docker/localstack-emulator.compose.yml
AWS_CLI_LOCAL    := aws --endpoint-url=$(EMULATOR_URL) --region us-east-1 --no-cli-pager
AWSLOCAL         := $(shell command -v awslocal 2>/dev/null || echo "$(AWS_CLI_LOCAL)")

# ── One-command lifecycle ────────────────────────────────────────────
#
# Full clean restart (wizard + Floci + dashboards):
#   make restart
#
# Variants:
#   make restart-fast     — skip dashboard regen (faster)
#   make restart-wizard   — wizard only, don't touch Floci
#   make stop-all         — stop wizard + emulators

restart:
	@./scripts/restart-all.sh

restart-fast:
	@./scripts/restart-all.sh --no-regen

restart-wizard:
	@./scripts/restart-all.sh --no-floci --no-regen

stop-all:
	@echo "Stopping wizard..."
	@lsof -ti tcp:8787 2>/dev/null | xargs kill -9 2>/dev/null || true
	@pkill -f "python3 -m wizard_server" 2>/dev/null || true
	@echo "Stopping emulators..."
	@docker compose -f $(FLOCI_COMPOSE) down --remove-orphans 2>/dev/null || true
	@docker compose -f $(LS_COMPOSE) down --remove-orphans 2>/dev/null || true
	@echo "All stopped."

# ── Wizard server (dashboard + API) ──────────────────────────────────

wizard-server:
	@python3 -m wizard_server

# ── Dashboard regen ─────────────────────────────────────────────────

refresh-lambda:
	@./scripts/refresh-lambda-dashboard.sh

refresh-angular:
	@./scripts/refresh-angular-dashboard.sh

# ── Emulator lifecycle ──────────────────────────────────────────────

emulator-up:
	@echo "Starting Floci emulator..."
	docker compose -f $(FLOCI_COMPOSE) up -d
	@echo "Waiting for emulator to be healthy..."
	@until curl -sf $(EMULATOR_URL)/_floci/health > /dev/null 2>&1 || \
	       curl -sf $(EMULATOR_URL)/_localstack/health > /dev/null 2>&1; do \
		printf '.'; sleep 1; \
	done
	@echo "\nEmulator ready."
	$(MAKE) setup-resources

emulator-up-localstack:
	@echo "Starting LocalStack (requires LOCALSTACK_AUTH_TOKEN)..."
	@[ -n "$(LOCALSTACK_AUTH_TOKEN)" ] || (echo "ERROR: Set LOCALSTACK_AUTH_TOKEN first" && exit 1)
	docker compose -f $(LS_COMPOSE) up -d
	@until curl -sf $(EMULATOR_URL)/_localstack/health > /dev/null 2>&1; do \
		printf '.'; sleep 2; \
	done
	@echo "\nLocalStack ready."
	$(MAKE) setup-resources

emulator-down:
	@docker compose -f $(FLOCI_COMPOSE) down 2>/dev/null || true
	@docker compose -f $(LS_COMPOSE) down 2>/dev/null || true
	@echo "Emulator stopped."

emulator-logs:
	docker compose -f $(FLOCI_COMPOSE) logs -f

emulator-health:
	@curl -sf $(EMULATOR_URL)/_floci/health 2>/dev/null || \
	 curl -sf $(EMULATOR_URL)/_localstack/health 2>/dev/null || \
	 echo "Emulator not running"

# ── Test resources (S3 buckets, SQS queues, DynamoDB tables) ────────

setup-resources:
	@./scripts/setup-emulator-aws-resources.sh

teardown-resources:
	@./scripts/teardown-emulator-aws-resources.sh

verify-resources:
	@echo "=== S3 Buckets ==="; $(AWSLOCAL) s3 ls
	@echo "\n=== SQS Queues ==="; $(AWSLOCAL) sqs list-queues
	@echo "\n=== DynamoDB Tables ==="; $(AWSLOCAL) dynamodb list-tables

# ── Lambda deploy / invoke / list / logs ────────────────────────────
#
# Deploy:
#   make lambda-deploy DIR=/path/to/fn HANDLER=index.sendEmail LANG=node
#   make lambda-deploy DIR=/path/to/fn HANDLER=handler.lambda_handler LANG=python
#
# Invoke:
#   make lambda-invoke NAME=local-send-email EVENT=lambda-test-events/sqs-event.json

lambda-deploy:
	@[ -n "$(DIR)" ]     || (echo "ERROR: Specify DIR=/path/to/function-dir" && exit 1)
	@[ -n "$(HANDLER)" ] || (echo "ERROR: Specify HANDLER=<file.function>" && exit 1)
	@[ -n "$(LANG)" ]    || (echo "ERROR: Specify LANG=node|python" && exit 1)
	@FN_NAME=local-$$(basename $(DIR) | tr '_' '-' | tr A-Z a-z); \
	RUNTIME=$${RUNTIME:-$$(if [ "$(LANG)" = "python" ]; then echo python3.11; else echo nodejs18.x; fi)}; \
	echo "Building $$FN_NAME ($(LANG))..."; \
	cd $(DIR); \
	if [ "$(LANG)" = "node" ]; then \
	  npm install --no-audit --no-fund; \
	  jq -e '.scripts.compile' package.json > /dev/null 2>&1 && npm run compile; \
	  [ -d dist ] && (cd dist && zip -qr ../function.zip .) || zip -qr function.zip . -x "node_modules/*" ".git/*"; \
	  zip -qur function.zip node_modules; \
	elif [ "$(LANG)" = "python" ]; then \
	  rm -rf .localtest_pkg && mkdir .localtest_pkg; \
	  [ -f requirements.txt ] && pip3 install -q -r requirements.txt -t .localtest_pkg; \
	  cp *.py .localtest_pkg/ 2>/dev/null || true; \
	  find . -maxdepth 1 -mindepth 1 -type d ! -name .localtest_pkg ! -name .git ! -name tests ! -name __pycache__ \
	    -exec cp -r {} .localtest_pkg/ \; 2>/dev/null || true; \
	  (cd .localtest_pkg && zip -qr ../function.zip .); \
	fi; \
	echo "Deploying $$FN_NAME (runtime=$$RUNTIME, handler=$(HANDLER))..."; \
	$(AWSLOCAL) lambda update-function-code \
	  --function-name $$FN_NAME \
	  --zip-file fileb://$(DIR)/function.zip >/dev/null 2>&1 || \
	$(AWSLOCAL) lambda create-function \
	  --function-name $$FN_NAME \
	  --runtime $$RUNTIME \
	  --handler $(HANDLER) \
	  --role arn:aws:iam::000000000000:role/lambda-role \
	  --zip-file fileb://$(DIR)/function.zip \
	  --timeout 60 --memory-size 256; \
	$(AWSLOCAL) lambda wait function-active-v2 --function-name $$FN_NAME; \
	echo "Deployed $$FN_NAME"

lambda-invoke:
	@[ -n "$(NAME)" ]  || (echo "ERROR: Specify NAME=<function-name>" && exit 1)
	@[ -n "$(EVENT)" ] || (echo "ERROR: Specify EVENT=<path/to/event.json>" && exit 1)
	$(AWSLOCAL) lambda invoke \
	  --function-name $(NAME) \
	  --payload fileb://$(EVENT) \
	  --cli-binary-format raw-in-base64-out \
	  --log-type Tail \
	  /tmp/lambda-out.json | jq -r '.LogResult // ""' | base64 -d 2>/dev/null || true
	@echo "── Output ──"
	@cat /tmp/lambda-out.json; echo

lambda-list:
	$(AWSLOCAL) lambda list-functions --query 'Functions[].FunctionName' --output table

lambda-logs:
	@[ -n "$(NAME)" ] || (echo "ERROR: Specify NAME=<function-name>" && exit 1)
	$(AWSLOCAL) logs tail /aws/lambda/$(NAME) --follow

lambda-clean:
	@[ -n "$(NAME)" ] || (echo "ERROR: Specify NAME=<function-name>" && exit 1)
	$(AWSLOCAL) lambda delete-function --function-name $(NAME)

# ── Dependency analysis ─────────────────────────────────────────────

depcheck:
	@[ -n "$(DEP_PATH)" ] || (echo "ERROR: Specify DEP_PATH=/path/to/lambda-repo" && exit 1)
	npx depcheck $(DEP_PATH)

.PHONY: restart restart-fast restart-wizard stop-all \
        wizard-server refresh-lambda refresh-angular \
        emulator-up emulator-up-localstack emulator-down emulator-logs emulator-health \
        setup-resources teardown-resources verify-resources \
        lambda-deploy lambda-invoke lambda-list lambda-logs lambda-clean \
        depcheck
