# Fixed app image for Phase 2 pilot (SPEC §6): web and worker share this image.
FROM python:3.12-slim-bookworm

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml alembic.ini ./
COPY src ./src
COPY migrations ./migrations

RUN pip install --no-cache-dir -e .

EXPOSE 8000

# Default = web; compose worker overrides command.
CMD ["python", "-m", "uvicorn", "support_platform.main:app", "--host", "0.0.0.0", "--port", "8000"]
