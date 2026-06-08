.PHONY: install uninstall configure build deploy destroy test lint clean validate

## One-shot installer (bash + AWS CLI). Run this if you don't have boto3.
install:
	./install.sh

## Full uninstaller - removes the stack, the deploy bucket, optionally
## the log bucket / SES identity / local files. Interactive.
uninstall:
	./uninstall.sh

## Full guided setup using Python (same end result as 'install')
configure:
	python3 configure.py

## Upload a new Lambda package to the stack's deployment bucket
## Usage: make build           (reads stack name from default: ai-guard-monitor)
##        make build STACK=my-stack REGION=us-east-1
build:
	./build.sh $(or $(STACK),ai-guard-monitor) $(or $(REGION),us-east-1)

## Re-deploy a specific stack instance using its generated cfn-deploy-<name>.sh.
## Self-sufficient: recreates the deploy bucket if missing, rebuilds and
## uploads the Lambda package, removes orphaned log groups, then deploys.
## Safe to run after 'make destroy' or 'make uninstall' without re-running install.sh.
## Usage: make deploy STACK=ai-guard-monitor-x4k9m2
deploy:
	./cfn-deploy-$(or $(STACK),$(shell ls cfn-deploy-*.sh 2>/dev/null | head -1 | sed 's/cfn-deploy-//;s/\.sh//')).sh

## Delete the CloudFormation stack and remove any orphaned log groups so
## 'make deploy' can be re-run immediately afterwards without errors.
## The S3 buckets (deploy bucket and log bucket) are NOT deleted here;
## run 'make uninstall' for a full teardown.
destroy:
	aws cloudformation delete-stack \
	  --stack-name $(or $(STACK),ai-guard-monitor) \
	  --region $(or $(REGION),us-east-1)
	@echo "Waiting for stack deletion..."
	aws cloudformation wait stack-delete-complete \
	  --stack-name $(or $(STACK),ai-guard-monitor) \
	  --region $(or $(REGION),us-east-1)
	@echo "Cleaning up orphaned log groups..."
	@aws logs describe-log-groups \
	  --region $(or $(REGION),us-east-1) \
	  --log-group-name-prefix "/aws/lambda/$(or $(STACK),ai-guard-monitor)-" \
	  --query 'logGroups[].logGroupName' --output text 2>/dev/null \
	  | tr '\t' '\n' \
	  | grep . \
	  | xargs -I{} aws logs delete-log-group --log-group-name {} --region $(or $(REGION),us-east-1) \
	  || true
	@echo "Done."

## Validate the CloudFormation template
validate:
	cfn-lint template.yaml --include-checks W

## Run the test suite (no live AWS or TM credentials needed)
test:
	pip install -q -r requirements-dev.txt -r src/requirements.txt
	PYTHONPATH=src pytest tests/ -v --cov=src --cov-report=term-missing

## Lint Python source
lint:
	ruff check src/ tests/ configure.py

## Remove build artefacts
clean:
	rm -rf __pycache__ src/__pycache__ .pytest_cache
