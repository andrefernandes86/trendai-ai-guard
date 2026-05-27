.PHONY: build deploy deploy-guided destroy test lint clean validate

## Build Lambda package (requires Docker for --use-container)
build:
	sam build --use-container

## First-time interactive deploy
deploy-guided: build
	sam deploy --guided

## Deploy using samconfig.toml
deploy: build
	sam deploy

## Validate the CloudFormation template (no AWS credentials needed)
validate:
	sam validate --lint

## Tear down the stack (log bucket is retained by DeletionPolicy: Retain)
destroy:
	sam delete

## Run the test suite (no live AWS or TM credentials needed)
test:
	pip install -q -r requirements-dev.txt -r src/requirements.txt
	PYTHONPATH=src pytest tests/ -v --cov=src --cov-report=term-missing

## Lint Python source
lint:
	ruff check src/ tests/

## Local invocation with the sample S3 event (requires .env.json)
local-invoke:
	sam local invoke AIGuardFunction \
		--event events/sample_s3_event.json \
		--env-vars .env.json

## Remove build artefacts
clean:
	rm -rf .aws-sam/
