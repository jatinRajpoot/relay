FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY pyproject.toml ./
COPY relay ./relay
RUN pip install --no-cache-dir .
CMD ["sh", "-c", "exec uvicorn relay.app:create_app --app-dir /app --factory --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --no-access-log --proxy-headers"]
