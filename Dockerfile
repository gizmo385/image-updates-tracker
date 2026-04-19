FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --no-install-project

COPY *.py ./
COPY templates/ templates/

EXPOSE 8585

CMD ["uv", "run", "gunicorn", "--bind", "0.0.0.0:8585", "server:app"]
