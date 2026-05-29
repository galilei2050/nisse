FROM python:3.12-slim

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

# git: pip-install baski from GitHub via uv.tools.sources.
# uv: deterministic install from uv.lock.
RUN apt-get update && \
    apt-get install -y --no-install-recommends git ca-certificates curl && \
    rm -rf /var/lib/apt/lists/* && \
    curl -LsSf https://astral.sh/uv/install.sh | sh && \
    cp /root/.local/bin/uv /usr/local/bin/uv

WORKDIR /app

# Lock first, source after: keeps the dependency layer cached across code edits.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . ./
RUN uv sync --frozen --no-dev && chmod +x entrypoint.sh

ENV PATH="/app/.venv/bin:$PATH"
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["backend"]
