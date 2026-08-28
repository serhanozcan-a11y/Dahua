# 1. Araştırma Bulguları

## 1.1 Cihaz ailesi: Dahua 608 serisi

608 serisi, Dahua'nın kurumsal/proje sınıfı NVR ailesidir. Sahada iki ana nesil bulunur:

| Nesil | Örnek modeller | Firmware hattı | Not |
|---|---|---|---|
| Eski (4KS2) | NVR608-32-4KS2, NVR608-64-4KS2 | V3.2xx (örn. V3.216, 2019) | 8 × SATA III, toplamda ~48 TB, RAID 0/1/5/6/10/50/60 + global hot-spare |
| Yeni (XI / WizMind) | NVR608H-32-XI, NVR608H-64-XI, NVR608H-128-XI | V4.x / V5.x | Aynı RAID yetenekleri, daha yeni web arayüzü (RPC2 ağırlıklı), daha sıkı güvenlik varsayılanları |

Her iki nesil de donanımsal olarak RAID ve hot-spare destekler; fark **yönetim
arayüzlerinde ve API davranışında**dır. İzleme uygulamasının "sürücü katmanı" bu farkları
soyutlamalıdır.

## 1.2 Veri toplama yolları

### A) HTTP CGI API (birincil yol)

Dahua'nın resmî "HTTP API" dokümanındaki (Dahua_HTTP_API) CGI uç noktaları hem eski hem
yeni firmware'lerde büyük oranda ortaktır:

| Amaç | Uç nokta |
|---|---|
| Cihaz kimliği | `GET /cgi-bin/magicBox.cgi?action=getDeviceType` / `getSerialNo` / `getSoftwareVersion` / `getSystemInfo` |
| Disk listesi + kapasite + durum | `GET /cgi-bin/storageDevice.cgi?action=getDeviceAllInfo` |
| Depolama adları | `GET /cgi-bin/storageDevice.cgi?action=getCollect` (bazı sürümlerde `factory.getCollect`) |
| SMART detayı | `GET /cgi-bin/storageDevice.cgi?action=getSmartInfo&name=<dev>` (sürüme göre değişir; bazı sürümlerde SMART verisi `getDeviceAllInfo` çıktısında `HealthDataFlag`/`SmartInfo` alanları olarak gelir) |
| Anlık olay aboneliği | `GET /cgi-bin/eventManager.cgi?action=attach&codes=[StorageFailure,StorageLowSpace,StorageNotExist]` — uzun ömürlü multipart HTTP akışı; NVR olayı anında iletir |

`getDeviceAllInfo` tipik çıktısı (anahtar=değer satırları) disk başına `Name`, `State`
(`Success`/`Failure`...), `TotalBytes`, `UsedBytes`, `IsError`, `Type` alanlarını içerir;
RAID kurulu cihazlarda mantıksal birim (ör. `/dev/md0`) ve üye diskler ayrı kayıtlar
olarak listelenir.

**Kimlik doğrulama farkları (kritik):**
- Çok eski firmware: HTTP **Basic** auth kabul eder.
- Güncel tüm firmware'ler: yalnızca **Digest** auth (Basic kapalı). İstemci kütüphanesi
  her ikisini de denemelidir (önce Digest, düşerse Basic).
- Yeni firmware'lerde art arda hatalı parola cihazın IP'yi geçici olarak **karantinaya
  alması**na (account lockout) yol açar — toplayıcı yanlış parolada agresif retry
  yapmamalıdır.
- Bazı yeni sürümlerde HTTPS zorunlu kılınabilir / öz-imzalı sertifika döner; istemci
  sertifika doğrulamasını cihaz-başına yapılandırılabilir tutmalıdır.

### B) RPC2 (web arayüzünün JSON-RPC kanalı)

Yeni (XI) arayüz, tarayıcıda `POST /RPC2_Login` + `POST /RPC2` JSON çağrılarıyla çalışır.
RAID **detayı** (seviye, degrade/rebuild durumu, rebuild yüzdesi, üye diskler, hot-spare)
bazı firmware'lerde CGI tarafında eksikken RPC2 tarafında tamdır (web arayüzü RAID
ekranını buradan besler; örn. `RAID.getDevices`, `RAID.getSubDevices`,
`storage.getDeviceAllInfo` metotları). Plan: CGI'ın verdiği yerde CGI, vermediği yerde
aynı oturumla RPC2 kullanmak. (RPC2 resmî olarak belgelenmemiştir; sürüm başına test
zorunlu — bu yüzden birincil değil tamamlayıcı yoldur.)

### C) SNMP (yedek yol + trap ile anlık bildirim)

Dahua NVR'lar SNMP v1/v2c/v3 destekler (web arayüzünden etkinleştirilir). Enterprise OID
kökü `1.3.6.1.4.1.1004849` (DAHUA-SNMP-MIB, LibreNMS deposunda mevcut):

- `physicalVolumeInfoTable` (…2.4.1): disk başına `physicalVolumeName`,
  `physicalVolumeStatus`, toplam/boş alan, eşik (`physicalVolumeThreshold`)
- Trap'ler: `storageFailureEvent` (…2.11.13.1), `storageLowSpaceEvent` (…2.11.13.2),
  `storageInOutEvent` (…2.11.13.3), `storageSMARTAbnormityEvent` (…2.11.13.4)
- **Sınırlama:** MIB'in yaygın sürümünde RAID hacim tablosu devre dışıdır (yorum satırı);
  yani SNMP tek başına RAID sağlığı için yeterli değildir. SNMP; erişilebilirlik, kaba
  disk durumu ve **trap ile anlık olay** için yedek kanal olarak kullanılacaktır.

Hazır referans: Intelbras/Dahua NVR'lar için topluluk Zabbix SNMP şablonu
(diasdmhub/Intelbras_NVR_Zabbix_Template) — OID doğrulamada test aracı olarak faydalı.

### D) Dahua NetSDK (son çare)

Dahua'nın resmî General NetSDK'sı (C kütüphanesi + resmî Python sarmalayıcı) tüm firmware
nesilleriyle çalışır; `QueryDevState` ile disk/RAID durumu sorgulanabilir ve kalıcı olay
aboneliği kurulabilir. Dezavantajı: kapalı kaynak ikili bağımlılık, dağıtımı ağırlaştırır.
Yalnızca CGI + RPC2 + SNMP üçlüsünün de yetersiz kaldığı çok eski firmware'ler için
adaptör olarak plana alınmıştır (Faz 3, opsiyonel).

## 1.3 Sürüm farklarının özeti ve stratejisi

| Konu | Eski (3.2xx) | Yeni (4.x/5.x XI) | Strateji |
|---|---|---|---|
| Auth | Basic (bazılarında Digest) | Yalnız Digest, lockout korumalı | Digest→Basic sıralı dene; başarısızlıkta backoff |
| Disk bilgisi | CGI `getDeviceAllInfo` | Aynı CGI çalışır | Ortak parser |
| SMART | Sınırlı / bazı sürümlerde yok | `getSmartInfo` / RPC2 ile zengin | Yeteneğe göre alan bazlı degrade |
| RAID detayı | CGI'da kısmi | RPC2'de tam | Önce CGI, eksikse RPC2 |
| HTTPS | Genelde kapalı | Açık/zorunlu olabilir | Cihaz-başına şema + sertifika ayarı |
| SNMP | v1/v2c | v2c/v3 (v3 önerilir) | Yedek kanal + trap alıcısı |

**Keşif (probe) yaklaşımı:** Toplayıcı bir cihazı envantere alırken sırayla yolları dener,
çalışan yetenek setini (`auth=digest`, `smart=cgi`, `raid=rpc2`, `snmp=v2c` gibi) cihaz
kaydına yazar; sonraki turlarda doğrudan bu profili kullanır. Firmware güncellemesi
algılanırsa (sürüm değişimi) profil yeniden keşfedilir.

## 1.4 Kaynaklar

- Dahua HTTP API dokümanı (v2.x, "all CGIs") — storageDevice/magicBox/eventManager uç noktaları
- DAHUA-SNMP-MIB (LibreNMS mibs/dahua) — OID listesi yukarıda
- Dahua SNMP wiki sayfası (dahuawiki.com/SNMP), RAID sayfaları (dahuawiki.com/RAID)
- NVR608-32-4KS2 ürün sayfası/datasheet — 8×SATA, RAID 0/1/5/6/10/50/60, hot-spare
- Topluluk Zabbix şablonu: diasdmhub/Intelbras_NVR_Zabbix_Template
