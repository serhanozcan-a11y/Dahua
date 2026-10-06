#!/usr/bin/env bash
# Dahua NVR izleme — tek komut kurulum (izleme sunucusunda çalıştırılır,
# örn. .182 numaralı sunucu). Rastgele parolalarla .env üretir ve yığını başlatır.
set -euo pipefail
cd "$(dirname "$0")"

command -v docker >/dev/null 2>&1 || {
    echo "HATA: Docker kurulu değil. https://docs.docker.com/engine/install/"; exit 1; }
docker compose version >/dev/null 2>&1 || {
    echo "HATA: 'docker compose' eklentisi yok."; exit 1; }

if [ ! -f .env ]; then
    DB_PW=$(openssl rand -hex 16)
    GRAFANA_PW=$(openssl rand -hex 8)
    PANEL_PW=$(openssl rand -hex 8)
    SECRET=$(openssl rand -base64 32 | tr '+/' '-_')
    cat > .env <<EOF
DB_PASSWORD=$DB_PW
GRAFANA_PASSWORD=$GRAFANA_PW
PANEL_PASSWORD=$PANEL_PW
SECRET_KEY=$SECRET
EOF
    chmod 600 .env
    echo "==> .env üretildi (chmod 600)."
    echo "    Grafana : admin / $GRAFANA_PW"
    echo "    Panel   : admin / $PANEL_PW"
    echo "    Bu parolaları güvenli bir yere kaydedin."
else
    echo "==> Mevcut .env korunuyor."
fi

# devices.yaml opsiyonel: cihazlar panelden de eklenebilir
[ -f devices.yaml ] || { touch devices.yaml; echo "==> Boş devices.yaml oluşturuldu (cihazları panelden ekleyin)."; }

docker compose up -d --build
IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "SUNUCU-IP")
echo
echo "==> Kurulum tamam."
echo "    Panel (NVR ekleme):  http://$IP:8000   (admin / .env: PANEL_PASSWORD)"
echo "    Grafana panosu:      http://$IP:3000   (admin / .env: GRAFANA_PASSWORD)"
echo "    Cihaz ekledikten sonra: docker compose restart collector"
