.PHONY: configure build deploy destroy test lint clean validate

## Interactive setup: pick S3 bucket, build Lambda, generate cfn-deploy.sh
configure:
	python3 configure.py

## Package Lambda code and upload to S3 (requires BUCKET variable)
## Usage: make build BUCKET=my-deploy-bucket REGION=us-east-1
build:
	./build.sh $(BUCKET) $(REGION)

## Deploy the stack using generated cfn-deploy.sh
deploy:
	./cfn-deploy.sh

## Tear down the stack (log bucket is retained by DeletionPolicy: Retain)
destroy:
	aws cloudformation delete-stack --stack-name ai-guard-monitor --region $(REGION)

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
