.PHONY: run makemigrations migrate test tailwind-download tailwind-watch tailwind-build

build:
	docker build .

run:
	uv run manage.py runserver

makemigrations:
	uv run manage.py makemigrations

migrate:
	uv run manage.py migrate

lint:
	uv run ruff format .
	uv run ruff check --fix .
	uv run ty check

test:
	uv run manage.py test wad

tailwind-watch:
	tailwindcss -i static/css/input.css -o static/css/output.css --watch

tailwind-build:
	tailwindcss -i static/css/input.css -o static/css/output.css --minify
