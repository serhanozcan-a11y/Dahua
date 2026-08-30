# Codex'e (veya başka bir yapay zekâ ajanına) Devir Dokümanı

Bu doküman, projeyi devralan Codex/ChatGPT/Claude oturumunun tüm bağlama tek
dosyadan sahip olması içindir. Ajanlar için asıl görev tanımı depo kökündeki
**AGENTS.md** (Codex) ve **CLAUDE.md** (Claude) dosyalarındadır; bu dosya durum
özeti ve devir talimatıdır.

## Proje durumu (2026-08-29 itibarıyla)

**Bitti ve doğrulandı** (geliştirme ortamında gerçek PostgreSQL + sahte NVR
simülatörüyle uçtan uca; 19/19 birim testi geçiyor):

- Toplayıcı servis: HTTP CGI sürücüsü (Digest→Basic auth, lockout koruması),
  disk/kapasite/RAID polling, anlık olay akışı (eventManager attach),
  günlük "en eski kayıt tarihi" (saklama derinliği) sorgusu, RPC2 istemcisi
  (deneysel, varsayılan kapalı — RAID rebuild yüzdesi için)
- Alarm motoru: cihaz down / disk arızası / disk kayıp / RAID degrade /
  SMART / doluluk / saklama ihlali; e-posta + Telegram; tekilleştirme,
  düzelme bildirimi, 24 saat hatırlatma
- Web panel (:8000): İzleme Panosu (KPI, cihaz detayı, grafikler, alarm
  onaylama) + Yönetim (cihaz ekleme; parolalar Fernet ile şifreli) + JSON API
- Grafana panosu (:3000, opsiyonel), TimescaleDB şeması, docker-compose,
  tek komut kurulum betiği `install.sh`

**Yapılmadı** (devralanın işi): 10.34.57.182 sunucusuna kurulum ve gerçek
cihazlarla Faz 0 saha doğrulaması. Adım adım tanım: AGENTS.md "AKTİF GÖREV".

## Devir yolları

### Yol 1 — Codex CLI (önerilen; kurulumu yapabilir)

Şirket ağına erişimi olan bir makinede:

```bash
git clone https://github.com/serhanozcan-a11y/Dahua.git && cd Dahua
git checkout claude/dahua-608-nvr-monitoring-l09z3l
codex
```

Codex, AGENTS.md'yi otomatik okur; "görevi tamamla" demek yeterlidir.

### Yol 2 — Codex cloud (chatgpt.com/codex)

ChatGPT hesabında Codex'i açıp GitHub'ı bağlayın, `serhanozcan-a11y/Dahua`
deposunu ve `claude/dahua-608-nvr-monitoring-l09z3l` dalını seçin.

> UYARI: Cloud Codex, OpenAI'nin bulutunda çalışır ve şirket iç ağına
> (10.34.57.182, NVR'lar) ERİŞEMEZ. Cloud'da yalnız kod inceleme/geliştirme
> işleri yapılabilir; kurulum ve saha doğrulaması için Yol 1 gerekir.

## Devralan ajana hazır görev promptu

```text
GÖREV: Dahua NVR izleme sistemini şirket sunucusuna kur ve devreye al.
Depo: https://github.com/serhanozcan-a11y/Dahua
Dal: claude/dahua-608-nvr-monitoring-l09z3l
Hedef: 10.34.57.182 (Docker + Compose gerekli)

1) Sunucuda ./install.sh çalıştır; üretilen parolaları kullanıcıya göster.
2) docker compose ps ve logs collector ile doğrula; panel http://10.34.57.182:8000
3) Kullanıcıdan NVR IP'lerini ve 'monitor' kullanıcı parolalarını iste;
   panelden ekle; docker compose restart collector.
4) Gerçek cihaz loglarını incele; bir cihazdan
   /cgi-bin/storageDevice.cgi?action=getDeviceAllInfo çıktısını alıp panel
   verisiyle karşılaştır. Ayrıştırma hatasında collector/dahua_monitor/
   parsing.py ve drivers/cgi.py'yi düzelt, pytest koştur, aynı dala push et.

KURALLAR: .env/devices.yaml commit edilmez; parola log'a yazılmaz; yanlış
parolayla TEKRAR DENEME (Dahua firmware IP kilitler); başka servis durdurma.
BİTTİ: panelde tüm NVR'lar erişilebilir, disk/RAID/saklama verisi akıyor.
```

## Sorularda başvuru

- Mimari ve alarm kuralları: `docs/02-mimari.md`
- Firmware farkları / API araştırması: `docs/01-arastirma.md`
- Kurulum ve sorun giderme: `README.md`
