FROM python:3.12.15-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
ENV PYTHONUNBUFFERED=1 UV_PROJECT_ENVIRONMENT=/app/.venv UV_PYTHON_DOWNLOADS=never \
    PATH=/app/.venv/bin:$PATH COPILOT_DATA_DIR=/data COPILOT_MODEL_DIR=/models \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 ANONYMIZED_TELEMETRY=False OMP_NUM_THREADS=2
WORKDIR /app/backend
RUN pip install --no-cache-dir uv==0.12.23 && useradd --uid 10001 --create-home copilot \
    && mkdir /data /models && chown copilot:copilot /data /models
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --locked --no-dev --python /usr/local/bin/python
COPY backend/copilot/ ./copilot/
COPY backend/ui/ ./ui/
COPY backend/models.lock.json ./models.lock.json
USER 10001:10001
EXPOSE 3001
CMD ["python", "-m", "copilot", "serve", "--container"]
