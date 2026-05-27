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

## Re-deploy using the generated cfn-deploy.sh
deploy:
	./cfn-deploy.sh

## Delete the CloudFormation stack. This also removes the S3 event
## notification from the source bucket (via the helper custom resource).
## The two S3 buckets created by the installer (deploy bucket and log
## bucket) are NOT in the stack and must be deleted manually if desired.
## See README -> "Uninstalling the solution" for the full teardown.
destroy:
	aws cloudformation delete-stack \
	  --stack-name $(or $(STACK),ai-guard-monitor) \
	  --region $(or $(REGION),us-east-1)

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
