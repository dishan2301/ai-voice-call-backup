FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SCHEDULER_ENABLED=false TTS_ENABLED=false
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY scripts scripts
COPY deploy deploy
RUN useradd --uid 10001 --create-home appuser && mkdir -p /app/data /app/secrets && chown -R appuser:appuser /app
USER appuser
EXPOSE 10000
CMD ["python", "deploy/start.py"]
