# Release tooling for the Cairn CLI (cairn-cli on PyPI).
#
#   make build      build sdist + wheel into dist/
#   make check      twine metadata check on the built artifacts
#   make clean       remove build artifacts
#   make release     clean + build + check + upload to PyPI
#
# Upload uses twine; supply credentials via ~/.pypirc or the
# TWINE_USERNAME / TWINE_PASSWORD env vars (use __token__ + an API token).

PYTHON ?= python3
VERSION := $(shell $(PYTHON) -c "import tomllib;print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")

.PHONY: build check clean release

build: clean
	$(PYTHON) -m build

check: build
	$(PYTHON) -m twine check dist/*

clean:
	rm -rf dist build *.egg-info cairn_cli.egg-info

release: clean build check
	@echo "Uploading cairn-cli $(VERSION) to PyPI..."
	$(PYTHON) -m twine upload dist/*
	@echo "Done. Tag the release with: git tag cli-v$(VERSION) && git push --tags"
