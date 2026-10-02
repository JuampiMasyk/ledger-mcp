FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LEDGER_DB=/app/data/ledger.db

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && ledger-seed --db /app/data/ledger.db --golden /app/evals/golden.json

# Run as a non-root user; the database is opened read-only by the server anyway.
RUN useradd --create-home app && chown -R app /app
USER app

EXPOSE 8000
CMD ["ledger-mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000"]
