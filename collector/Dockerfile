FROM python:3.11-slim
WORKDIR /app

# ── Katman 1: build araçları + bağımlılıklar ────────────────────────────────
# Ayrı, önbelleklenen katman. PyPI'ye YALNIZ bu satır (pyproject bağımlılıkları
# ile elle eşle) değiştiğinde erişilir. Kaynak kod değişiklikleri bu katmanı
# bozmaz — böylece kod rebuild'i ağ/PyPI kesintisinden etkilenmez.
RUN pip install --no-cache-dir \
      "setuptools>=68" wheel \
      "httpx>=0.27" \
      "PyYAML>=6.0" \
      "asyncpg>=0.29" \
      "cryptography>=42.0" \
      "fastapi>=0.111" \
      "uvicorn>=0.30" \
      "python-multipart>=0.0.9" \
      "ldap3>=2.9"

# ── Katman 2: paket kaynağı ─────────────────────────────────────────────────
# Sık değişir. --no-build-isolation: katman 1'deki setuptools kullanılır (indirmez).
# --no-deps: bağımlılık çözümü/indirimi yapılmaz. Sonuç: kaynak rebuild'i
# TAMAMEN çevrimdışı çalışır (PyPI gerekmez).
COPY pyproject.toml ./
COPY dahua_monitor ./dahua_monitor
RUN pip install --no-cache-dir --no-build-isolation --no-deps .

CMD ["dahua-monitor", "--config", "/app/devices.yaml"]
