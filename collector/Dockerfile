FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml ./
COPY dahua_monitor ./dahua_monitor
RUN pip install --no-cache-dir .
CMD ["dahua-monitor", "--config", "/app/devices.yaml"]
