# Dahua 608 NVR Filo İzleme — Proje Rehberi ve Aktif Görev

Bu depo, Dahua 608 serisi NVR'ların disk kapasitesi, RAID sağlığı, SMART,
anlık olay ve saklama derinliği (en eski kayıt tarihi) izlemesi için tam bir
sistemdir. Kod tamamlandı ve simülatörle uçtan uca doğrulandı; sıradaki iş
sahaya kurulumdur (aşağıdaki GÖREV bölümü).

## AKTİF GÖREV: 10.34.57.182 sunucusunda devreye alma

Bu depoyu açan Claude oturumunun görevi (kullanıcı "görevi tamamla" derse
buradan devam et):

1. **Kur:** Bu makine 10.34.57.182 ise doğrudan; değilse SSH ile bağlanarak:
   `./install.sh` (Docker + Compose gerektirir). Betik `.env`'i rastgele
   parolalarla üretir ve ekrana yazar — parolaları KULLANICIYA GÖSTER,
   kaydetmesini söyle.
2. **Doğrula:** `docker compose ps` (4 servis ayakta), `docker compose logs
   collector`. Panel: http://10.34.57.182:8000 (admin + PANEL_PASSWORD).
3. **Cihazları ekle:** Kullanıcıdan NVR IP'lerini ve izleme kullanıcısı
   bilgilerini iste (her NVR'da salt-okunur `monitor` hesabı açılmalı).
   Panelden ekle, sonra `docker compose restart collector`.
4. **Faz 0 saha doğrulaması (kritik):** İlk gerçek cihaz eklendikten sonra
   collector loglarını incele. Ayrıca bir cihazdan
   `http://IP/cgi-bin/storageDevice.cgi?action=getDeviceAllInfo` çıktısını al;
   ayrıştırma hatası görürsen `collector/dahua_monitor/parsing.py` ve
   `drivers/cgi.py` içindeki alan eşlemelerini gerçek çıktıya göre düzelt,
   testine ekle (`collector/tests/`), commit'le.
5. **Bitti sayılır:** Panelde tüm NVR'lar "erişilebilir", disk/RAID verileri
   akıyor, bir test alarmı (kablo çekme değil — devices.yaml'a sahte cihaz
   ekleyip kaldırmak yeterli) e-posta/log kanalına düşüyor.

Sorun çıkarsa: README "Sorun giderme" bölümü + docs/01-arastirma.md (firmware
farkları). Kod değişikliği yaparsan testleri koştur ve mevcut dala push et.

## Yapı

- `collector/` — Python 3.11 toplayıcı (`dahua_monitor` paketi):
  `drivers/cgi.py` (Digest→Basic auth, lockout koruması), `drivers/rpc2.py`
  (deneysel, varsayılan kapalı), `scheduler.py` (polling + anlık olay akışı +
  günlük retention), `alerts.py` (alarm motoru), `panel.py` + `panel_ui.html`
  (web arayüzü + JSON API), `store.py`, `parsing.py`
- `simulator/nvr_sim.py` — sahte NVR (`SIM_SCENARIO=healthy|disk_error|raid_degraded`)
- `db/schema.sql` — PostgreSQL/TimescaleDB şeması
- `docker-compose.yml`, `install.sh` — dağıtım; `grafana/` opsiyonel pano
- `docs/` — araştırma, mimari, yol haritası

## Komutlar

```bash
cd collector && pip install -e ".[dev]" && pytest    # testler (19 adet)
./install.sh                                          # tam kurulum
docker compose logs -f collector                      # canlı log
SIM_SCENARIO=raid_degraded python simulator/nvr_sim.py  # arıza senaryosu
```

## Kurallar

- `.env` ve `devices.yaml` asla commit edilmez (.gitignore'da).
- NVR parolaları yalnız `.env`'e (dosya yönetimi) veya panele (DB'de Fernet
  şifreli) girilir; koda/log'a düz metin parola yazma.
- Yanlış parolayla tekrar deneme YAPMA: yeni firmware'ler IP'yi kilitler.
  Collector bu durumda cihazı kendiliğinden duraklatır.
- Çalışma dalı: `claude/dahua-608-nvr-monitoring-l09z3l`.
