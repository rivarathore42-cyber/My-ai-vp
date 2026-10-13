# Galaxy AI Engine v7 on a Hugging Face Space (Docker SDK). The Space's app_port is 7860.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=7860 \
    GALAXY_DB=/app/data/galaxy_memory.db \
    GALAXY_GGUF_PATH=/app/models/model.gguf \
    GALAXY_GGUF_REPO=Qwen/Qwen2.5-1.5B-Instruct-GGUF \
    GALAXY_GGUF_FILE=qwen2.5-1.5b-instruct-q4_k_m.gguf \
    GALAXY_MODEL_PRESET=qwen \
    HF_HOME=/app/.cache/huggingface

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential cmake \
 && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 user
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The local model engine. If it fails to build, the engine still starts in heuristics mode.
RUN pip install --no-cache-dir llama-cpp-python || echo "llama-cpp-python not installed: heuristics mode"

COPY . .

# Optional offline voice (Vosk). Off by default; see README before enabling.
ARG FETCH_VOICE=0
RUN FETCH_VOICE=$FETCH_VOICE python scripts/fetch_voice_assets.py

RUN mkdir -p /app/data /app/models /app/logs /app/.cache \
 && chown -R user:user /app

USER user
EXPOSE 7860
CMD ["sh", "-c", "python scripts/fetch_model.py; exec python run.py"]
