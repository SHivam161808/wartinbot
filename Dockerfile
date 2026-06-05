FROM python:3.11-slim

WORKDIR /app

# Install system dependencies (gcc, etc.) needed for numpy/faiss
RUN apt-get update && apt-get install -y \
    g++ \
    gcc \
    libopenblas-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first (better caching)
COPY requirements.txt .

# Install Python dependencies, including uvicorn
RUN pip install --no-cache-dir --prefer-binary -r requirements.txt uvicorn

# Copy the rest of the application
COPY . .

# Run uvicorn, binding to all interfaces and using Render's $PORT
CMD uvicorn backend.server:app --host 0.0.0.0 --port $PORT