PYTHON ?= python
CONFIG ?= operator.toml
SOURCE_DATE_EPOCH ?= 315532800
export SOURCE_DATE_EPOCH

.PHONY: help install init doctor run once status test test-core test-plugin check dist release clean

help:
	@$(PYTHON) -c "print('Targets: install init doctor run once status test check dist release clean')"

install:
	$(PYTHON) -m pip install -e .

init:
	$(PYTHON) -m hermes_operator --config $(CONFIG) init

doctor:
	$(PYTHON) -m hermes_operator --config $(CONFIG) doctor

run:
	$(PYTHON) -m hermes_operator --config $(CONFIG) run

once:
	$(PYTHON) -m hermes_operator --config $(CONFIG) run-once

status:
	$(PYTHON) -m hermes_operator --config $(CONFIG) status

test: test-core test-plugin

test-core:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v

test-plugin:
	$(PYTHON) -m unittest discover -s integrations/hermes_operator_plugin/tests -v

check:
	$(PYTHON) -m compileall -q src integrations
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests
	$(PYTHON) -m unittest discover -s integrations/hermes_operator_plugin/tests
	@$(PYTHON) -c "import pathlib,sys; roots=[pathlib.Path(p) for p in ('README.md','LICENSE','Dockerfile','compose.yaml','Makefile','deploy','docs','config','scripts','src','integrations','tests')]; files=[p for root in roots for p in ([root] if root.is_file() else root.rglob('*')) if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc']; hits=[str(p) for p in files if any(mark in p.read_text(encoding='utf-8') for mark in (chr(0x2013),chr(0x2014)))]; print('Unicode dash found in: '+', '.join(hits)) if hits else None; sys.exit(bool(hits))"

dist: clean
	$(PYTHON) -m pip wheel --no-deps --no-build-isolation --wheel-dir dist .
	$(PYTHON) -m pip wheel --no-deps --no-build-isolation --wheel-dir dist ./integrations/hermes_operator_plugin

release: check dist
	$(PYTHON) scripts/build_release.py

clean:
	$(PYTHON) -c "import pathlib,shutil; generated=[*pathlib.Path('.').rglob('__pycache__'),pathlib.Path('build'),pathlib.Path('dist'),pathlib.Path('src/hermes_operator.egg-info'),pathlib.Path('integrations/hermes_operator_plugin/build'),pathlib.Path('integrations/hermes_operator_plugin/dist'),pathlib.Path('integrations/hermes_operator_plugin/hermes_operator_plugin.egg-info')]; [shutil.rmtree(p, ignore_errors=True) for p in generated]"
