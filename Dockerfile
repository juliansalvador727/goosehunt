FROM python:3.12-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY requirements.txt .
# Install CPU-only torch first so sentence-transformers doesn't pull the CUDA build (~3 GB vs ~750 MB)
RUN uv pip install --system --no-cache torch --index-url https://download.pytorch.org/whl/cpu
RUN uv pip install --system --no-cache -r requirements.txt

COPY . .

EXPOSE 8000
ENTRYPOINT ["sh", "docker-entrypoint.sh"]
