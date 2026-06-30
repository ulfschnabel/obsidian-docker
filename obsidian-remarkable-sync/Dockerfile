FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    pandoc \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libgdk-pixbuf-xlib-2.0-0 \
    libffi-dev \
    shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .
COPY obsidian_remarkable_sync/ obsidian_remarkable_sync/

RUN pip install --no-cache-dir -e ".[dev]" 2>/dev/null || pip install --no-cache-dir -e .

# rmapi binary copied from Calibre container's config
COPY rmapi /usr/local/bin/rmapi
RUN chmod +x /usr/local/bin/rmapi

CMD ["obsidian-remarkable-sync"]
