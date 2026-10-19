FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY pyproject.toml ./
COPY relay ./relay
RUN pip install --no-cache-dir .
# The container is only reachable through the hosting platform's front end, so the
# client address arrives in X-Forwarded-For. Without --forwarded-allow-ips uvicorn
# trusts that header only from 127.0.0.1; every request would then look like it came
# from the proxy, and the per-address sign-in throttle would apply to all users at once.
CMD ["sh", "-c", "exec uvicorn relay.app:create_app --app-dir /app --factory --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --no-access-log --proxy-headers --forwarded-allow-ips=*"]
