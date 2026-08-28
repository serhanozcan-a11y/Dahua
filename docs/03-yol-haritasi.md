# 3. Yol Haritası, Efor ve Riskler

## Faz 0 — Saha keşfi ve doğrulama (1 hafta)
- [ ] Tüm 608'lerin envanteri: IP, model, firmware sürümü, RAID yapılandırması
      (küçük bir tarama scripti: `magicBox.cgi?action=getSoftwareVersion`)
- [ ] Her firmware sürümünden 1 temsilci cihazda uç nokta testi:
      `getDeviceAllInfo`, `getSmartInfo`, RPC2 RAID metotları, SNMP walk
      (sonuçlar `docs/cihaz-matrisi.md` olarak kayda geçer)
- [ ] İzleme kullanıcılarının açılması, SNMP'nin (v3 tercihen) etkinleştirilmesi
- **Çıktı / kabul:** hangi sürümde hangi verinin hangi yolla alınabildiğini gösteren
  doğrulanmış uyumluluk matrisi. *Bu faz tasarım varsayımlarını doğrulamadan kod
  yazılmayacak tek kritik kapıdır.*

## Faz 1 — MVP: toplama + kayıt + temel alarm (2–3 hafta)
- [ ] Proje iskeleti (Docker Compose: collector, PostgreSQL/Timescale, Grafana)
- [ ] `CgiDriver` (Digest/Basic, ortak parser) + keşif/probe mekanizması
- [ ] Disk kapasitesi, disk durumu, erişilebilirlik polling'i; veri modeli ve yazım
- [ ] Grafana filo panosu (up/down, arızalı disk, doluluk trendi)
- [ ] E-posta alarmı: NVR down, disk Failure, doluluk eşiği
- **Kabul:** en az 3 farklı firmware'li cihaz 24 saat kesintisiz izleniyor, suni disk
  çekme testi 5 dk içinde alarm üretiyor.

## Faz 2 — RAID + SMART + anlık olaylar (2–3 hafta)
- [ ] `Rpc2Driver` ile RAID detayı (durum, rebuild %, üyeler, hot-spare)
- [ ] SMART toplama (destekleyen sürümlerde) ve SMART tabanlı erken uyarı
- [ ] `eventManager.cgi` olay akışı aboneliği + SNMP trap alıcısı
- [ ] Alarm motoru: tekilleştirme, tekrar hatırlatma, bakım penceresi; Telegram kanalı
- **Kabul:** test cihazında RAID degrade senaryosu (disk çekme) hem olay kanalından
  anında hem polling'den yakalanıyor; rebuild ilerleyişi panoda izleniyor.

## Faz 3 — Sertleştirme ve kapsam genişletme (2 hafta + opsiyonlar)
- [ ] FastAPI yönetim API'si + rol bazlı erişim, envanter yönetim ekranı
- [ ] "Disk dolmasına kalan gün" projeksiyonu, haftalık özet raporu (e-posta)
- [ ] Parola şifreleme/rotasyon prosedürü, yedekleme, runbook dokümanı
- [ ] (Opsiyonel) `NetSdkDriver` — CGI/RPC2'nin yetmediği çok eski cihaz kalırsa
- [ ] (Opsiyonel) Özel web UI, diğer Dahua serilerinin (5xxx vb.) eklenmesi
- **Kabul:** operasyon ekibi devralma eğitimi tamamlandı, runbook ile 1 hafta gölge işletim.

**Toplam tahmin:** 1 geliştiriciyle ~7–9 hafta (Faz 0 dahil). Cihaz sayısı eforu pek
etkilemez (mimari eşzamanlı polling'e göre tasarlı, yüzlerce cihaza ölçeklenir).

## Riskler ve önlemler

| Risk | Etki | Önlem |
|---|---|---|
| Eski firmware'de SMART/RAID ucu hiç yok | Veri eksikliği | Faz 0 matrisi ile erken tespit; alan bazlı degrade (en azından disk durumu+kapasite her sürümde var); gerekirse NetSDK; mümkünse firmware güncelleme önerisi |
| RPC2 belgesiz, sürümle değişebilir | Kırılganlık | RPC2 yalnız RAID detayı için; sürüm başına entegrasyon testi; CGI'a düşme (fallback) |
| Yanlış parola → cihaz lockout | NVR yönetimi kilitlenir | Tek deneme + otomatik duraklatma + yönetici alarmı |
| NVR'ların API'si zayıf donanımda yavaş | Polling yükü | Cihaz başına eşzamanlılık 1, nazik aralıklar, jitter |
| CCTV VLAN erişim kısıtları | Kurulum gecikmesi | Ağ ekibiyle Faz 0'da firewall kuralları |
| Olay akışı bağlantı kopmaları | Olay kaçırma | Polling her zaman güvence katmanı; akış sadece hızlandırıcı |

## Açık sorular (Faz 0'da netleşecek)

1. Kaç adet NVR var, firmware dağılımı nedir? (temsilci test cihazları buna göre seçilecek)
2. Cihazlarda kayıt modu overwrite mi? (doluluk alarmı kuralını belirler)
3. Şirkette hâlihazırda Zabbix/Prometheus/Grafana altyapısı var mı? (varsa pano ve alarm
   o altyapıya entegre edilir, ayrı Grafana kurulmaz)
4. Alarm kanalı tercihi: e-posta mı, Telegram/Teams mi, SMS gerekli mi?
5. İzleme sunucusu nerede koşacak (VM, Docker host), CCTV VLAN'a erişimi kim açacak?
