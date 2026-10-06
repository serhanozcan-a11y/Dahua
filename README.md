# Dahua 608 Serisi NVR — Disk / RAID Sağlık İzleme Platformu

Şirket içindeki tüm Dahua 608 serisi NVR cihazlarının (NVR608-32/64-4KS2, NVR608H-XI vb.)
**disk kapasitelerini, RAID sağlığını, disk (SMART) sağlığını** merkezi olarak ve sürekli
izleyecek; eski ve yeni firmware sürümleriyle birlikte çalışacak bir izleme uygulamasının
proje planı ve tasarımı.

## Belgeler

| Belge | İçerik |
|---|---|
| [docs/01-arastirma.md](docs/01-arastirma.md) | Cihaz ailesi, firmware farkları, veri toplama yolları (HTTP CGI, RPC2, SNMP, NetSDK) — araştırma bulguları |
| [docs/02-mimari.md](docs/02-mimari.md) | Önerilen sistem mimarisi, bileşenler, veri modeli, alarm kuralları, güvenlik |
| [docs/03-yol-haritasi.md](docs/03-yol-haritasi.md) | Fazlar, iş kırılımı, efor tahmini, riskler ve kabul kriterleri |

## Özet

- **Sorun:** 608 serisi NVR'lar tekil web arayüzlerinden yönetiliyor; disk dolması, RAID
  degrade olması veya bir diskin SMART hatası vermesi ancak cihaza tek tek girilince fark
  ediliyor. Firmware sürümleri heterojen (eski 3.2xx ve yeni 4.x/5.x).
- **Çözüm:** Her NVR'ı periyodik olarak sorgulayan bir **toplayıcı (collector) servis** +
  zaman serisi veritabanı + web panosu + alarm motoru. Cihaz tarafında kurulum gerekmez;
  tüm veri NVR'ın kendi HTTP API'sinden (ve yedek olarak SNMP'den) çekilir.
- **Sürüm uyumluluğu:** Firmware farkları tek bir "sürücü (driver) katmanı" arkasına
  saklanır. Toplayıcı her cihazda önce yeteneklerini keşfeder (probe), sonra o cihaz için
  çalışan yöntemi kalıcı olarak kullanır:
  1. HTTP CGI API (`storageDevice.cgi`, `magicBox.cgi`, `eventManager.cgi`) — birincil
  2. RPC2 (web arayüzünün JSON kanalı) — RAID detayı CGI'da eksikse
  3. SNMP v2c/v3 (Dahua enterprise MIB `1.3.6.1.4.1.1004849`) — yedek + trap ile anlık olay
  4. Dahua NetSDK — CGI'sı kısıtlı çok eski firmware'ler için son çare
- **Teknoloji önerisi:** Python 3.11+ toplayıcı, PostgreSQL + TimescaleDB, FastAPI,
  Grafana panosu (veya hafif özel web UI), e-posta/Telegram/SMS alarmları. Alternatif
  olarak Zabbix tabanlı hazır çözüm karşılaştırması `docs/02-mimari.md` içinde.

## Geliştirme (Faz 1 iskeleti hazır)

```
collector/            # Python toplayıcı servis (dahua-monitor paketi)
  dahua_monitor/
    parsing.py        # Dahua anahtar=değer yanıt ayrıştırıcısı
    models.py         # DiskInfo / RaidInfo / PollResult ortak modelleri
    config.py         # devices.yaml + .env yükleme (parolalar env'den)
    drivers/          # sürücü katmanı: base (arayüz) + cgi (Digest/Basic)
    scheduler.py      # cihaz başına asyncio polling, lockout koruması
    store.py          # PostgreSQL/Timescale yazımı
    main.py           # dahua-monitor CLI girişi
  tests/              # pytest (ağ yerine httpx.MockTransport)
simulator/nvr_sim.py  # sahte NVR: healthy | disk_error | raid_degraded senaryoları
db/schema.sql         # şema (Timescale varsa hypertable'a çevirir)
docker-compose.yml    # db + collector + grafana (+ dev profilinde nvr-sim)
```

## Kurulum (sunucuda, adım adım)

Gereksinim: Docker + Docker Compose kurulu bir Linux sunucu; NVR'ların bulunduğu
CCTV ağına HTTP(S) erişimi.

1. **Tek komut kurulum** (izleme sunucusunda, örn. .182):
   ```bash
   git clone <bu-depo> Dahua && cd Dahua
   ./install.sh
   ```
   Betik; rastgele parolalarla `.env` üretir (ekrana yazar — kaydedin),
   şifreleme anahtarını (SECRET_KEY) oluşturur ve tüm yığını başlatır.
2. **Her NVR'da izleme kullanıcısı açın** (web arayüzü → Hesap): salt-okunur
   yetkili `monitor` kullanıcısı.
3. **Cihazları web panelinden ekleyin:** `http://sunucu:8000`
   (kullanıcı `admin`, parola `.env`'deki `PANEL_PASSWORD`). NVR şifreleri
   panele girilir ve veritabanında **şifreli** (Fernet/SECRET_KEY) saklanır —
   düz metin parola hiçbir dosyaya yazılmaz. Ekleme sonrası:
   ```bash
   docker compose restart collector
   ```
   (İsteyen `devices.yaml` ile dosyadan da yönetebilir; ikisi birlikte çalışır,
   aynı isimde panel kaydı önceliklidir.)
4. **Panoya girin:** `http://sunucu:3000` (kullanıcı `admin`, parola `.env`'deki
   `GRAFANA_PASSWORD`). "Dahua NVR" klasöründeki **Dahua NVR Filosu** panosu
   veri kaynağıyla birlikte otomatik kurulur — elle tanım gerekmez.
5. **Alarmları test edin:** `devices.yaml`'daki `alerting.email` bölümünü kendi
   SMTP sunucunuza göre doldurun. Gerçek cihaz riske atmadan denemek için sahte
   NVR ile arıza senaryosu üretebilirsiniz:
   ```bash
   SIM_SCENARIO=raid_degraded docker compose --profile dev up -d nvr-sim
   # devices.yaml'a sim cihazını ekleyin (örnek dosyada hazır) → kritik alarm düşer
   ```
6. **Kalıcılık:** `pg-data/` dizinini ve `grafana-data` docker volume'unu yedekleme planınıza
   ekleyin. Servisler `restart: unless-stopped` ile çöküş/yeniden başlatmada
   kendiliğinden kalkar.

Sorun giderme:
- Collector loglarında `kimlik doğrulama reddedildi ... DURAKLATILDI` görürseniz
  cihaz parolası yanlıştır; lockout koruması gereği o cihaz otomatik durdurulur —
  `.env`'i düzeltip `docker compose restart collector` deyin.
- Bir cihazdan RAID/SMART verisi gelmiyorsa firmware'i desteklemiyor olabilir;
  cihaz yine disk durumu + kapasite + erişilebilirlik ile izlenir
  (bkz. docs/01-arastirma.md §1.3).

Geliştirme ortamı testleri:

```bash
cd collector && pip install -e ".[dev]" && pytest   # 16 test
```

## Toplanacak ana metrikler

- Disk başına: model/seri no, kapasite, kullanılan alan, durum (OK / Error / Absent),
  SMART sağlık bayrağı ve (destekleyen firmware'de) sıcaklık, çalışma saati, yeniden
  tahsis edilmiş sektör sayısı
- RAID başına: seviye (RAID 0/1/5/6/10/50/60), durum (Active / Degraded / Rebuilding /
  Failed), rebuild yüzdesi, üye diskler, hot-spare durumu
- Cihaz başına: erişilebilirlik (up/down), firmware sürümü, kayıt durumu, toplam/boş alan
- Saklama derinliği: cihazdaki **en eski kaydın tarihi** günde bir sorgulanır
  (`mediaFileFind.cgi` ile kanal taraması); `min_retention_days` tanımlıysa altına
  düşüldüğünde uyarı üretilir
- Olaylar: StorageFailure, StorageLowSpace, StorageNotExist, StorageAbnormal —
  hem polling (5 dk güvence katmanı) hem **anlık olay akışı** ile
  (`eventManager.cgi` aboneliği: arıza saniyeler içinde alarma dönüşür;
  kopan bağlantı üstel backoff ile otomatik yeniden kurulur)
- RAID rebuild yüzdesi / üye diskler: RPC2 üzerinden (deneysel, cihaz başına
  `rpc2: true` ile açılır — Faz 0'da gerçek cihazda doğrulandıktan sonra)
