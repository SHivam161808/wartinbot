FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for numpy and faiss
RUN apt-get update && apt-get install -y \
    g++ \
    gcc \
    libopenblas-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefer-binary -r requirements.txt

COPY . .

CMD uvicorn backend.server:app --host 0.0.0.0 --port $PORT