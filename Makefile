.PHONY: build run seed makemigrations migrate lint static test test-offline tailwind-watch tailwind-build

build:
	docker build .

run: migrate seed
	uv run manage.py runserver 127.0.0.1:8080

seed:
	uv run manage.py seed_dev

makemigrations:
	uv run manage.py makemigrations

migrate:
	uv run manage.py migrate

lint:
	uv run ruff format .
	uv run ruff check --fix .
	uv run ty check

static:
	uv run manage.py collectstatic --noinput

# Static files are collected first because the templates resolve them through the
# hashed-name manifest, exactly as they do in production.
test: static
	uv run pytest

# The suite minus the tests that reach the Ministry of Finance and NBP, for working
# without a network.
test-offline: static
	uv run pytest -m "not live"

tailwind-watch:
	tailwindcss -i assets/tailwind.css -o static/css/output.css --watch

tailwind-build:
	tailwindcss -i assets/tailwind.css -o static/css/output.css --minify
