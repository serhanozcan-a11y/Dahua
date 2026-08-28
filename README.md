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

## Toplanacak ana metrikler

- Disk başına: model/seri no, kapasite, kullanılan alan, durum (OK / Error / Absent),
  SMART sağlık bayrağı ve (destekleyen firmware'de) sıcaklık, çalışma saati, yeniden
  tahsis edilmiş sektör sayısı
- RAID başına: seviye (RAID 0/1/5/6/10/50/60), durum (Active / Degraded / Rebuilding /
  Failed), rebuild yüzdesi, üye diskler, hot-spare durumu
- Cihaz başına: erişilebilirlik (up/down), firmware sürümü, kayıt durumu, toplam/boş alan
- Olaylar: StorageFailure, StorageLowSpace, StorageNotExist, SMART anormalliği (hem
  polling hem NVR'ın kendi olay kanalı / SNMP trap üzerinden)
