lint-dockerfile:
	docker run --rm -i hadolint/hadolint < Dockerfile

lint-python:
	ruff check .
	mypy .

test:
	pytest

