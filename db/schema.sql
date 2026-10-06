-- Dahua NVR izleme şeması (MVP). TimescaleDB varsa metrik tabloları
-- hypertable'a çevrilir; yoksa düz tablo olarak da çalışır.

CREATE TABLE IF NOT EXISTS nvr (
    id               SERIAL PRIMARY KEY,
    name             TEXT NOT NULL UNIQUE,
    host             TEXT NOT NULL,
    device_type      TEXT NOT NULL DEFAULT '',
    serial           TEXT NOT NULL DEFAULT '',
    software_version TEXT NOT NULL DEFAULT '',
    last_seen        TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS nvr_metrics (
    ts         TIMESTAMPTZ NOT NULL,
    nvr_id     INT NOT NULL REFERENCES nvr(id),
    reachable  BOOLEAN NOT NULL,
    latency_ms DOUBLE PRECISION,
    error      TEXT
);

CREATE TABLE IF NOT EXISTS disk_metrics (
    ts            TIMESTAMPTZ NOT NULL,
    nvr_id        INT NOT NULL REFERENCES nvr(id),
    disk_name     TEXT NOT NULL,
    state         TEXT NOT NULL,
    total_bytes   BIGINT NOT NULL DEFAULT 0,
    used_bytes    BIGINT NOT NULL DEFAULT 0,
    is_error      BOOLEAN NOT NULL DEFAULT false,
    health_ok     BOOLEAN,
    temperature_c INT,
    raw           JSONB
);

CREATE TABLE IF NOT EXISTS raid_metrics (
    ts          TIMESTAMPTZ NOT NULL,
    nvr_id      INT NOT NULL REFERENCES nvr(id),
    raid_name   TEXT NOT NULL,
    level       TEXT NOT NULL DEFAULT '',
    state       TEXT NOT NULL,
    rebuild_pct DOUBLE PRECISION,
    raw         JSONB
);

-- Panelden yönetilen cihaz envanteri. password_enc, SECRET_KEY (Fernet) ile
-- şifrelidir; düz metin parola veritabanına asla yazılmaz.
CREATE TABLE IF NOT EXISTS device_config (
    id                      SERIAL PRIMARY KEY,
    name                    TEXT NOT NULL UNIQUE,
    host                    TEXT NOT NULL,
    port                    INT,
    username                TEXT NOT NULL DEFAULT 'monitor',
    password_enc            TEXT NOT NULL,
    https                   BOOLEAN NOT NULL DEFAULT false,
    verify_tls              BOOLEAN NOT NULL DEFAULT false,
    poll_interval_s         INT NOT NULL DEFAULT 300,
    reachability_interval_s INT NOT NULL DEFAULT 60,
    overwrite_recording     BOOLEAN NOT NULL DEFAULT true,
    event_stream            BOOLEAN NOT NULL DEFAULT true,
    rpc2                    BOOLEAN NOT NULL DEFAULT false,
    login_watch             BOOLEAN NOT NULL DEFAULT false,
    login_allowlist         TEXT[]  NOT NULL DEFAULT '{}',
    retention_check         BOOLEAN NOT NULL DEFAULT true,
    max_channels            INT NOT NULL DEFAULT 32,
    first_channel           INT NOT NULL DEFAULT 1,
    min_retention_days      INT,
    enabled                 BOOLEAN NOT NULL DEFAULT true,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Günlük saklama derinliği: cihazdaki en eski kaydın tarihi.
-- oldest_recording cihazın KENDİ saatiyle döner (tz bilgisi yok).
CREATE TABLE IF NOT EXISTS retention_metrics (
    ts               TIMESTAMPTZ NOT NULL,
    nvr_id           INT NOT NULL REFERENCES nvr(id),
    oldest_recording TIMESTAMP,
    retention_days   DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS event (
    id       BIGSERIAL PRIMARY KEY,
    ts       TIMESTAMPTZ NOT NULL DEFAULT now(),
    nvr_id   INT REFERENCES nvr(id),
    source   TEXT NOT NULL,          -- poll | event-stream | snmp-trap
    code     TEXT NOT NULL,          -- StorageFailure, AuthFailed, ...
    severity TEXT NOT NULL,          -- info | warning | high | critical
    payload  JSONB,
    acked_by TEXT
);

-- Kamera (kanal) bağlantı durumu: cihaz başına anlık özet (hypertable değil,
-- upsert edilir). Kopan kameralar offline_list'te; alarm/olaylar event tablosunda.
CREATE TABLE IF NOT EXISTS camera_state (
    nvr_id       INT PRIMARY KEY REFERENCES nvr(id) ON DELETE CASCADE,
    ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
    total        INT NOT NULL DEFAULT 0,
    online       INT NOT NULL DEFAULT 0,
    offline      INT NOT NULL DEFAULT 0,
    offline_list JSONB NOT NULL DEFAULT '[]'   -- [{channel, name, state}]
);

-- Fiziksel disk SMART: disk başına anlık değerlendirilmiş sağlık (upsert). Öngörücü
-- bakım için kritik sayaçlar + tüm öznitelikler (attrs). Alarmlar event tablosunda.
CREATE TABLE IF NOT EXISTS disk_smart (
    nvr_id         INT NOT NULL REFERENCES nvr(id) ON DELETE CASCADE,
    disk_name      TEXT NOT NULL,
    ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
    health         TEXT,                      -- ok | warn | crit
    temperature_c  INT,
    power_on_hours INT,
    reallocated    INT,
    pending        INT,
    uncorrectable  INT,
    predict        BOOLEAN NOT NULL DEFAULT false,
    attrs          JSONB NOT NULL DEFAULT '[]',
    PRIMARY KEY (nvr_id, disk_name)
);

-- Fiziksel disk SMART GEÇMİŞİ (append-only hypertable): öngörücü bakımın temeli.
-- Kritik sayaçların ZAMAN İÇİNDEKİ eğilimi (reallocated/pending artış hızı,
-- sıcaklık trendi) buradan çıkar → "disk ~X gün içinde riskli" tahmini.
CREATE TABLE IF NOT EXISTS disk_smart_history (
    ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
    nvr_id         INT NOT NULL REFERENCES nvr(id) ON DELETE CASCADE,
    disk_name      TEXT NOT NULL,
    health         TEXT,
    temperature_c  INT,
    power_on_hours INT,
    reallocated    INT,
    pending        INT,
    uncorrectable  INT,
    predict        BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS disk_smart_history_ts
    ON disk_smart_history (nvr_id, disk_name, ts DESC);

-- Programatik erişim (genel API /api/v1) için anahtarlar. Anahtar sha256 karması
-- ile saklanır; düz metin tutulmaz (üretildiğinde bir kez gösterilir).
CREATE TABLE IF NOT EXISTS api_key (
    id         BIGSERIAL PRIMARY KEY,
    name       TEXT NOT NULL,
    key_hash   TEXT NOT NULL UNIQUE,
    enabled    BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used  TIMESTAMPTZ
);

-- Panel ayarları (anahtar-değer): AD yapılandırması vb. (sır içermez).
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS nvr_metrics_ts ON nvr_metrics (nvr_id, ts DESC);
CREATE INDEX IF NOT EXISTS disk_metrics_ts ON disk_metrics (nvr_id, disk_name, ts DESC);
CREATE INDEX IF NOT EXISTS raid_metrics_ts ON raid_metrics (nvr_id, raid_name, ts DESC);
CREATE INDEX IF NOT EXISTS retention_metrics_ts ON retention_metrics (nvr_id, ts DESC);

-- TimescaleDB (opsiyonel ama önerilir)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        PERFORM create_hypertable('nvr_metrics', 'ts', if_not_exists => TRUE, migrate_data => TRUE);
        PERFORM create_hypertable('disk_metrics', 'ts', if_not_exists => TRUE, migrate_data => TRUE);
        PERFORM create_hypertable('raid_metrics', 'ts', if_not_exists => TRUE, migrate_data => TRUE);
        PERFORM create_hypertable('retention_metrics', 'ts', if_not_exists => TRUE, migrate_data => TRUE);
        PERFORM create_hypertable('disk_smart_history', 'ts', if_not_exists => TRUE, migrate_data => TRUE);

        -- Saklama politikaları: metrik tablolarının sınırsız büyümesini önler.
        -- Yüksek frekanslı metrikler 90 gün; günlük saklama derinliği 365 gün.
        -- SMART geçmişi 365 gün: yavaş bozulma trendleri uzun pencere ister.
        PERFORM add_retention_policy('nvr_metrics', INTERVAL '90 days', if_not_exists => TRUE);
        PERFORM add_retention_policy('disk_metrics', INTERVAL '90 days', if_not_exists => TRUE);
        PERFORM add_retention_policy('raid_metrics', INTERVAL '90 days', if_not_exists => TRUE);
        PERFORM add_retention_policy('retention_metrics', INTERVAL '365 days', if_not_exists => TRUE);
        PERFORM add_retention_policy('disk_smart_history', INTERVAL '365 days', if_not_exists => TRUE);
    END IF;
END $$;
