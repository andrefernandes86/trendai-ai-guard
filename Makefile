.PHONY: configure build deploy destroy test lint clean validate

## Full guided setup: collect params, deploy stack, build + upload Lambda code
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

## Delete the CloudFormation stack
## (Lambda deploy bucket and log bucket are retained — delete manually if needed)
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
	rm -rf .aws-sam/ __pycache__ src/__pycache__
