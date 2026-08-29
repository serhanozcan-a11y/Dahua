"""PostgreSQL/TimescaleDB yazım katmanı. Şema: db/schema.sql"""

from __future__ import annotations

import json
from datetime import datetime

import asyncpg

from .models import PollResult


class Store:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, database_url: str) -> "Store":
        return cls(await asyncpg.create_pool(database_url, min_size=1, max_size=5))

    async def close(self) -> None:
        await self._pool.close()

    async def upsert_nvr(self, name: str, host: str) -> int:
        row = await self._pool.fetchrow(
            """
            INSERT INTO nvr (name, host) VALUES ($1, $2)
            ON CONFLICT (name) DO UPDATE SET host = EXCLUDED.host
            RETURNING id
            """,
            name,
            host,
        )
        return row["id"]

    async def write_event(
        self, device_name: str, source: str, code: str, severity: str, message: str
    ) -> None:
        await self._pool.execute(
            """
            INSERT INTO event (nvr_id, source, code, severity, payload)
            VALUES ((SELECT id FROM nvr WHERE name=$1), $2, $3, $4,
                    jsonb_build_object('message', $5::text))
            """,
            device_name,
            source,
            code,
            severity,
            message,
        )

    async def write_retention(
        self, nvr_id: int, oldest: datetime | None, retention_days: float | None
    ) -> None:
        await self._pool.execute(
            """
            INSERT INTO retention_metrics (ts, nvr_id, oldest_recording, retention_days)
            VALUES (now(), $1, $2, $3)
            """,
            nvr_id,
            oldest,
            retention_days,
        )

    async def write_poll(self, nvr_id: int, result: PollResult) -> None:
        async with self._pool.acquire() as conn, conn.transaction():
            if result.identity:
                await conn.execute(
                    """
                    UPDATE nvr SET device_type=$2, serial=$3, software_version=$4,
                                   last_seen = CASE WHEN $5 THEN now() ELSE last_seen END
                    WHERE id=$1
                    """,
                    nvr_id,
                    result.identity.device_type,
                    result.identity.serial,
                    result.identity.software_version,
                    result.reachable,
                )
            await conn.execute(
                """
                INSERT INTO nvr_metrics (ts, nvr_id, reachable, latency_ms, error)
                VALUES (now(), $1, $2, $3, $4)
                """,
                nvr_id,
                result.reachable,
                result.latency_ms,
                result.error,
            )
            for d in result.disks:
                await conn.execute(
                    """
                    INSERT INTO disk_metrics
                        (ts, nvr_id, disk_name, state, total_bytes, used_bytes,
                         is_error, health_ok, temperature_c, raw)
                    VALUES (now(), $1, $2, $3, $4, $5, $6, $7, $8, $9)
                    """,
                    nvr_id,
                    d.name,
                    d.state.value,
                    d.total_bytes,
                    d.used_bytes,
                    d.is_error,
                    d.health_ok,
                    d.temperature_c,
                    json.dumps(d.raw, default=str),
                )
            for r in result.raids:
                await conn.execute(
                    """
                    INSERT INTO raid_metrics
                        (ts, nvr_id, raid_name, level, state, rebuild_pct, raw)
                    VALUES (now(), $1, $2, $3, $4, $5, $6)
                    """,
                    nvr_id,
                    r.name,
                    r.level,
                    r.state.value,
                    r.rebuild_pct,
                    json.dumps(r.raw, default=str),
                )
