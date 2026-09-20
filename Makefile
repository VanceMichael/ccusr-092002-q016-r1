
.PHONY: migrate seed test run
migrate:
	python -m scripts.migrate
seed:
	python -m scripts.seed
test:
	python -m unittest discover -s tests
run:
	python -m app.main
