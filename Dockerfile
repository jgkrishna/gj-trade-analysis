FROM python:3.11-slim

WORKDIR /app

# System deps for scipy/matplotlib wheels build cleanly on slim images
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render/Railway inject $PORT at runtime; default 8501 lets `docker run` work locally too.
ENV PORT=8501
EXPOSE 8501

# Shell form (not exec form) so ${PORT:-8501} actually expands.
CMD streamlit run dashboard.py \
    --server.port=${PORT:-8501} \
    --server.address=0.0.0.0 \
    --server.headless=true
