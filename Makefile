.PHONY: build deploy destroy test lint clean

## Build the SAM application
build:
	sam build --use-container

## Interactive guided deploy (first time)
deploy-guided: build
	sam deploy --guided

## Deploy using samconfig.toml
deploy: build
	sam deploy

## Tear down the CloudFormation stack
destroy:
	sam delete

## Run the test suite
test:
	pip install -q -r requirements-dev.txt -r src/requirements.txt
	PYTHONPATH=src pytest tests/ -v --cov=src --cov-report=term-missing

## Lint with ruff (optional, install with: pip install ruff)
lint:
	ruff check src/ tests/

## Local invocation with a sample S3 event
local-invoke:
	sam local invoke AIGuardFunction --event events/sample_s3_event.json --env-vars .env.json

## Remove build artefacts
clean:
	rm -rf .aws-sam/
