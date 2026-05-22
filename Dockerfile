FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
# Install CPU-only torch first so sentence-transformers doesn't pull the CUDA build (~3 GB vs ~750 MB)
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
ENTRYPOINT ["sh", "docker-entrypoint.sh"]
