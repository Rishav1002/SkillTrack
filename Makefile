SHELL := /bin/bash

setup:
	bash scripts/setup.sh

seed:
	PYTHONPATH=. python3 -c "from backend.app.main import seed_data; seed_data(reset=True)"

ml:
	python3 ml/evaluate_scoring.py
	python3 ml/train_placement_model.py

build:
	cd frontend && npm run build

start:
	python run_demo.py

test:
	PYTHONPATH=. pytest -q backend/tests

lint:
	ruff check backend ml
	cd frontend && npm run lint

embeddings:
	pip install -r backend/requirements-embeddings.txt
	python ml/evaluate_scoring.py
	python ml/train_placement_model.py
