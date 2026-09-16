FROM python:3.12-slim
WORKDIR /app
RUN useradd --create-home --uid 10001 appuser
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
COPY . .
USER appuser
# Offline core only; no network bot/server is exposed by this image.
CMD ["python", "-m", "velmora.demo"]
