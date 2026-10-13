# Galaxy AI Engine v7 on Render.com (Docker web service). Render sets $PORT at run time; run.py listens on it.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=10000 \
    GALAXY_DB=/app/data/galaxy_memory.db \
    GALAXY_SKIP_LLM=1

RUN useradd -m -u 1000 user
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Optional local model engine. Off by default: a model does not fit a free instance's memory.
ARG INSTALL_LLM=0
RUN if [ "$INSTALL_LLM" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends build-essential cmake \
      && rm -rf /var/lib/apt/lists/* \
      && (pip install --no-cache-dir llama-cpp-python || echo "llama-cpp-python not installed: heuristics mode"); \
    fi

COPY . .

# Optional offline voice (Vosk). Off by default; see README.
ARG FETCH_VOICE=0
RUN FETCH_VOICE=$FETCH_VOICE python scripts/fetch_voice_assets.py

RUN mkdir -p /app/data /app/logs && chown -R user:user /app

USER user
CMD ["sh", "-c", "python scripts/fetch_model.py; exec python run.py"]
