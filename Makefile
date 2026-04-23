# Lambda Local Testing Makefile
# Usage:
#   make emulator-up              — start Floci emulator
#   make emulator-up-localstack   — start LocalStack emulator (requires LOCALSTACK_AUTH_TOKEN)
#   make emulator-down            — stop emulator
#   make setup-resources          — create S3 buckets, SQS queues, DynamoDB tables
#   make test-local FUNCTION=MyFunction EVENT=test-events/sqs-event.json
#   make test-all FUNCTION=MyFunction
#   make logs                     — tail emulator logs
#   make health                   — check emulator health

EMULATOR_URL := http://localhost:4566
AWS_CMD      := aws --endpoint-url=$(EMULATOR_URL) \
                    --region us-east-1 \
                    --no-cli-pager
COMPOSE_FILE := docker-compose.test.yml

# ── Emulator lifecycle ────────────────────────────────────────────────────────

emulator-up:
	@echo "Starting Floci emulator..."
	docker compose -f $(COMPOSE_FILE) up -d
	@echo "Waiting for emulator to be healthy..."
	@until curl -sf $(EMULATOR_URL)/_floci/health > /dev/null 2>&1 || \
	       curl -sf $(EMULATOR_URL)/_localstack/health > /dev/null 2>&1; do \
		printf '.'; sleep 1; \
	done
	@echo "\nEmulator ready."
	$(MAKE) setup-resources

emulator-up-localstack:
	@echo "Starting LocalStack emulator (requires LOCALSTACK_AUTH_TOKEN)..."
	@[ -n "$(LOCALSTACK_AUTH_TOKEN)" ] || (echo "ERROR: Set LOCALSTACK_AUTH_TOKEN first" && exit 1)
	docker compose -f docker-compose.localstack.yml up -d
	@echo "Waiting for LocalStack to be healthy..."
	@until curl -sf $(EMULATOR_URL)/_localstack/health > /dev/null 2>&1; do \
		printf '.'; sleep 2; \
	done
	@echo "\nLocalStack ready."
	$(MAKE) setup-resources

emulator-down:
	docker compose -f docker-compose.test.yml down 2>/dev/null || true
	docker compose -f docker-compose.localstack.yml down 2>/dev/null || true
	@echo "Emulator stopped."

logs:
	docker compose -f $(COMPOSE_FILE) logs -f

health:
	@curl -sf $(EMULATOR_URL)/_floci/health 2>/dev/null || \
	 curl -sf $(EMULATOR_URL)/_localstack/health 2>/dev/null || \
	 echo "Emulator not running"

# ── Resource setup ────────────────────────────────────────────────────────────

setup-resources:
	@echo "Creating test AWS resources..."
	./scripts/setup-local-resources.sh

teardown-resources:
	@echo "Removing test AWS resources..."
	./scripts/teardown.sh

# ── Lambda invocation ─────────────────────────────────────────────────────────

# Invoke a single Lambda with a specific test event
# Usage: make test-local FUNCTION=MyFunction EVENT=test-events/sqs-event.json
test-local:
	@[ -n "$(FUNCTION)" ] || (echo "ERROR: Specify FUNCTION=<name>" && exit 1)
	@[ -n "$(EVENT)" ] || (echo "ERROR: Specify EVENT=<path>" && exit 1)
	@echo "Building $(FUNCTION)..."
	sam build --template-file $(FUNCTION)/template.yml 2>/dev/null || sam build
	@echo "Invoking $(FUNCTION) with $(EVENT)..."
	sam local invoke $(FUNCTION) \
		--event $(EVENT) \
		--env-vars env.json \
		--docker-network host
	@echo "\nDone. Check output above for errors."

# Invoke Lambda with ALL test events in test-events/
# Usage: make test-all FUNCTION=MyFunction
test-all:
	@[ -n "$(FUNCTION)" ] || (echo "ERROR: Specify FUNCTION=<name>" && exit 1)
	@echo "Building $(FUNCTION)..."
	sam build --template-file $(FUNCTION)/template.yml 2>/dev/null || sam build
	@for event in test-events/*.json; do \
		echo "\n--- Testing with $$event ---"; \
		sam local invoke $(FUNCTION) \
			--event $$event \
			--env-vars env.json \
			--docker-network host; \
	done

# Quick sanity check — list S3 buckets & SQS queues to verify resources exist
verify-resources:
	@echo "=== S3 Buckets ==="
	$(AWS_CMD) s3 ls
	@echo "\n=== SQS Queues ==="
	$(AWS_CMD) sqs list-queues
	@echo "\n=== DynamoDB Tables ==="
	$(AWS_CMD) dynamodb list-tables

# ── Dependency analysis ───────────────────────────────────────────────────────

# Find unused Node.js dependencies in a Lambda repo
# Usage: make depcheck PATH=/path/to/lambda-repo
depcheck:
	@[ -n "$(PATH)" ] || (echo "ERROR: Specify PATH=/path/to/lambda-repo" && exit 1)
	npx depcheck $(PATH)

# ── Generic Lambda deploy/invoke (CLI equivalents of dashboard Local Testing) ──
#
# Usage:
#   make lambda-deploy DIR=/path/to/fn HANDLER=index.sendEmail RUNTIME=nodejs18.x LANG=node
#   make lambda-invoke NAME=local-send-email EVENT=test-events/sqs-event.json
#   make lambda-list
#   make lambda-logs NAME=local-send-email
#
# LANG=node builds via `npm install && npm run compile` (prefers dist/).
# LANG=python installs requirements into .localtest_pkg/ before zipping.

AWSLOCAL := $(shell command -v awslocal 2>/dev/null || echo "aws --endpoint-url=$(EMULATOR_URL) --region us-east-1 --no-cli-pager")

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

# ── Wizard server (serves dashboard + local-test API) ─────────────────────────

wizard-server:
	@python3 -m wizard_server

.PHONY: emulator-up emulator-up-localstack emulator-down logs health \
        setup-resources teardown-resources test-local test-all \
        verify-resources depcheck \
        lambda-deploy lambda-invoke lambda-list lambda-logs lambda-clean \
        wizard-server
