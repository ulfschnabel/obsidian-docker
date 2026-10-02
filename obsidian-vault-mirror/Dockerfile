FROM python:3.11-slim

WORKDIR /app

# vaultstore comes from the compose build context of the same name (additional_contexts).
RUN --mount=type=bind,from=vaultstore,target=/src/vaultstore,rw \
    pip install --no-cache-dir /src/vaultstore

COPY mirror.py .

# Owns /mirror and /state; consumers mount the mirror read-only.
USER 1000:1000

CMD ["python", "mirror.py"]
