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

CREATE INDEX IF NOT EXISTS nvr_metrics_ts ON nvr_metrics (nvr_id, ts DESC);
CREATE INDEX IF NOT EXISTS disk_metrics_ts ON disk_metrics (nvr_id, disk_name, ts DESC);
CREATE INDEX IF NOT EXISTS raid_metrics_ts ON raid_metrics (nvr_id, raid_name, ts DESC);

-- TimescaleDB (opsiyonel ama önerilir)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        PERFORM create_hypertable('nvr_metrics', 'ts', if_not_exists => TRUE, migrate_data => TRUE);
        PERFORM create_hypertable('disk_metrics', 'ts', if_not_exists => TRUE, migrate_data => TRUE);
        PERFORM create_hypertable('raid_metrics', 'ts', if_not_exists => TRUE, migrate_data => TRUE);
    END IF;
END $$;
