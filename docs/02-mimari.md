# 2. Sistem Mimarisi

## 2.1 Genel görünüm

```
                          CCTV VLAN (yalnız çıkış izinli)
 ┌──────────┐  HTTP(S) CGI/RPC2  ┌────────────────────┐
 │ NVR #1   │◄───────────────────│                    │
 │ (3.216)  │  SNMP get/trap     │   Collector        │      ┌──────────────┐
 ├──────────┤◄───────────────────│   (Python servis)  │─────►│ PostgreSQL + │
 │ NVR #2   │  event attach      │  - sürücü katmanı  │      │ TimescaleDB  │
 │ (5.x XI) │◄───────────────────│  - zamanlayıcı     │      └──────┬───────┘
 ├──────────┤                    │  - trap alıcısı    │             │
 │  ...     │                    └────────────────────┘      ┌──────▼───────┐
 └──────────┘                                                │ API (FastAPI)│
                                                             └──────┬───────┘
                                              ┌───────────┐  ┌──────▼───────┐
                                              │ Alarm     │◄─│ Pano (Grafana│
                                              │ (mail/    │  │ veya web UI) │
                                              │ Telegram) │  └──────────────┘
                                              └───────────┘
```

Cihazlara hiçbir kurulum yapılmaz; tek gereksinim her NVR'da izleme için açılmış bir
**salt-okunur / düşük yetkili kullanıcı** ve (istenirse) SNMP'nin etkinleştirilmesidir.

## 2.2 Bileşenler

### Collector (toplayıcı servis) — Python 3.11+
- **Sürücü katmanı (adapter pattern):** `CgiDriver` (httpx + Digest/Basic),
  `Rpc2Driver`, `SnmpDriver` (pysnmp), opsiyonel `NetSdkDriver`. Ortak arayüz:
  `get_device_info()`, `get_disks()`, `get_raid()`, `subscribe_events()`.
- **Keşif (probe):** yeni cihaz eklenince yetenek profili çıkarır ve DB'ye yazar
  (bkz. araştırma §1.3).
- **Zamanlayıcı:** cihaz başına eşzamanlı (asyncio) polling —
  disk/kapasite: 5 dk, RAID durumu: 5 dk (rebuild sırasında 1 dk), erişilebilirlik: 1 dk,
  SMART detayı: 1 saat. Aralıklar yapılandırılabilir.
- **Olay kanalı:** her cihaza `eventManager.cgi?action=attach` uzun bağlantısı (kopunca
  üstel backoff ile yeniden bağlanır) + merkezi SNMP trap alıcısı (UDP 162). Olaylar
  polling'i beklemeden alarm üretir.
- **Dayanıklılık:** cihaz başına hata sayacı, lockout'a düşmemek için yanlış-parola
  algılandığında o cihazı duraklatıp yönetici alarmı üretme.

### Veritabanı — PostgreSQL 15 + TimescaleDB
- Envanter tabloları: `nvr`, `disk`, `raid_array`, `raid_member`
- Zaman serisi (hypertable): `disk_metrics(ts, disk_id, used_bytes, total_bytes, temp, smart_status, state)`,
  `raid_metrics(ts, raid_id, status, rebuild_pct)`, `nvr_metrics(ts, nvr_id, reachable, latency_ms)`
- Olay tablosu: `event(ts, nvr_id, source, code, severity, payload_json, acked_by)`
- Saklama politikası: ham metrik 90 gün, saatlik özet 2 yıl (Timescale continuous aggregate).

### API — FastAPI
- Panoya ve entegrasyonlara REST/JSON: filo özeti, cihaz detayı, geçmiş grafikleri,
  alarm listesi ve onaylama (ack), envanter CRUD.
- Kimlik doğrulama: yerel kullanıcı + (varsa) LDAP/AD entegrasyonu.
- Prometheus uyumlu `/metrics` ucu — ileride şirket izleme altyapısına takılabilsin diye.

### Pano
- **Faz 1'de Grafana** (hazır, hızlı): filo genel durumu (kaç NVR up, kaç disk arızalı,
  degrade RAID listesi), cihaz detay sayfası, kapasite doluluk trendi ve "disk dolmasına
  kalan gün" projeksiyonu.
- İhtiyaç netleşirse Faz 3'te özel web UI (React) opsiyonu.

### Alarm motoru
- Kanallar: e-posta (SMTP), Telegram bot, opsiyonel SMS ağ geçidi / webhook (Teams/Slack).
- Tekilleştirme + tekrarlı hatırlatma (degrade RAID çözülmediyse 24 saatte bir),
  bakım penceresi (maintenance window) desteği.

## 2.3 Alarm kuralları (başlangıç seti)

| Kural | Eşik | Önem |
|---|---|---|
| NVR erişilemiyor | 3 ardışık başarısız kontrol (~3 dk) | Kritik |
| RAID Degraded / Failed | anında (olay veya polling) | Kritik |
| RAID Rebuilding | bilgi + tamamlanınca kapanış bildirimi | Uyarı |
| Disk State != OK / StorageFailure olayı | anında | Kritik |
| SMART anormalliği | anında | Yüksek |
| Doluluk (kayıt üzerine yazmayan yapılandırmada) | %85 uyarı / %95 kritik | Uyarı–Kritik |
| Disk sayısı düştü (StorageNotExist / InOut) | anında | Yüksek |
| Firmware sürümü değişti | bilgi | Bilgi |
| Sertifika/parola hatası (lockout riski) | anında, cihaz duraklatıldı | Yüksek |

Not: NVR'lar tipik olarak döngüsel (overwrite) kayıt yapar; bu modda %100 doluluk
normaldir. Doluluk alarmı yalnızca overwrite kapalı cihazlarda anlamlıdır — kural cihaz
başına açılıp kapatılabilir olacak.

## 2.4 Güvenlik

- Her NVR'da yalnız izleme yetkili ayrı kullanıcı; parolalar uygulama DB'sinde **şifreli**
  (Fernet/KMS) saklanır, loglara asla yazılmaz.
- Toplayıcı CCTV VLAN'ına tek yönlü erişen bir sunucuda çalışır; NVR'lardan internete
  çıkış gerekmez.
- SNMP kullanılacaksa v3 (authPriv) tercih; v2c zorunluysa community string cihaz başına
  benzersiz ve ACL'li.
- HTTPS destekleyen cihazlarda HTTPS + sertifika sabitleme (pinning) seçeneği.
- Uygulama erişimi rol bazlı: izleyici / operatör (ack) / yönetici (envanter, kimlik).

## 2.5 "Kendin yaz" vs Zabbix karşılaştırması

| Kriter | Özel uygulama (önerilen) | Zabbix + SNMP şablonu |
|---|---|---|
| RAID detayı (rebuild %, üye diskler) | ✔ (CGI/RPC2) | ✖ (MIB'de RAID yok) |
| SMART detayı | ✔ (sürüm destekliyorsa) | Kısmi (yalnız trap) |
| Eski+yeni firmware tek çatı | ✔ sürücü katmanı | Kısmi |
| Kurulum hızı | Orta (geliştirme gerekir) | Hızlı |
| Bakım | Kendi kodun | Hazır ürün |

**Öneri:** Özel toplayıcı asıl çözüm; istenirse toplayıcının Prometheus `/metrics` ucu
üzerinden mevcut Zabbix/Grafana altyapısına da beslenebilir. Zabbix yalnız SNMP ile
kurulursa RAID görünürlüğü elde edilemediği için tek başına yeterli değildir.
