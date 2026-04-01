import asyncio
import io
import json
import logging
import os
import random
import re
import shutil
import sqlite3
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

# override=True avoids stale system env values shadowing .env.
load_dotenv(override=True)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
VOICE_CHANNEL_ID_RAW = os.getenv("VOICE_CHANNEL_ID", "").strip()
OWNER_USER_ID_RAW = os.getenv("OWNER_USER_ID", "").strip()
DB_PATH = os.getenv("DB_PATH", "bot_data.sqlite3").strip()
USE_PRIVILEGED_INTENTS_RAW = os.getenv("USE_PRIVILEGED_INTENTS", "1").strip().lower()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("voice-sitter")

URL_REGEX = re.compile(r"https?://[^\s]+", re.IGNORECASE)

DEFAULTS_INT: dict[str, int] = {
    "raid_join_limit": 8,
    "raid_channel_create_limit": 5,
    "raid_role_create_limit": 3,
    "raid_mention_limit": 8,
    "automod_spam_messages": 6,
    "automod_spam_window_sec": 8,
    "automod_caps_percent": 70,
    "automod_caps_min_len": 12,
    "automod_block_links": 1,
    "warn_timeout_points": 3,
    "warn_voice_ban_points": 5,
    "warn_ban_points": 7,
    "warn_timeout_minutes": 60,
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utcnow().isoformat()


def _parse_required_int(value: str, env_name: str) -> int:
    if not value:
        raise ValueError(f"Переменная {env_name} не задана.")
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{env_name} должен быть числом.") from exc


def _parse_owner_ids(value: str) -> set[int]:
    if not value:
        return set()

    result: set[int] = set()
    for item in [part.strip() for part in value.split(",") if part.strip()]:
        try:
            result.add(int(item))
        except ValueError as exc:
            raise ValueError("OWNER_USER_ID должен быть числом или списком ID через запятую.") from exc
    return result


VOICE_CHANNEL_ID = _parse_required_int(VOICE_CHANNEL_ID_RAW, "VOICE_CHANNEL_ID")
OWNER_USER_IDS = _parse_owner_ids(OWNER_USER_ID_RAW)
USE_PRIVILEGED_INTENTS = USE_PRIVILEGED_INTENTS_RAW in {"1", "true", "yes", "on"}


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        script = """
        CREATE TABLE IF NOT EXISTS settings (
            guild_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (guild_id, key)
        );

        CREATE TABLE IF NOT EXISTS cases (
            case_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            moderator_id INTEGER,
            target_id INTEGER,
            reason TEXT,
            created_at TEXT NOT NULL,
            metadata TEXT,
            reverted INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS warns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            moderator_id INTEGER,
            points INTEGER NOT NULL,
            reason TEXT,
            created_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS warn_state (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            level INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS automod_offenses (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            last_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            next_run_at INTEGER NOT NULL,
            interval_seconds INTEGER,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_by INTEGER,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS temp_rooms (
            channel_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            owner_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS economy_profiles (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            balance INTEGER NOT NULL DEFAULT 0,
            daily_last TEXT,
            daily_streak INTEGER NOT NULL DEFAULT 0,
            total_duels INTEGER NOT NULL DEFAULT 0,
            duel_wins INTEGER NOT NULL DEFAULT 0,
            rps_wins INTEGER NOT NULL DEFAULT 0,
            slots_wins INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            reporter_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS shop_roles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            role_id INTEGER NOT NULL,
            price INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(guild_id, role_id)
        );

        CREATE TABLE IF NOT EXISTS marriages (
            guild_id INTEGER NOT NULL,
            user1_id INTEGER NOT NULL,
            user2_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, user1_id),
            UNIQUE(guild_id, user2_id)
        );

        CREATE TABLE IF NOT EXISTS personal_roles (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        );
        """
        self.conn.executescript(script)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def get_setting(self, guild_id: int, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute(
            "SELECT value FROM settings WHERE guild_id = ? AND key = ?",
            (guild_id, key),
        ).fetchone()
        if row is None:
            return default
        return str(row["value"])

    def set_setting(self, guild_id: int, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO settings(guild_id, key, value)
            VALUES(?, ?, ?)
            ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value
            """,
            (guild_id, key, value),
        )
        self.conn.commit()

    def get_int_setting(self, guild_id: int, key: str) -> int:
        value = self.get_setting(guild_id, key)
        if value is None:
            return DEFAULTS_INT[key]
        try:
            return int(value)
        except ValueError:
            return DEFAULTS_INT[key]

    def get_csv_setting(self, guild_id: int, key: str) -> list[str]:
        raw = self.get_setting(guild_id, key, "") or ""
        return [item.strip() for item in raw.split(",") if item.strip()]

    def set_csv_setting(self, guild_id: int, key: str, values: list[str]) -> None:
        normalized = ",".join(values)
        self.set_setting(guild_id, key, normalized)

    def get_id_set_setting(self, guild_id: int, key: str) -> set[int]:
        values = self.get_csv_setting(guild_id, key)
        result: set[int] = set()
        for value in values:
            try:
                result.add(int(value))
            except ValueError:
                continue
        return result

    def set_id_set_setting(self, guild_id: int, key: str, values: set[int]) -> None:
        normalized = ",".join(str(v) for v in sorted(values))
        self.set_setting(guild_id, key, normalized)

    def add_case(
        self,
        guild_id: int,
        action: str,
        moderator_id: Optional[int],
        target_id: Optional[int],
        reason: str,
        metadata: Optional[dict],
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO cases(guild_id, action, moderator_id, target_id, reason, created_at, metadata)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                action,
                moderator_id,
                target_id,
                reason,
                iso_now(),
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def get_case(self, guild_id: int, case_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM cases WHERE guild_id = ? AND case_id = ?",
            (guild_id, case_id),
        ).fetchone()

    def mark_case_reverted(self, guild_id: int, case_id: int) -> None:
        self.conn.execute(
            "UPDATE cases SET reverted = 1 WHERE guild_id = ? AND case_id = ?",
            (guild_id, case_id),
        )
        self.conn.commit()

    def add_warn(
        self,
        guild_id: int,
        user_id: int,
        moderator_id: Optional[int],
        points: int,
        reason: str,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO warns(guild_id, user_id, moderator_id, points, reason, created_at, active)
            VALUES(?, ?, ?, ?, ?, ?, 1)
            """,
            (guild_id, user_id, moderator_id, points, reason, iso_now()),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def get_warn(self, guild_id: int, warn_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM warns WHERE guild_id = ? AND id = ?",
            (guild_id, warn_id),
        ).fetchone()

    def deactivate_warn(self, guild_id: int, warn_id: int) -> None:
        self.conn.execute(
            "UPDATE warns SET active = 0 WHERE guild_id = ? AND id = ?",
            (guild_id, warn_id),
        )
        self.conn.commit()

    def get_warn_total(self, guild_id: int, user_id: int) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(points), 0) AS total FROM warns WHERE guild_id = ? AND user_id = ? AND active = 1",
            (guild_id, user_id),
        ).fetchone()
        return int(row["total"]) if row else 0

    def list_warns(self, guild_id: int, user_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM warns WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT 20",
                (guild_id, user_id),
            ).fetchall()
        )

    def get_warn_level(self, guild_id: int, user_id: int) -> int:
        row = self.conn.execute(
            "SELECT level FROM warn_state WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        return int(row["level"]) if row else 0

    def set_warn_level(self, guild_id: int, user_id: int, level: int) -> None:
        self.conn.execute(
            """
            INSERT INTO warn_state(guild_id, user_id, level)
            VALUES(?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET level=excluded.level
            """,
            (guild_id, user_id, level),
        )
        self.conn.commit()

    def increment_offense(self, guild_id: int, user_id: int) -> int:
        row = self.conn.execute(
            "SELECT count FROM automod_offenses WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        new_count = (int(row["count"]) + 1) if row else 1
        self.conn.execute(
            """
            INSERT INTO automod_offenses(guild_id, user_id, count, last_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id)
            DO UPDATE SET count=excluded.count, last_at=excluded.last_at
            """,
            (guild_id, user_id, new_count, iso_now()),
        )
        self.conn.commit()
        return new_count

    def add_schedule(
        self,
        guild_id: int,
        channel_id: int,
        content: str,
        next_run_at: int,
        interval_seconds: Optional[int],
        created_by: int,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO schedules(guild_id, channel_id, content, next_run_at, interval_seconds, enabled, created_by, created_at)
            VALUES(?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (guild_id, channel_id, content, next_run_at, interval_seconds, created_by, iso_now()),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def get_due_schedules(self, now_ts: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM schedules WHERE enabled = 1 AND next_run_at <= ?",
                (now_ts,),
            ).fetchall()
        )

    def list_schedules(self, guild_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM schedules WHERE guild_id = ? AND enabled = 1 ORDER BY next_run_at ASC LIMIT 25",
                (guild_id,),
            ).fetchall()
        )

    def get_schedule(self, guild_id: int, schedule_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM schedules WHERE guild_id = ? AND id = ?",
            (guild_id, schedule_id),
        ).fetchone()

    def remove_schedule(self, guild_id: int, schedule_id: int) -> None:
        self.conn.execute(
            "UPDATE schedules SET enabled = 0 WHERE guild_id = ? AND id = ?",
            (guild_id, schedule_id),
        )
        self.conn.commit()

    def mark_schedule_ran(self, schedule_id: int, next_run_at: Optional[int]) -> None:
        if next_run_at is None:
            self.conn.execute("UPDATE schedules SET enabled = 0 WHERE id = ?", (schedule_id,))
        else:
            self.conn.execute(
                "UPDATE schedules SET next_run_at = ? WHERE id = ?",
                (next_run_at, schedule_id),
            )
        self.conn.commit()

    def add_temp_room(self, guild_id: int, channel_id: int, owner_id: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO temp_rooms(channel_id, guild_id, owner_id, created_at) VALUES(?, ?, ?, ?)",
            (channel_id, guild_id, owner_id, iso_now()),
        )
        self.conn.commit()

    def remove_temp_room(self, channel_id: int) -> None:
        self.conn.execute("DELETE FROM temp_rooms WHERE channel_id = ?", (channel_id,))
        self.conn.commit()

    def get_temp_room_owner(self, channel_id: int) -> Optional[int]:
        row = self.conn.execute(
            "SELECT owner_id FROM temp_rooms WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()
        return int(row["owner_id"]) if row else None

    def ensure_profile(self, guild_id: int, user_id: int) -> None:
        self.conn.execute(
            """
            INSERT INTO economy_profiles(guild_id, user_id, balance, daily_last, daily_streak, total_duels, duel_wins, rps_wins, slots_wins, created_at)
            VALUES(?, ?, 0, NULL, 0, 0, 0, 0, 0, ?)
            ON CONFLICT(guild_id, user_id) DO NOTHING
            """,
            (guild_id, user_id, iso_now()),
        )
        self.conn.commit()

    def get_profile(self, guild_id: int, user_id: int) -> sqlite3.Row:
        self.ensure_profile(guild_id, user_id)
        row = self.conn.execute(
            "SELECT * FROM economy_profiles WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("Не удалось получить профиль пользователя")
        return row

    def get_balance(self, guild_id: int, user_id: int) -> int:
        row = self.get_profile(guild_id, user_id)
        return int(row["balance"])

    def add_balance(self, guild_id: int, user_id: int, amount: int, min_balance: int = 0) -> int:
        profile = self.get_profile(guild_id, user_id)
        new_balance = int(profile["balance"]) + amount
        if new_balance < min_balance:
            raise ValueError("Недостаточно средств")
        self.conn.execute(
            "UPDATE economy_profiles SET balance = ? WHERE guild_id = ? AND user_id = ?",
            (new_balance, guild_id, user_id),
        )
        self.conn.commit()
        return new_balance

    def transfer_balance(self, guild_id: int, from_user_id: int, to_user_id: int, amount: int) -> tuple[int, int]:
        if amount <= 0:
            raise ValueError("Сумма должна быть больше 0")
        if from_user_id == to_user_id:
            raise ValueError("Нельзя переводить самому себе")

        from_balance = self.get_balance(guild_id, from_user_id)
        if from_balance < amount:
            raise ValueError("Недостаточно средств")

        new_from = self.add_balance(guild_id, from_user_id, -amount, min_balance=0)
        new_to = self.add_balance(guild_id, to_user_id, amount, min_balance=0)
        return new_from, new_to

    def set_daily_claim(self, guild_id: int, user_id: int, claimed_at: datetime, streak: int) -> None:
        self.get_profile(guild_id, user_id)
        self.conn.execute(
            "UPDATE economy_profiles SET daily_last = ?, daily_streak = ? WHERE guild_id = ? AND user_id = ?",
            (claimed_at.isoformat(), streak, guild_id, user_id),
        )
        self.conn.commit()

    def increment_profile_counter(self, guild_id: int, user_id: int, field: str, amount: int = 1) -> None:
        allowed = {"total_duels", "duel_wins", "rps_wins", "slots_wins"}
        if field not in allowed:
            raise ValueError("Недопустимый счетчик профиля")
        self.get_profile(guild_id, user_id)
        self.conn.execute(
            f"UPDATE economy_profiles SET {field} = {field} + ? WHERE guild_id = ? AND user_id = ?",
            (amount, guild_id, user_id),
        )
        self.conn.commit()

    def add_report(self, guild_id: int, reporter_id: int, target_id: int, reason: str) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO reports(guild_id, reporter_id, target_id, reason, status, created_at)
            VALUES(?, ?, ?, ?, 'open', ?)
            """,
            (guild_id, reporter_id, target_id, reason, iso_now()),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def count_reports_for_target(self, guild_id: int, target_id: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS count FROM reports WHERE guild_id = ? AND target_id = ?",
            (guild_id, target_id),
        ).fetchone()
        return int(row["count"]) if row else 0

    def upsert_shop_role(self, guild_id: int, role_id: int, price: int) -> None:
        self.conn.execute(
            """
            INSERT INTO shop_roles(guild_id, role_id, price, created_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(guild_id, role_id) DO UPDATE SET price = excluded.price
            """,
            (guild_id, role_id, max(1, price), iso_now()),
        )
        self.conn.commit()

    def remove_shop_role(self, guild_id: int, role_id: int) -> bool:
        cursor = self.conn.execute(
            "DELETE FROM shop_roles WHERE guild_id = ? AND role_id = ?",
            (guild_id, role_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def get_shop_role(self, guild_id: int, role_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM shop_roles WHERE guild_id = ? AND role_id = ?",
            (guild_id, role_id),
        ).fetchone()

    def list_shop_roles(self, guild_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM shop_roles WHERE guild_id = ? ORDER BY price ASC, role_id ASC",
                (guild_id,),
            ).fetchall()
        )

    def get_marriage(self, guild_id: int, user_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM marriages WHERE guild_id = ? AND (user1_id = ? OR user2_id = ?)",
            (guild_id, user_id, user_id),
        ).fetchone()

    def set_marriage(self, guild_id: int, user_a_id: int, user_b_id: int) -> bool:
        if user_a_id == user_b_id:
            return False
        if self.get_marriage(guild_id, user_a_id) is not None or self.get_marriage(guild_id, user_b_id) is not None:
            return False

        first, second = sorted((user_a_id, user_b_id))
        self.conn.execute(
            "INSERT INTO marriages(guild_id, user1_id, user2_id, created_at) VALUES(?, ?, ?, ?)",
            (guild_id, first, second, iso_now()),
        )
        self.conn.commit()
        return True

    def clear_marriage(self, guild_id: int, user_id: int) -> bool:
        cursor = self.conn.execute(
            "DELETE FROM marriages WHERE guild_id = ? AND (user1_id = ? OR user2_id = ?)",
            (guild_id, user_id, user_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def get_personal_role_id(self, guild_id: int, user_id: int) -> Optional[int]:
        row = self.conn.execute(
            "SELECT role_id FROM personal_roles WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        return int(row["role_id"]) if row else None

    def set_personal_role(self, guild_id: int, user_id: int, role_id: int) -> None:
        self.conn.execute(
            """
            INSERT INTO personal_roles(guild_id, user_id, role_id, created_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET role_id = excluded.role_id
            """,
            (guild_id, user_id, role_id, iso_now()),
        )
        self.conn.commit()

    def clear_personal_role(self, guild_id: int, user_id: int) -> None:
        self.conn.execute(
            "DELETE FROM personal_roles WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        self.conn.commit()

    def list_top_profiles(self, guild_id: int, limit: int = 10) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT * FROM economy_profiles
                WHERE guild_id = ?
                ORDER BY balance DESC, user_id ASC
                LIMIT ?
                """,
                (guild_id, max(1, limit)),
            ).fetchall()
        )

    def backup_guild_data(self, guild_id: int) -> dict:
        data: dict[str, list[dict]] = {}
        tables = [
            "settings",
            "cases",
            "warns",
            "warn_state",
            "automod_offenses",
            "schedules",
            "temp_rooms",
            "economy_profiles",
            "reports",
            "shop_roles",
            "marriages",
            "personal_roles",
        ]
        for table in tables:
            rows = self.conn.execute(
                f"SELECT * FROM {table} WHERE guild_id = ?",
                (guild_id,),
            ).fetchall()
            data[table] = [dict(row) for row in rows]
        return data

    def restore_guild_data(self, guild_id: int, data: dict) -> None:
        tables = [
            "settings",
            "cases",
            "warns",
            "warn_state",
            "automod_offenses",
            "schedules",
            "temp_rooms",
            "economy_profiles",
            "reports",
            "shop_roles",
            "marriages",
            "personal_roles",
        ]

        for table in tables:
            self.conn.execute(f"DELETE FROM {table} WHERE guild_id = ?", (guild_id,))

        for row in data.get("settings", []):
            self.conn.execute(
                "INSERT INTO settings(guild_id, key, value) VALUES(?, ?, ?)",
                (guild_id, row["key"], row["value"]),
            )

        for row in data.get("cases", []):
            self.conn.execute(
                """
                INSERT INTO cases(case_id, guild_id, action, moderator_id, target_id, reason, created_at, metadata, reverted)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.get("case_id"),
                    guild_id,
                    row.get("action"),
                    row.get("moderator_id"),
                    row.get("target_id"),
                    row.get("reason"),
                    row.get("created_at"),
                    row.get("metadata"),
                    row.get("reverted", 0),
                ),
            )

        for row in data.get("warns", []):
            self.conn.execute(
                """
                INSERT INTO warns(id, guild_id, user_id, moderator_id, points, reason, created_at, active)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.get("id"),
                    guild_id,
                    row.get("user_id"),
                    row.get("moderator_id"),
                    row.get("points"),
                    row.get("reason"),
                    row.get("created_at"),
                    row.get("active", 1),
                ),
            )

        for row in data.get("warn_state", []):
            self.conn.execute(
                "INSERT INTO warn_state(guild_id, user_id, level) VALUES(?, ?, ?)",
                (guild_id, row.get("user_id"), row.get("level", 0)),
            )

        for row in data.get("automod_offenses", []):
            self.conn.execute(
                "INSERT INTO automod_offenses(guild_id, user_id, count, last_at) VALUES(?, ?, ?, ?)",
                (guild_id, row.get("user_id"), row.get("count", 0), row.get("last_at", iso_now())),
            )

        for row in data.get("schedules", []):
            self.conn.execute(
                """
                INSERT INTO schedules(id, guild_id, channel_id, content, next_run_at, interval_seconds, enabled, created_by, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.get("id"),
                    guild_id,
                    row.get("channel_id"),
                    row.get("content"),
                    row.get("next_run_at"),
                    row.get("interval_seconds"),
                    row.get("enabled", 1),
                    row.get("created_by"),
                    row.get("created_at", iso_now()),
                ),
            )

        for row in data.get("temp_rooms", []):
            self.conn.execute(
                "INSERT INTO temp_rooms(channel_id, guild_id, owner_id, created_at) VALUES(?, ?, ?, ?)",
                (
                    row.get("channel_id"),
                    guild_id,
                    row.get("owner_id"),
                    row.get("created_at", iso_now()),
                ),
            )

        for row in data.get("economy_profiles", []):
            self.conn.execute(
                """
                INSERT INTO economy_profiles(guild_id, user_id, balance, daily_last, daily_streak, total_duels, duel_wins, rps_wins, slots_wins, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    row.get("user_id"),
                    row.get("balance", 0),
                    row.get("daily_last"),
                    row.get("daily_streak", 0),
                    row.get("total_duels", 0),
                    row.get("duel_wins", 0),
                    row.get("rps_wins", 0),
                    row.get("slots_wins", 0),
                    row.get("created_at", iso_now()),
                ),
            )

        for row in data.get("reports", []):
            self.conn.execute(
                """
                INSERT INTO reports(id, guild_id, reporter_id, target_id, reason, status, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.get("id"),
                    guild_id,
                    row.get("reporter_id"),
                    row.get("target_id"),
                    row.get("reason", "Без причины"),
                    row.get("status", "open"),
                    row.get("created_at", iso_now()),
                ),
            )

        for row in data.get("shop_roles", []):
            self.conn.execute(
                "INSERT INTO shop_roles(id, guild_id, role_id, price, created_at) VALUES(?, ?, ?, ?, ?)",
                (
                    row.get("id"),
                    guild_id,
                    row.get("role_id"),
                    row.get("price", 1),
                    row.get("created_at", iso_now()),
                ),
            )

        for row in data.get("marriages", []):
            self.conn.execute(
                "INSERT INTO marriages(guild_id, user1_id, user2_id, created_at) VALUES(?, ?, ?, ?)",
                (
                    guild_id,
                    row.get("user1_id"),
                    row.get("user2_id"),
                    row.get("created_at", iso_now()),
                ),
            )

        for row in data.get("personal_roles", []):
            self.conn.execute(
                "INSERT INTO personal_roles(guild_id, user_id, role_id, created_at) VALUES(?, ?, ?, ?)",
                (
                    guild_id,
                    row.get("user_id"),
                    row.get("role_id"),
                    row.get("created_at", iso_now()),
                ),
            )

        self.conn.commit()


store = Store(DB_PATH)

intents = discord.Intents.none()
intents.guilds = True
intents.voice_states = True
intents.messages = True
intents.message_content = USE_PRIVILEGED_INTENTS
intents.members = USE_PRIVILEGED_INTENTS

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

connect_lock = asyncio.Lock()
commands_synced = False
views_registered = False

join_windows: dict[int, deque[datetime]] = defaultdict(deque)
channel_create_windows: dict[int, deque[datetime]] = defaultdict(deque)
role_create_windows: dict[int, deque[datetime]] = defaultdict(deque)
message_windows: dict[tuple[int, int], deque[datetime]] = defaultdict(deque)

OWNER_PANEL_MOD_ACTIONS = {"ban", "kick", "timeout", "voice_ban", "warn", "unban", "untimeout", "voice_unban", "unwarn"}
OWNER_PANEL_FLOW_TTL_SECONDS = 180
owner_panel_pending_actions: dict[tuple[int, int], dict[str, object]] = {}
ECONOMY_DAILY_COOLDOWN_HOURS = 24
ECONOMY_DAILY_BASE_REWARD = 100
ECONOMY_DAILY_STREAK_BONUS = 20
ECONOMY_DAILY_STREAK_CAP = 7


def _is_owner_override(user_id: int) -> bool:
    return user_id in OWNER_USER_IDS


def _is_admin_member(member: discord.Member) -> bool:
    return member.guild_permissions.administrator


def _can_use_admin_actions(interaction: discord.Interaction) -> bool:
    if _is_owner_override(interaction.user.id):
        return True
    if isinstance(interaction.user, discord.Member):
        return _is_admin_member(interaction.user)
    return False


async def ensure_admin(interaction: discord.Interaction) -> bool:
    if _can_use_admin_actions(interaction):
        return True
    await interaction.response.send_message("Нужны права администратора сервера.", ephemeral=True)
    return False


def can_bot_moderate(target: discord.Member) -> tuple[bool, str]:
    guild = target.guild
    me = guild.me
    if me is None:
        return False, "Не удалось получить роль бота."
    if target.id == guild.owner_id:
        return False, "Нельзя модерировать владельца сервера."
    if target.top_role >= me.top_role:
        return False, "Роль бота ниже или равна роли цели."
    return True, ""


def can_moderate_target(moderator: discord.Member, target: discord.Member) -> tuple[bool, str]:
    if moderator.id == target.id:
        return False, "Нельзя модерировать самого себя."

    guild = moderator.guild
    if target.id == guild.owner_id:
        return False, "Нельзя модерировать владельца сервера."

    if moderator.id != guild.owner_id and target.top_role >= moderator.top_role:
        return False, "У цели роль выше или равна твоей."

    can_bot, reason = can_bot_moderate(target)
    if not can_bot:
        return False, reason

    return True, ""


def get_id_setting(guild_id: int, key: str) -> Optional[int]:
    value = store.get_setting(guild_id, key)
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def get_alert_channel_id(guild_id: int) -> Optional[int]:
    return get_id_setting(guild_id, "alert_channel_id") or get_id_setting(guild_id, "modlog_channel_id")


async def send_alert(guild: discord.Guild, message: str) -> None:
    channel_id = get_alert_channel_id(guild.id)
    if not channel_id:
        return

    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except discord.HTTPException:
            return

    if isinstance(channel, (discord.TextChannel, discord.Thread)):
        try:
            await channel.send(f"🚨 {message}")
        except discord.HTTPException:
            pass


async def record_case(
    guild: discord.Guild,
    action: str,
    moderator_id: Optional[int],
    target_id: Optional[int],
    reason: str,
    metadata: Optional[dict] = None,
) -> int:
    case_id = store.add_case(guild.id, action, moderator_id, target_id, reason, metadata)

    modlog_channel_id = get_id_setting(guild.id, "modlog_channel_id")
    if not modlog_channel_id:
        return case_id

    channel = guild.get_channel(modlog_channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(modlog_channel_id)
        except discord.HTTPException:
            return case_id

    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return case_id

    embed = discord.Embed(
        title=f"Case #{case_id}: {action}",
        color=discord.Color.orange(),
        timestamp=utcnow(),
    )
    embed.add_field(name="Moderator", value=f"<@{moderator_id}>" if moderator_id else "system", inline=True)
    embed.add_field(name="Target", value=f"<@{target_id}>" if target_id else "-", inline=True)
    embed.add_field(name="Reason", value=reason or "Без причины", inline=False)

    if metadata:
        pretty = json.dumps(metadata, ensure_ascii=False)[:900]
        embed.add_field(name="Metadata", value=f"```json\n{pretty}\n```", inline=False)

    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        pass

    return case_id


async def set_voice_ban_for_member(guild: discord.Guild, target: discord.Member, enabled: bool, reason: str) -> int:
    changed_channels = 0
    for channel in guild.channels:
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            continue

        overwrite = channel.overwrites_for(target)

        if enabled:
            overwrite.connect = False
            overwrite.speak = False
            overwrite.stream = False
            overwrite.use_soundboard = False
        else:
            overwrite.connect = None
            overwrite.speak = None
            overwrite.stream = None
            overwrite.use_soundboard = None

        if overwrite.is_empty():
            await channel.set_permissions(target, overwrite=None, reason=reason)
        else:
            await channel.set_permissions(target, overwrite=overwrite, reason=reason)

        changed_channels += 1

    return changed_channels


async def apply_warn_threshold_action(
    guild: discord.Guild,
    target: discord.Member,
    total_points: int,
    moderator_id: int,
    reason: str,
) -> list[str]:
    timeout_points = store.get_int_setting(guild.id, "warn_timeout_points")
    voice_ban_points = store.get_int_setting(guild.id, "warn_voice_ban_points")
    ban_points = store.get_int_setting(guild.id, "warn_ban_points")
    timeout_minutes = store.get_int_setting(guild.id, "warn_timeout_minutes")

    old_level = store.get_warn_level(guild.id, target.id)

    new_level = 0
    if total_points >= ban_points:
        new_level = 3
    elif total_points >= voice_ban_points:
        new_level = 2
    elif total_points >= timeout_points:
        new_level = 1

    actions: list[str] = []
    if new_level <= old_level:
        return actions

    if new_level == 1:
        until = utcnow() + timedelta(minutes=timeout_minutes)
        await target.edit(timed_out_until=until, reason=f"Warn threshold: {reason}")
        case_id = await record_case(
            guild,
            "warn_auto_timeout",
            moderator_id,
            target.id,
            f"Auto timeout ({timeout_minutes} мин) по warn threshold",
            {"total_points": total_points},
        )
        actions.append(f"timeout ({timeout_minutes} мин, case #{case_id})")

    if new_level == 2:
        await set_voice_ban_for_member(guild, target, enabled=True, reason=f"Warn threshold: {reason}")
        if target.voice is not None:
            await target.move_to(None, reason="Warn threshold voice ban")
        case_id = await record_case(
            guild,
            "warn_auto_voice_ban",
            moderator_id,
            target.id,
            "Auto voice ban по warn threshold",
            {"total_points": total_points},
        )
        actions.append(f"voice_ban (case #{case_id})")

    if new_level == 3:
        await guild.ban(target, reason=f"Warn threshold: {reason}", delete_message_days=0)
        case_id = await record_case(
            guild,
            "warn_auto_ban",
            moderator_id,
            target.id,
            "Auto ban по warn threshold",
            {"total_points": total_points},
        )
        actions.append(f"ban (case #{case_id})")

    store.set_warn_level(guild.id, target.id, new_level)
    return actions


async def fetch_audit_executor(
    guild: discord.Guild,
    action: discord.AuditLogAction,
    target_id: int,
) -> Optional[discord.Member]:
    try:
        async for entry in guild.audit_logs(limit=8, action=action):
            if entry.target is None:
                continue
            if getattr(entry.target, "id", None) != target_id:
                continue
            age = (utcnow() - entry.created_at).total_seconds()
            if age > 20:
                continue
            if entry.user is None:
                return None
            return guild.get_member(entry.user.id)
    except discord.HTTPException:
        return None
    return None


async def apply_auto_timeout(
    guild: discord.Guild,
    target: discord.Member,
    minutes: int,
    reason: str,
    action_name: str,
    metadata: Optional[dict] = None,
) -> bool:
    ok, why = can_bot_moderate(target)
    if not ok:
        await send_alert(guild, f"Не удалось авто-наказать <@{target.id}>: {why}")
        return False

    try:
        await target.edit(timed_out_until=utcnow() + timedelta(minutes=minutes), reason=reason)
    except discord.HTTPException:
        return False

    await record_case(guild, action_name, None, target.id, reason, metadata)
    return True


async def maybe_handle_mass_mentions(message: discord.Message) -> bool:
    if message.guild is None or not isinstance(message.author, discord.Member):
        return False

    guild = message.guild
    if message.author.bot or _is_owner_override(message.author.id) or _is_admin_member(message.author):
        return False

    mention_limit = store.get_int_setting(guild.id, "raid_mention_limit")
    mention_count = len(message.mentions) + len(message.role_mentions)
    if message.mention_everyone:
        mention_count += 4

    if mention_count < mention_limit:
        return False

    try:
        await message.delete()
    except discord.HTTPException:
        pass

    await apply_auto_timeout(
        guild,
        message.author,
        minutes=20,
        reason="Anti-raid: mass mention",
        action_name="anti_raid_mass_mention",
        metadata={"mentions": mention_count},
    )
    await send_alert(
        guild,
        f"Mass mention от <@{message.author.id}>: {mention_count} упоминаний. Выдан timeout.",
    )
    return True


def parse_domain(url: str) -> str:
    parsed = urlparse(url)
    return (parsed.netloc or "").lower().replace("www.", "")


def is_automod_exempt(member: discord.Member, channel_id: int) -> bool:
    if _is_owner_override(member.id) or _is_admin_member(member):
        return True

    exempt_channels = store.get_id_set_setting(member.guild.id, "automod_exempt_channel_ids")
    if channel_id in exempt_channels:
        return True

    exempt_roles = store.get_id_set_setting(member.guild.id, "automod_exempt_role_ids")
    member_role_ids = {role.id for role in member.roles}
    if exempt_roles.intersection(member_role_ids):
        return True

    return False


async def handle_automod_violation(
    message: discord.Message,
    violation: str,
    details: str,
) -> None:
    if message.guild is None or not isinstance(message.author, discord.Member):
        return

    guild = message.guild
    member = message.author

    try:
        await message.delete()
    except discord.HTTPException:
        pass

    offense = store.increment_offense(guild.id, member.id)
    action = "warn"

    if offense == 1:
        warn_id = store.add_warn(guild.id, member.id, None, 1, f"AutoMod: {violation}")
        case_id = await record_case(
            guild,
            "automod_warn",
            None,
            member.id,
            f"AutoMod violation: {violation}",
            {"details": details, "offense": offense, "warn_id": warn_id},
        )
        await send_alert(guild, f"AutoMod warn: <@{member.id}> | {violation} | case #{case_id}")
    elif offense == 2:
        action = "timeout"
        await apply_auto_timeout(
            guild,
            member,
            minutes=10,
            reason=f"AutoMod ({violation})",
            action_name="automod_timeout",
            metadata={"details": details, "offense": offense},
        )
        await send_alert(guild, f"AutoMod timeout: <@{member.id}> | {violation}")
    elif offense == 3:
        action = "voice_ban"
        ok, why = can_bot_moderate(member)
        if ok:
            await set_voice_ban_for_member(guild, member, True, reason=f"AutoMod ({violation})")
            if member.voice is not None:
                await member.move_to(None, reason="AutoMod voice ban")
            case_id = await record_case(
                guild,
                "automod_voice_ban",
                None,
                member.id,
                f"AutoMod ({violation})",
                {"details": details, "offense": offense},
            )
            await send_alert(guild, f"AutoMod voice_ban: <@{member.id}> | case #{case_id}")
        else:
            await send_alert(guild, f"AutoMod не смог выдать voice_ban для <@{member.id}>: {why}")
    else:
        action = "ban"
        ok, why = can_bot_moderate(member)
        if ok:
            try:
                await guild.ban(member, reason=f"AutoMod ({violation})", delete_message_days=0)
                case_id = await record_case(
                    guild,
                    "automod_ban",
                    None,
                    member.id,
                    f"AutoMod ({violation})",
                    {"details": details, "offense": offense},
                )
                await send_alert(guild, f"AutoMod ban: <@{member.id}> | case #{case_id}")
            except discord.HTTPException:
                await send_alert(guild, f"AutoMod не смог забанить <@{member.id}>.")
        else:
            await send_alert(guild, f"AutoMod не смог выдать ban для <@{member.id}>: {why}")

    try:
        await message.channel.send(
            f"<@{member.id}> AutoMod: {violation} ({action}).",
            delete_after=8,
        )
    except discord.HTTPException:
        pass


async def check_automod(message: discord.Message) -> None:
    if message.guild is None or not isinstance(message.author, discord.Member):
        return

    member = message.author
    if member.bot:
        return

    if is_automod_exempt(member, message.channel.id):
        return

    content = message.content.strip()
    if not content:
        return

    guild_id = message.guild.id
    now = utcnow()

    # spam detection
    key = (guild_id, member.id)
    spam_window = message_windows[key]
    spam_window_sec = store.get_int_setting(guild_id, "automod_spam_window_sec")
    spam_messages = store.get_int_setting(guild_id, "automod_spam_messages")

    spam_window.append(now)
    cutoff = now - timedelta(seconds=spam_window_sec)
    while spam_window and spam_window[0] < cutoff:
        spam_window.popleft()

    if len(spam_window) >= spam_messages:
        await handle_automod_violation(message, "spam", f"{len(spam_window)} messages / {spam_window_sec}s")
        return

    # caps detection
    letters = [ch for ch in content if ch.isalpha()]
    caps_min_len = store.get_int_setting(guild_id, "automod_caps_min_len")
    if len(letters) >= caps_min_len:
        upper = sum(1 for ch in letters if ch.isupper())
        ratio = int((upper / max(len(letters), 1)) * 100)
        caps_percent = store.get_int_setting(guild_id, "automod_caps_percent")
        if ratio >= caps_percent:
            await handle_automod_violation(message, "caps", f"{ratio}% uppercase")
            return

    # bad words
    bad_words = [w.casefold() for w in store.get_csv_setting(guild_id, "automod_bad_words")]
    lowered = content.casefold()
    for word in bad_words:
        if word and word in lowered:
            await handle_automod_violation(message, "blacklisted_word", word)
            return

    # links
    if store.get_int_setting(guild_id, "automod_block_links") == 1:
        whitelist = set(store.get_csv_setting(guild_id, "automod_whitelist_domains"))
        for url in URL_REGEX.findall(content):
            domain = parse_domain(url)
            if not domain:
                continue
            if domain not in whitelist:
                await handle_automod_violation(message, "link", domain)
                return


async def ensure_views_registered() -> None:
    global views_registered
    if views_registered:
        return
    client.add_view(TicketCreateView())
    client.add_view(TicketCloseView())
    client.add_view(TempVoiceCreatePanelView())
    client.add_view(OwnerAdminPanelView())
    client.add_view(OwnerControlCenterView())
    views_registered = True


def find_temp_room_for_owner(guild: discord.Guild, owner_id: int) -> Optional[discord.VoiceChannel]:
    for voice_channel in guild.voice_channels:
        stored_owner_id = store.get_temp_room_owner(voice_channel.id)
        if stored_owner_id == owner_id:
            return voice_channel
    return None


def parse_user_tokens(raw: str) -> list[str]:
    return [token.strip() for token in raw.replace(";", ",").split(",") if token.strip()]


def normalize_user_token(token: str) -> str:
    trimmed = token.strip()
    if trimmed.startswith("<@") and trimmed.endswith(">"):
        trimmed = trimmed.strip("<@!>")
    if trimmed.startswith("@"):
        trimmed = trimmed[1:]
    return trimmed.strip()


async def resolve_members_from_text(guild: discord.Guild, raw: str) -> tuple[list[discord.Member], list[str]]:
    found: list[discord.Member] = []
    missed: list[str] = []

    for token in parse_user_tokens(raw):
        norm_token = normalize_user_token(token)
        member: Optional[discord.Member] = None

        id_match = re.search(r"\d{5,20}", norm_token)
        if id_match:
            member_id = int(id_match.group(0))
            member = guild.get_member(member_id)
            if member is None:
                try:
                    member = await guild.fetch_member(member_id)
                except discord.HTTPException:
                    member = None
        else:
            token_norm = norm_token.casefold()
            member = discord.utils.find(
                lambda m: m.name.casefold() == token_norm or m.display_name.casefold() == token_norm,
                guild.members,
            )

            if member is None:
                partial_matches = [
                    m for m in guild.members
                    if token_norm in m.display_name.casefold() or token_norm in m.name.casefold()
                ]
                if len(partial_matches) == 1:
                    member = partial_matches[0]

        if member is None:
            missed.append(token)
            continue

        if member not in found:
            found.append(member)

    return found, missed


async def resolve_single_member_input(guild: discord.Guild, raw: str) -> tuple[Optional[discord.Member], Optional[str]]:
    members, missed = await resolve_members_from_text(guild, raw)
    if members:
        return members[0], None
    bad = missed[0] if missed else raw
    return None, f"Не удалось найти пользователя: {bad}"


async def resolve_banned_user_input(guild: discord.Guild, raw: str) -> tuple[Optional[discord.User], Optional[str]]:
    token = normalize_user_token(raw)
    id_match = re.search(r"\d{5,20}", token)
    if id_match:
        user_id = int(id_match.group(0))
        try:
            user = await client.fetch_user(user_id)
            return user, None
        except discord.HTTPException:
            return None, "Не удалось получить пользователя по ID."

    token_norm = token.casefold()
    try:
        async for entry in guild.bans(limit=1000):
            user = entry.user
            username = user.name.casefold() if user.name else ""
            global_name = user.global_name.casefold() if user.global_name else ""
            if token_norm in {username, global_name}:
                return user, None
            if token_norm and (token_norm in username or token_norm in global_name):
                return user, None
    except discord.HTTPException:
        return None, "Не удалось прочитать бан-лист."

    return None, f"Пользователь '{raw}' не найден в бан-листе."


def parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def format_remaining(delta: timedelta) -> str:
    total = max(0, int(delta.total_seconds()))
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours > 0:
        return f"{hours}ч {minutes}м"
    if minutes > 0:
        return f"{minutes}м {seconds}с"
    return f"{seconds}с"


def get_marriage_partner_id(guild_id: int, user_id: int) -> Optional[int]:
    row = store.get_marriage(guild_id, user_id)
    if row is None:
        return None
    first = int(row["user1_id"])
    second = int(row["user2_id"])
    return second if first == user_id else first


def get_marriage_created_at(guild_id: int, user_id: int) -> Optional[datetime]:
    row = store.get_marriage(guild_id, user_id)
    if row is None:
        return None
    return parse_iso_datetime(row["created_at"])


def parse_color_hex(raw: str) -> Optional[discord.Colour]:
    text = raw.strip().lower().replace("#", "")
    if not re.fullmatch(r"[0-9a-f]{6}", text):
        return None
    return discord.Colour(int(text, 16))


def is_role_manageable(guild: discord.Guild, role: discord.Role) -> tuple[bool, str]:
    me = guild.me
    if me is None:
        return False, "Не удалось получить роль бота."
    if role.managed:
        return False, "Эта роль управляется интеграцией и недоступна для выдачи."
    if role >= me.top_role:
        return False, "Роль выше или равна роли бота. Подними роль бота выше."
    return True, ""


async def create_temp_room_for_member(
    member: discord.Member,
    room_name: Optional[str] = None,
    user_limit: int = 0,
    is_private: bool = False,
    allowed_members: Optional[list[discord.Member]] = None,
) -> tuple[discord.VoiceChannel, bool]:
    existing = find_temp_room_for_owner(member.guild, member.id)
    if existing is not None:
        return existing, False

    category_id = get_id_setting(member.guild.id, "temp_voice_category_id")
    category = member.guild.get_channel(category_id) if category_id else None

    if category is None:
        lobby_id = get_id_setting(member.guild.id, "temp_voice_lobby_id")
        lobby = member.guild.get_channel(lobby_id) if lobby_id else None
        if isinstance(lobby, discord.VoiceChannel):
            category = lobby.category

    if category is not None and not isinstance(category, discord.CategoryChannel):
        category = None

    default_role_overwrite = discord.PermissionOverwrite(view_channel=True, connect=not is_private)

    overwrites = {
        member.guild.default_role: default_role_overwrite,
        member: discord.PermissionOverwrite(
            view_channel=True,
            connect=True,
            speak=True,
            stream=True,
            use_soundboard=True,
            move_members=True,
            mute_members=True,
            deafen_members=True,
            manage_channels=True,
        ),
    }

    for allowed_member in allowed_members or []:
        if allowed_member.id == member.id:
            continue
        overwrites[allowed_member] = discord.PermissionOverwrite(
            view_channel=True,
            connect=True,
            speak=True,
            stream=True,
            use_soundboard=True,
        )

    me = member.guild.me
    if me is not None:
        overwrites[me] = discord.PermissionOverwrite(
            view_channel=True,
            connect=True,
            speak=True,
            stream=True,
            use_soundboard=True,
            move_members=True,
            manage_channels=True,
        )

    resolved_room_name = (room_name or f"room-{member.display_name}")[:90]
    room = await member.guild.create_voice_channel(
        name=resolved_room_name,
        category=category,
        overwrites=overwrites,
        user_limit=user_limit,
        reason="Temp room create (button)",
    )
    store.add_temp_room(member.guild.id, room.id, member.id)
    return room, True


def is_private_room(room: discord.VoiceChannel) -> bool:
    overwrite = room.overwrites_for(room.guild.default_role)
    return overwrite.connect is False


async def safe_send_modal(
    interaction: discord.Interaction,
    modal: discord.ui.Modal,
    *,
    context: str,
    retry_delay_sec: float = 0.8,
) -> bool:
    for attempt in range(2):
        try:
            await interaction.response.send_modal(modal)
            return True
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionResetError) as exc:
            if attempt == 0:
                logger.warning("Сетевой сбой при открытии модалки (%s), повторяем: %s", context, exc)
                await asyncio.sleep(retry_delay_sec)
                continue

            logger.warning("Не удалось открыть модалку после повтора (%s): %s", context, exc)
            break
        except discord.HTTPException as exc:
            logger.warning("HTTP ошибка при открытии модалки (%s): %s", context, exc)
            break
        except Exception:
            logger.exception("Неожиданная ошибка при открытии модалки (%s)", context)
            break

    text = "Не удалось открыть окно из-за временной сетевой ошибки. Нажми кнопку еще раз через пару секунд."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except Exception:
        logger.debug("Не удалось отправить fallback-сообщение для модалки (%s)", context)

    return False


async def safe_defer_interaction(
    interaction: discord.Interaction,
    *,
    ephemeral: bool = True,
    thinking: bool = True,
    context: str = "interaction",
) -> bool:
    try:
        if interaction.response.is_done():
            return True
        await interaction.response.defer(ephemeral=ephemeral, thinking=thinking)
        return True
    except discord.NotFound as exc:
        logger.warning("Interaction истек до defer (%s): %s", context, exc)
        return False
    except (aiohttp.ClientError, asyncio.TimeoutError, discord.HTTPException) as exc:
        logger.warning("Не удалось defer interaction (%s): %s", context, exc)
        return False


async def safe_reply_interaction(
    interaction: discord.Interaction,
    text: str,
    *,
    ephemeral: bool = True,
    context: str = "interaction",
) -> bool:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(text, ephemeral=ephemeral)
        return True
    except discord.NotFound as exc:
        logger.warning("Interaction истек до send (%s): %s", context, exc)
        return False
    except (aiohttp.ClientError, asyncio.TimeoutError, discord.HTTPException) as exc:
        logger.warning("Не удалось отправить ответ interaction (%s): %s", context, exc)
        return False


class TempRoomCreateModal(discord.ui.Modal, title="Параметры временной комнаты"):
    room_name = discord.ui.TextInput(label="Название", required=False, max_length=90, placeholder="room-my-team")
    room_limit = discord.ui.TextInput(label="Лимит (0-99)", required=False, default="0", max_length=2)
    privacy = discord.ui.TextInput(
        label="Тип комнаты",
        required=False,
        default="open",
        placeholder="open или private",
        max_length=10,
    )
    allowed_users = discord.ui.TextInput(
        label="Доступ (id/@/name, через запятую)",
        required=False,
        max_length=400,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Кнопка работает только на сервере.", ephemeral=True)
            return

        try:
            limit = int(self.room_limit.value.strip() or "0")
        except ValueError:
            await interaction.response.send_message("Лимит должен быть числом 0-99.", ephemeral=True)
            return

        if limit < 0 or limit > 99:
            await interaction.response.send_message("Лимит должен быть в диапазоне 0-99.", ephemeral=True)
            return

        privacy_mode = (self.privacy.value or "open").strip().lower()
        is_private = privacy_mode in {"private", "closed", "закрытая", "закрыто"}

        allowed_members: list[discord.Member] = []
        missed: list[str] = []
        if is_private and (self.allowed_users.value or "").strip():
            allowed_members, missed = await resolve_members_from_text(interaction.guild, self.allowed_users.value)

        try:
            room, is_new = await create_temp_room_for_member(
                interaction.user,
                room_name=(self.room_name.value or "").strip() or None,
                user_limit=limit,
                is_private=is_private,
                allowed_members=allowed_members,
            )
        except discord.HTTPException:
            await interaction.response.send_message("Не удалось создать комнату. Проверь права бота.", ephemeral=True)
            return

        if interaction.user.voice is not None:
            try:
                await interaction.user.move_to(room, reason="Temp room move by button")
            except discord.HTTPException:
                pass

        prefix = "Создана" if is_new else "Уже есть"
        mode = "private" if is_private else "open"
        missed_note = f" | не найдены: {', '.join(missed[:5])}" if missed else ""
        await interaction.response.send_message(
            f"{prefix} комната: {room.mention} | mode={mode} | limit={room.user_limit}{missed_note}",
            ephemeral=True,
        )


class TempRoomAccessModal(discord.ui.Modal):
    def __init__(self, add_access: bool) -> None:
        self.add_access = add_access
        title = "Добавить доступ в мою private-комнату" if add_access else "Убрать доступ из моей private-комнаты"
        super().__init__(title=title)

        self.users = discord.ui.TextInput(
            label="Пользователи (id/@/name через запятую)",
            required=True,
            max_length=400,
        )
        self.add_item(self.users)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Кнопка работает только на сервере.", ephemeral=True)
            return

        room = find_temp_room_for_owner(interaction.guild, interaction.user.id)
        if room is None:
            await interaction.response.send_message("У тебя нет созданной временной комнаты.", ephemeral=True)
            return

        if not is_private_room(room):
            await interaction.response.send_message("Текущая комната не private. Закрой её кнопкой ниже.", ephemeral=True)
            return

        members, missed = await resolve_members_from_text(interaction.guild, self.users.value)
        if not members:
            await interaction.response.send_message("Не удалось найти пользователей по введенным значениям.", ephemeral=True)
            return

        changed = 0
        for member in members:
            overwrite = room.overwrites_for(member)
            if self.add_access:
                overwrite.connect = True
                overwrite.view_channel = True
            else:
                overwrite.connect = None
                overwrite.view_channel = None

            try:
                if overwrite.is_empty():
                    await room.set_permissions(member, overwrite=None, reason="Temp room access update")
                else:
                    await room.set_permissions(member, overwrite=overwrite, reason="Temp room access update")
                changed += 1
            except discord.HTTPException:
                continue

        mode = "добавлен" if self.add_access else "удален"
        missed_note = f" | не найдены: {', '.join(missed[:5])}" if missed else ""
        await interaction.response.send_message(f"Доступ {mode} для {changed} пользователей{missed_note}", ephemeral=True)


class TempVoiceCreatePanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Создать комнату", style=discord.ButtonStyle.green, custom_id="tempvoice:create")
    async def create_temp_voice(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_send_modal(interaction, TempRoomCreateModal(), context="tempvoice:create")

    @discord.ui.button(label="Добавить доступ", style=discord.ButtonStyle.primary, custom_id="tempvoice:add_access")
    async def add_access(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_send_modal(interaction, TempRoomAccessModal(add_access=True), context="tempvoice:add_access")

    @discord.ui.button(label="Убрать доступ", style=discord.ButtonStyle.secondary, custom_id="tempvoice:remove_access")
    async def remove_access(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_send_modal(interaction, TempRoomAccessModal(add_access=False), context="tempvoice:remove_access")

    @discord.ui.button(label="Сделать комнату open", style=discord.ButtonStyle.success, custom_id="tempvoice:make_open")
    async def make_open(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Кнопка работает только на сервере.", ephemeral=True)
            return

        room = find_temp_room_for_owner(interaction.guild, interaction.user.id)
        if room is None:
            await interaction.response.send_message("У тебя нет созданной временной комнаты.", ephemeral=True)
            return

        overwrite = room.overwrites_for(interaction.guild.default_role)
        overwrite.connect = True
        await room.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason="Temp room open")
        await interaction.response.send_message("Комната открыта для всех.", ephemeral=True)

    @discord.ui.button(label="Сделать комнату private", style=discord.ButtonStyle.danger, custom_id="tempvoice:make_private")
    async def make_private(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Кнопка работает только на сервере.", ephemeral=True)
            return

        room = find_temp_room_for_owner(interaction.guild, interaction.user.id)
        if room is None:
            await interaction.response.send_message("У тебя нет созданной временной комнаты.", ephemeral=True)
            return

        overwrite = room.overwrites_for(interaction.guild.default_role)
        overwrite.connect = False
        await room.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason="Temp room private")
        await interaction.response.send_message("Комната переведена в private режим.", ephemeral=True)


def is_owner_panel_user(user_id: int) -> bool:
    return _is_owner_override(user_id)


async def execute_owner_moderation_action(
    guild: discord.Guild,
    moderator: discord.Member,
    action: str,
    target_input: str,
    reason: str,
    *,
    value_raw: str = "",
    prefetched_member_id: Optional[int] = None,
) -> str:
    normalized_reason = (reason or "Без причины").strip() or "Без причины"
    normalized_value = (value_raw or "").strip()

    target_id: Optional[int] = None
    target: Optional[discord.Member] = None
    banned_user: Optional[discord.User] = None

    if action == "unwarn":
        try:
            target_id = int(target_input.strip())
        except ValueError as exc:
            raise ValueError("Для unwarn нужен числовой ID warn.") from exc
    elif action == "unban":
        banned_user, error = await resolve_banned_user_input(guild, target_input)
        if banned_user is None:
            raise ValueError(error or "Пользователь в бан-листе не найден.")
        target_id = banned_user.id
    else:
        if prefetched_member_id is not None:
            target = guild.get_member(prefetched_member_id)
            if target is None:
                try:
                    target = await guild.fetch_member(prefetched_member_id)
                except discord.HTTPException:
                    target = None

        if target is None:
            target, error = await resolve_single_member_input(guild, target_input)
            if target is None:
                raise ValueError(error or "Пользователь не найден на сервере.")

        ok, why = can_moderate_target(moderator, target)
        if not ok:
            raise ValueError(why)
        target_id = target.id

    if action == "ban":
        delete_days = int(normalized_value) if normalized_value else 0
        delete_days = max(0, min(7, delete_days))
        await guild.ban(target, reason=normalized_reason, delete_message_days=delete_days)
        case_id = await record_case(guild, "ban", moderator.id, target.id, normalized_reason, {"delete_days": delete_days})
        return f"ban выполнен. Case #{case_id}"

    if action == "kick":
        await target.kick(reason=normalized_reason)
        case_id = await record_case(guild, "kick", moderator.id, target.id, normalized_reason, None)
        return f"kick выполнен. Case #{case_id}"

    if action == "timeout":
        minutes = int(normalized_value) if normalized_value else 60
        minutes = max(1, minutes)
        await target.edit(timed_out_until=utcnow() + timedelta(minutes=minutes), reason=normalized_reason)
        case_id = await record_case(guild, "timeout", moderator.id, target.id, normalized_reason, {"minutes": minutes})
        return f"timeout выполнен. Case #{case_id}"

    if action == "voice_ban":
        await set_voice_ban_for_member(guild, target, True, normalized_reason)
        if target.voice is not None:
            await target.move_to(None, reason="voice ban by owner panel")
        case_id = await record_case(guild, "voice_ban", moderator.id, target.id, normalized_reason, None)
        return f"voice_ban выполнен. Case #{case_id}"

    if action == "warn":
        points = int(normalized_value) if normalized_value else 1
        points = max(1, points)
        warn_id = store.add_warn(guild.id, target.id, moderator.id, points, normalized_reason)
        total = store.get_warn_total(guild.id, target.id)
        case_id = await record_case(
            guild,
            "warn",
            moderator.id,
            target.id,
            normalized_reason,
            {"warn_id": warn_id, "points": points, "total_points": total},
        )
        extras = await apply_warn_threshold_action(guild, target, total, moderator.id, normalized_reason)
        suffix = f" | авто: {', '.join(extras)}" if extras else ""
        return f"warn выполнен. Case #{case_id}{suffix}"

    if action == "unban":
        await guild.unban(banned_user, reason=normalized_reason)
        case_id = await record_case(guild, "unban", moderator.id, target_id, normalized_reason, None)
        return f"unban выполнен. Case #{case_id}"

    if action == "untimeout":
        await target.edit(timed_out_until=None, reason=normalized_reason)
        case_id = await record_case(guild, "untimeout", moderator.id, target.id, normalized_reason, None)
        return f"untimeout выполнен. Case #{case_id}"

    if action == "voice_unban":
        await set_voice_ban_for_member(guild, target, False, normalized_reason)
        case_id = await record_case(guild, "voice_unban", moderator.id, target.id, normalized_reason, None)
        return f"voice_unban выполнен. Case #{case_id}"

    if action == "unwarn":
        warn_row = store.get_warn(guild.id, target_id)
        if warn_row is None:
            raise ValueError("warn id не найден.")
        if int(warn_row["active"]) == 0:
            raise ValueError("warn уже снят.")
        store.deactivate_warn(guild.id, target_id)
        case_id = await record_case(
            guild,
            "unwarn",
            moderator.id,
            int(warn_row["user_id"]),
            normalized_reason,
            {"warn_id": target_id},
        )
        return f"unwarn выполнен. Case #{case_id}"

    raise ValueError("Неизвестное действие панели.")


async def start_owner_panel_moderation_flow(interaction: discord.Interaction, action: str) -> None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Панель доступна только на сервере.", ephemeral=True)
        return
    if not is_owner_panel_user(interaction.user.id):
        await interaction.response.send_message("Доступ только для owner-пользователей.", ephemeral=True)
        return

    if action not in OWNER_PANEL_MOD_ACTIONS:
        await interaction.response.send_message("Неизвестное действие moderation-flow.", ephemeral=True)
        return

    admin_channel_id = get_id_setting(interaction.guild.id, "owner_admin_channel_id")
    if admin_channel_id is not None and interaction.channel_id != admin_channel_id:
        await interaction.response.send_message(
            f"Используй этот action в owner-канале: <#{admin_channel_id}>.",
            ephemeral=True,
        )
        return

    key = (interaction.guild.id, interaction.user.id)
    owner_panel_pending_actions[key] = {
        "action": action,
        "stage": "await_target",
        "channel_id": interaction.channel_id,
        "created_at": utcnow(),
    }

    if action == "unwarn":
        hint = "Напиши: ID_warn причина"
    elif action == "unban":
        hint = "Напиши: @пользователь причина"
    else:
        hint = "Напиши: @пользователь причина"

    await interaction.response.send_message(
        f"Режим {action} активирован.\n{hint}\nИли в 2 шага: сначала цель, потом причина.\nОтмена: отмена",
        ephemeral=True,
    )


async def maybe_handle_owner_panel_flow_message(message: discord.Message) -> bool:
    if message.guild is None or not isinstance(message.author, discord.Member):
        return False
    if not is_owner_panel_user(message.author.id):
        return False

    key = (message.guild.id, message.author.id)
    state = owner_panel_pending_actions.get(key)
    if state is None:
        return False

    channel_id = state.get("channel_id")
    if isinstance(channel_id, int) and message.channel.id != channel_id:
        return False

    created_at = state.get("created_at")
    if isinstance(created_at, datetime):
        age = (utcnow() - created_at).total_seconds()
        if age > OWNER_PANEL_FLOW_TTL_SECONDS:
            owner_panel_pending_actions.pop(key, None)
            await message.reply("Сессия действия истекла. Выбери действие заново в панели.", mention_author=False)
            return True

    action = str(state.get("action") or "")
    stage = str(state.get("stage") or "await_target")
    text = message.content.strip()

    async def finish_action(reason_text: str) -> bool:
        target_input = str(state.get("target_input") or "").strip()
        prefetched_member_id = state.get("target_member_id")

        if not target_input:
            owner_panel_pending_actions.pop(key, None)
            await message.reply("Цель не зафиксирована. Выбери action заново.", mention_author=False)
            return True

        reason = "Без причины" if reason_text in {"-", "—"} else (reason_text or "Без причины")
        try:
            result = await execute_owner_moderation_action(
                message.guild,
                message.author,
                action,
                target_input,
                reason,
                prefetched_member_id=prefetched_member_id if isinstance(prefetched_member_id, int) else None,
            )
            await message.reply(result, mention_author=False)
        except ValueError as exc:
            await message.reply(str(exc), mention_author=False)
        except discord.HTTPException:
            await message.reply("Ошибка выполнения действия. Проверь права бота и попробуй снова.", mention_author=False)
        finally:
            owner_panel_pending_actions.pop(key, None)

        return True

    if text.casefold() in {"отмена", "cancel"}:
        owner_panel_pending_actions.pop(key, None)
        await message.reply("Действие отменено.", mention_author=False)
        return True

    if stage == "await_target":
        if action == "unwarn":
            id_match = re.match(r"\s*(\d{1,20})(?:\s+(.+))?\s*$", text)
            if id_match is None:
                await message.reply("Нужен числовой ID warn. Попробуй еще раз.", mention_author=False)
                return True
            state["target_input"] = id_match.group(1)
            inline_reason = (id_match.group(2) or "").strip()
            if inline_reason:
                return await finish_action(inline_reason)
        else:
            mention = message.mentions[0] if message.mentions else None
            if mention is not None:
                state["target_input"] = f"<@{mention.id}>"
                if isinstance(mention, discord.Member):
                    state["target_member_id"] = mention.id
                inline_reason = re.sub(r"^<@!?\d+>\s*", "", text).strip()
                if inline_reason:
                    return await finish_action(inline_reason)
            else:
                if not text:
                    await message.reply("Укажи пользователя: @mention, username или ID.", mention_author=False)
                    return True
                id_with_reason = re.match(r"\s*(\d{5,20})(?:\s+(.+))?\s*$", text)
                if id_with_reason is not None:
                    state["target_input"] = id_with_reason.group(1)
                    inline_reason = (id_with_reason.group(2) or "").strip()
                    if inline_reason:
                        return await finish_action(inline_reason)
                else:
                    # Username/display name path: keep full message as target, ask reason next.
                    state["target_input"] = text

        state["stage"] = "await_reason"
        await message.reply("Цель принята. Теперь отправь причину одним сообщением (или '-' для без причины).", mention_author=False)
        return True

    if stage == "await_reason":
        return await finish_action(text)

    owner_panel_pending_actions.pop(key, None)
    return False


async def build_backup_file_for_guild(guild: discord.Guild) -> discord.File:
    payload = {
        "guild_id": guild.id,
        "created_at": iso_now(),
        "data": store.backup_guild_data(guild.id),
    }
    raw = json.dumps(payload, ensure_ascii=False, indent=2)
    file_name = f"backup-guild-{guild.id}-{int(utcnow().timestamp())}.json"
    return discord.File(io.BytesIO(raw.encode("utf-8")), filename=file_name)


class OwnerAdminPanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Backup", style=discord.ButtonStyle.primary, emoji="💾", custom_id="ownerpanel:backup")
    async def make_backup(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Кнопка доступна только на сервере.", ephemeral=True)
            return
        if not is_owner_panel_user(interaction.user.id):
            await interaction.response.send_message("Доступ только для owner-пользователей.", ephemeral=True)
            return

        backup_file = await build_backup_file_for_guild(interaction.guild)
        await interaction.response.send_message("Backup создан.", file=backup_file, ephemeral=True)

    @discord.ui.button(label="Обновить панели", style=discord.ButtonStyle.secondary, emoji="🔁", custom_id="ownerpanel:republish")
    async def republish_panels(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Кнопка доступна только на сервере.", ephemeral=True)
            return
        if not is_owner_panel_user(interaction.user.id):
            await interaction.response.send_message("Доступ только для owner-пользователей.", ephemeral=True)
            return

        await publish_owner_panels(interaction.guild)
        await interaction.response.send_message("Панели опубликованы заново.", ephemeral=True)

    @discord.ui.button(label="Sync", style=discord.ButtonStyle.success, emoji="⚡", custom_id="ownerpanel:sync")
    async def sync_commands(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Кнопка доступна только на сервере.", ephemeral=True)
            return
        if not is_owner_panel_user(interaction.user.id):
            await interaction.response.send_message("Доступ только для owner-пользователей.", ephemeral=True)
            return

        global commands_synced
        commands_synced = False
        await sync_app_commands()
        await interaction.response.send_message("Синхронизация slash-команд выполнена.", ephemeral=True)


class OwnerActionSelect(discord.ui.Select):
    def __init__(self) -> None:
        options = [
            discord.SelectOption(label="Ban", value="ban", emoji="🛑", description="Забанить пользователя"),
            discord.SelectOption(label="Kick", value="kick", emoji="👢", description="Кикнуть пользователя"),
            discord.SelectOption(label="Timeout", value="timeout", emoji="⏱", description="Выдать timeout"),
            discord.SelectOption(label="Voice Ban", value="voice_ban", emoji="🔇", description="Запретить вход в voice"),
            discord.SelectOption(label="Warn", value="warn", emoji="⚠", description="Выдать warn"),
            discord.SelectOption(label="Unban", value="unban", emoji="✅", description="Разбанить пользователя"),
            discord.SelectOption(label="Untimeout", value="untimeout", emoji="✅", description="Снять timeout"),
            discord.SelectOption(label="Voice Unban", value="voice_unban", emoji="🔊", description="Снять voice ban"),
            discord.SelectOption(label="Unwarn", value="unwarn", emoji="🧹", description="Снять warn по ID"),
            discord.SelectOption(label="Clear", value="clear", emoji="🧽", description="Очистить сообщения"),
            discord.SelectOption(label="Lock", value="lock", emoji="🔒", description="Закрыть текстовый канал"),
            discord.SelectOption(label="Unlock", value="unlock", emoji="🔓", description="Открыть текстовый канал"),
            discord.SelectOption(label="Set ModLog", value="set_modlog", emoji="📝", description="Назначить mod-log канал"),
            discord.SelectOption(label="Set Alert", value="set_alert", emoji="🚨", description="Назначить alert канал"),
            discord.SelectOption(label="Set Backup", value="set_backup", emoji="🗃", description="Назначить backup канал"),
            discord.SelectOption(label="Schedule Reminder", value="schedule_reminder", emoji="⏰", description="Разовое напоминание"),
            discord.SelectOption(label="Schedule Every", value="schedule_every", emoji="🔁", description="Повторяющееся объявление"),
            discord.SelectOption(label="Schedule Remove", value="schedule_remove", emoji="🗑", description="Удалить расписание"),
        ]

        super().__init__(
            placeholder="Выбери действие...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="ownerpanel:action_select",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Панель доступна только на сервере.", ephemeral=True)
            return
        if not is_owner_panel_user(interaction.user.id):
            await interaction.response.send_message("Доступ только для owner-пользователей.", ephemeral=True)
            return

        action = self.values[0]
        if action in OWNER_PANEL_MOD_ACTIONS:
            await start_owner_panel_moderation_flow(interaction, action)
            return

        if action == "clear":
            await safe_send_modal(interaction, ClearModal(), context="ownerpanel:clear")
            return

        if action == "lock":
            await safe_send_modal(interaction, LockUnlockModal(lock=True), context="ownerpanel:lock")
            return

        if action == "unlock":
            await safe_send_modal(interaction, LockUnlockModal(lock=False), context="ownerpanel:unlock")
            return

        if action == "set_modlog":
            await safe_send_modal(
                interaction,
                ChannelSettingModal("modlog_channel_id", "Control Center: set_modlog"),
                context="ownerpanel:set_modlog",
            )
            return

        if action == "set_alert":
            await safe_send_modal(
                interaction,
                ChannelSettingModal("alert_channel_id", "Control Center: set_alert"),
                context="ownerpanel:set_alert",
            )
            return

        if action == "set_backup":
            await safe_send_modal(
                interaction,
                ChannelSettingModal("backup_channel_id", "Control Center: set_backup"),
                context="ownerpanel:set_backup",
            )
            return

        if action in {"schedule_reminder", "schedule_every", "schedule_remove"}:
            await safe_send_modal(interaction, ScheduleModal(action), context=f"ownerpanel:{action}")
            return

        await interaction.response.send_message("Неизвестное действие.", ephemeral=True)


class OwnerControlCenterView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(OwnerActionSelect())


class ModerationActionModal(discord.ui.Modal):
    def __init__(self, action: str) -> None:
        self.action = action
        super().__init__(title=f"Панель: {action}")

        target_label = "ID warn" if action == "unwarn" else "Пользователь (@ник или username)"
        target_placeholder = "например: @user или username" if action != "unwarn" else "например: 123"
        self.target_id = discord.ui.TextInput(label=target_label, required=True, max_length=120, placeholder=target_placeholder)
        self.value = discord.ui.TextInput(label="Параметр (минуты/баллы/delete_days)", required=False, max_length=8)
        self.reason = discord.ui.TextInput(label="Причина", required=False, max_length=300)

        self.add_item(self.target_id)
        self.add_item(self.value)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Кнопка доступна только на сервере.", ephemeral=True)
            return
        if not is_owner_panel_user(interaction.user.id):
            await interaction.response.send_message("Доступ только для owner-пользователей.", ephemeral=True)
            return

        try:
            result = await execute_owner_moderation_action(
                interaction.guild,
                interaction.user,
                self.action,
                self.target_id.value,
                self.reason.value,
                value_raw=self.value.value,
            )
            await interaction.response.send_message(result, ephemeral=True)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
        except discord.HTTPException:
            await interaction.response.send_message("Ошибка выполнения действия. Проверь ввод и права бота.", ephemeral=True)


class ClearModal(discord.ui.Modal, title="Панель: clear"):
    amount = discord.ui.TextInput(label="Количество сообщений (1-200)", required=True, default="20", max_length=3)
    channel_id = discord.ui.TextInput(label="ID канала (пусто = текущий)", required=False, max_length=22)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Кнопка доступна только на сервере.", ephemeral=True)
            return
        if not is_owner_panel_user(interaction.user.id):
            await interaction.response.send_message("Доступ только для owner-пользователей.", ephemeral=True)
            return

        try:
            amount = max(1, min(200, int(self.amount.value.strip())))
        except ValueError:
            await interaction.response.send_message("Количество должно быть числом 1-200.", ephemeral=True)
            return

        target_channel: Optional[discord.abc.Messageable] = interaction.channel
        if (self.channel_id.value or "").strip():
            try:
                channel_id = int(self.channel_id.value.strip())
            except ValueError:
                await interaction.response.send_message("ID канала должен быть числом.", ephemeral=True)
                return
            target_channel = interaction.guild.get_channel(channel_id)

        if not isinstance(target_channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("Нужен текстовый канал.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        deleted = await target_channel.purge(limit=amount, reason=f"clear by owner panel {interaction.user}")
        case_id = await record_case(
            interaction.guild,
            "clear",
            interaction.user.id,
            None,
            "Clear by owner panel",
            {"count": len(deleted), "channel_id": target_channel.id},
        )
        await interaction.followup.send(f"clear выполнен. Удалено {len(deleted)}. Case #{case_id}", ephemeral=True)


class ChannelSettingModal(discord.ui.Modal):
    def __init__(self, key: str, title: str) -> None:
        self.key = key
        super().__init__(title=title)
        self.channel_id = discord.ui.TextInput(label="ID текстового канала", required=True, max_length=22)
        self.add_item(self.channel_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Кнопка доступна только на сервере.", ephemeral=True)
            return
        if not is_owner_panel_user(interaction.user.id):
            await interaction.response.send_message("Доступ только для owner-пользователей.", ephemeral=True)
            return

        try:
            channel_id = int(self.channel_id.value.strip())
        except ValueError:
            await interaction.response.send_message("ID канала должен быть числом.", ephemeral=True)
            return

        channel = interaction.guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Текстовый канал не найден.", ephemeral=True)
            return

        store.set_setting(interaction.guild.id, self.key, str(channel.id))
        await interaction.response.send_message(f"Настройка обновлена: {channel.mention}", ephemeral=True)


class LockUnlockModal(discord.ui.Modal):
    def __init__(self, lock: bool) -> None:
        self.lock = lock
        title = "Панель: lock" if lock else "Панель: unlock"
        super().__init__(title=title)
        self.channel_id = discord.ui.TextInput(label="ID канала (пусто = текущий)", required=False, max_length=22)
        self.reason = discord.ui.TextInput(label="Причина", required=False, max_length=300)
        self.add_item(self.channel_id)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Кнопка доступна только на сервере.", ephemeral=True)
            return
        if not is_owner_panel_user(interaction.user.id):
            await interaction.response.send_message("Доступ только для owner-пользователей.", ephemeral=True)
            return

        target = interaction.channel
        if (self.channel_id.value or "").strip():
            try:
                channel_id = int(self.channel_id.value.strip())
            except ValueError:
                await interaction.response.send_message("ID канала должен быть числом.", ephemeral=True)
                return
            target = interaction.guild.get_channel(channel_id)

        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message("Нужен текстовый канал.", ephemeral=True)
            return

        overwrite = target.overwrites_for(target.guild.default_role)
        overwrite.send_messages = False if self.lock else None
        await target.set_permissions(target.guild.default_role, overwrite=overwrite, reason=(self.reason.value or "panel lock/unlock"))

        action = "lock" if self.lock else "unlock"
        case_id = await record_case(
            interaction.guild,
            action,
            interaction.user.id,
            None,
            self.reason.value or "Без причины",
            {"channel_id": target.id},
        )
        await interaction.response.send_message(f"{action} выполнен. Case #{case_id}", ephemeral=True)


class ScheduleModal(discord.ui.Modal):
    def __init__(self, action: str) -> None:
        self.action = action
        super().__init__(title=f"Панель: {action}")

        if action in {"schedule_reminder", "schedule_every"}:
            self.value_1 = discord.ui.TextInput(label="Минуты", required=True, max_length=6)
            self.value_2 = discord.ui.TextInput(label="ID канала (пусто = текущий для reminder)", required=False, max_length=22)
            self.value_3 = discord.ui.TextInput(label="Текст", required=True, max_length=1500)
            self.add_item(self.value_1)
            self.add_item(self.value_2)
            self.add_item(self.value_3)
        else:
            self.value_1 = discord.ui.TextInput(label="ID schedule", required=True, max_length=12)
            self.add_item(self.value_1)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Кнопка доступна только на сервере.", ephemeral=True)
            return
        if not is_owner_panel_user(interaction.user.id):
            await interaction.response.send_message("Доступ только для owner-пользователей.", ephemeral=True)
            return

        if self.action == "schedule_remove":
            try:
                schedule_id = int(self.value_1.value.strip())
            except ValueError:
                await interaction.response.send_message("ID schedule должен быть числом.", ephemeral=True)
                return
            row = store.get_schedule(interaction.guild.id, schedule_id)
            if row is None:
                await interaction.response.send_message("schedule не найден.", ephemeral=True)
                return
            store.remove_schedule(interaction.guild.id, schedule_id)
            await interaction.response.send_message("schedule_remove выполнен.", ephemeral=True)
            return

        try:
            minutes = int(self.value_1.value.strip())
        except ValueError:
            await interaction.response.send_message("Минуты должны быть числом.", ephemeral=True)
            return

        channel = interaction.channel
        if (self.value_2.value or "").strip():
            try:
                channel_id = int(self.value_2.value.strip())
            except ValueError:
                await interaction.response.send_message("ID канала должен быть числом.", ephemeral=True)
                return
            channel = interaction.guild.get_channel(channel_id)

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("Нужен текстовый канал.", ephemeral=True)
            return

        message = self.value_3.value
        if self.action == "schedule_reminder":
            next_run = int((utcnow() + timedelta(minutes=max(1, minutes))).timestamp())
            schedule_id = store.add_schedule(interaction.guild.id, channel.id, message, next_run, None, interaction.user.id)
            await interaction.response.send_message(f"schedule_reminder создан. ID={schedule_id}", ephemeral=True)
            return

        interval = int(timedelta(minutes=max(1, minutes)).total_seconds())
        next_run = int((utcnow() + timedelta(minutes=max(1, minutes))).timestamp())
        schedule_id = store.add_schedule(interaction.guild.id, channel.id, message, next_run, interval, interaction.user.id)
        await interaction.response.send_message(f"schedule_every создан. ID={schedule_id}", ephemeral=True)


class OwnerModerationButtonsView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="ban", style=discord.ButtonStyle.danger, custom_id="ownerpanel:cmd_ban")
    async def cmd_ban(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_send_modal(interaction, ModerationActionModal("ban"), context="ownerpanel:cmd_ban")

    @discord.ui.button(label="kick", style=discord.ButtonStyle.danger, custom_id="ownerpanel:cmd_kick")
    async def cmd_kick(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_send_modal(interaction, ModerationActionModal("kick"), context="ownerpanel:cmd_kick")

    @discord.ui.button(label="timeout", style=discord.ButtonStyle.secondary, custom_id="ownerpanel:cmd_timeout")
    async def cmd_timeout(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_send_modal(interaction, ModerationActionModal("timeout"), context="ownerpanel:cmd_timeout")

    @discord.ui.button(label="voice_ban", style=discord.ButtonStyle.secondary, custom_id="ownerpanel:cmd_voice_ban")
    async def cmd_voice_ban(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_send_modal(interaction, ModerationActionModal("voice_ban"), context="ownerpanel:cmd_voice_ban")

    @discord.ui.button(label="warn", style=discord.ButtonStyle.primary, custom_id="ownerpanel:cmd_warn")
    async def cmd_warn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_send_modal(interaction, ModerationActionModal("warn"), context="ownerpanel:cmd_warn")


class OwnerModerationButtonsView2(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="unban", style=discord.ButtonStyle.success, custom_id="ownerpanel:cmd_unban")
    async def cmd_unban(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_send_modal(interaction, ModerationActionModal("unban"), context="ownerpanel:cmd_unban")

    @discord.ui.button(label="untimeout", style=discord.ButtonStyle.success, custom_id="ownerpanel:cmd_untimeout")
    async def cmd_untimeout(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_send_modal(interaction, ModerationActionModal("untimeout"), context="ownerpanel:cmd_untimeout")

    @discord.ui.button(label="voice_unban", style=discord.ButtonStyle.success, custom_id="ownerpanel:cmd_voice_unban")
    async def cmd_voice_unban(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_send_modal(interaction, ModerationActionModal("voice_unban"), context="ownerpanel:cmd_voice_unban")

    @discord.ui.button(label="unwarn", style=discord.ButtonStyle.success, custom_id="ownerpanel:cmd_unwarn")
    async def cmd_unwarn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_send_modal(interaction, ModerationActionModal("unwarn"), context="ownerpanel:cmd_unwarn")

    @discord.ui.button(label="clear", style=discord.ButtonStyle.primary, custom_id="ownerpanel:cmd_clear")
    async def cmd_clear(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_send_modal(interaction, ClearModal(), context="ownerpanel:cmd_clear")


class OwnerConfigButtonsView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="lock", style=discord.ButtonStyle.secondary, custom_id="ownerpanel:cmd_lock")
    async def cmd_lock(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_send_modal(interaction, LockUnlockModal(lock=True), context="ownerpanel:cmd_lock")

    @discord.ui.button(label="unlock", style=discord.ButtonStyle.secondary, custom_id="ownerpanel:cmd_unlock")
    async def cmd_unlock(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_send_modal(interaction, LockUnlockModal(lock=False), context="ownerpanel:cmd_unlock")

    @discord.ui.button(label="set_modlog", style=discord.ButtonStyle.primary, custom_id="ownerpanel:cmd_set_modlog")
    async def cmd_set_modlog(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_send_modal(
            interaction,
            ChannelSettingModal("modlog_channel_id", "Панель: set_modlog"),
            context="ownerpanel:cmd_set_modlog",
        )

    @discord.ui.button(label="set_alert", style=discord.ButtonStyle.primary, custom_id="ownerpanel:cmd_set_alert")
    async def cmd_set_alert(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_send_modal(
            interaction,
            ChannelSettingModal("alert_channel_id", "Панель: set_alert_channel"),
            context="ownerpanel:cmd_set_alert",
        )

    @discord.ui.button(label="set_backup", style=discord.ButtonStyle.primary, custom_id="ownerpanel:cmd_set_backup")
    async def cmd_set_backup(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_send_modal(
            interaction,
            ChannelSettingModal("backup_channel_id", "Панель: set_backup_channel"),
            context="ownerpanel:cmd_set_backup",
        )


class OwnerScheduleButtonsView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="schedule_reminder", style=discord.ButtonStyle.secondary, custom_id="ownerpanel:cmd_schedule_reminder")
    async def cmd_schedule_reminder(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_send_modal(interaction, ScheduleModal("schedule_reminder"), context="ownerpanel:cmd_schedule_reminder")

    @discord.ui.button(label="schedule_every", style=discord.ButtonStyle.secondary, custom_id="ownerpanel:cmd_schedule_every")
    async def cmd_schedule_every(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_send_modal(interaction, ScheduleModal("schedule_every"), context="ownerpanel:cmd_schedule_every")

    @discord.ui.button(label="schedule_remove", style=discord.ButtonStyle.secondary, custom_id="ownerpanel:cmd_schedule_remove")
    async def cmd_schedule_remove(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await safe_send_modal(interaction, ScheduleModal("schedule_remove"), context="ownerpanel:cmd_schedule_remove")


class DuelInviteView(discord.ui.View):
    def __init__(self, guild_id: int, challenger_id: int, target_id: int, stake: int) -> None:
        super().__init__(timeout=60)
        self.guild_id = guild_id
        self.challenger_id = challenger_id
        self.target_id = target_id
        self.stake = stake
        self.finished = False

    def _lock_buttons(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    @discord.ui.button(label="Принять", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Только на сервере.", ephemeral=True)
            return
        if interaction.user.id != self.target_id:
            await interaction.response.send_message("Принять дуэль может только вызванный игрок.", ephemeral=True)
            return
        if self.finished:
            await interaction.response.send_message("Эта дуэль уже завершена.", ephemeral=True)
            return

        challenger = interaction.guild.get_member(self.challenger_id)
        target = interaction.guild.get_member(self.target_id)
        if challenger is None or target is None:
            self.finished = True
            self._lock_buttons()
            await interaction.response.edit_message(content="Дуэль отменена: участник вышел с сервера.", view=self)
            return

        challenger_balance = store.get_balance(self.guild_id, challenger.id)
        target_balance = store.get_balance(self.guild_id, target.id)
        if challenger_balance < self.stake or target_balance < self.stake:
            self.finished = True
            self._lock_buttons()
            await interaction.response.edit_message(
                content="Дуэль отменена: у одного из игроков недостаточно средств.",
                view=self,
            )
            return

        winner_id = random.choice([challenger.id, target.id])
        loser_id = target.id if winner_id == challenger.id else challenger.id

        try:
            loser_balance, winner_balance = store.transfer_balance(self.guild_id, loser_id, winner_id, self.stake)
        except ValueError:
            self.finished = True
            self._lock_buttons()
            await interaction.response.edit_message(content="Дуэль отменена из-за ошибки перевода.", view=self)
            return

        store.increment_profile_counter(self.guild_id, challenger.id, "total_duels", 1)
        store.increment_profile_counter(self.guild_id, target.id, "total_duels", 1)
        store.increment_profile_counter(self.guild_id, winner_id, "duel_wins", 1)

        self.finished = True
        self._lock_buttons()
        winner_mention = f"<@{winner_id}>"
        loser_mention = f"<@{loser_id}>"
        await interaction.response.edit_message(
            content=(
                f"Дуэль завершена. Победитель: {winner_mention}.\n"
                f"Ставка {self.stake} монет списана у {loser_mention} и начислена победителю.\n"
                f"Новый баланс победителя: {winner_balance}, проигравшего: {loser_balance}"
            ),
            view=self,
        )

    @discord.ui.button(label="Отклонить", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != self.target_id:
            await interaction.response.send_message("Отклонить дуэль может только вызванный игрок.", ephemeral=True)
            return
        if self.finished:
            await interaction.response.send_message("Эта дуэль уже завершена.", ephemeral=True)
            return

        self.finished = True
        self._lock_buttons()
        await interaction.response.edit_message(content="Дуэль отклонена.", view=self)

    async def on_timeout(self) -> None:
        self.finished = True
        self._lock_buttons()


class MarryProposalView(discord.ui.View):
    def __init__(self, guild_id: int, proposer_id: int, target_id: int) -> None:
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.proposer_id = proposer_id
        self.target_id = target_id
        self.finished = False

    def _lock_buttons(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    @discord.ui.button(label="Согласиться", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != self.target_id:
            await interaction.response.send_message("Ответить может только тот, кому сделали предложение.", ephemeral=True)
            return
        if interaction.guild is None:
            await interaction.response.send_message("Только на сервере.", ephemeral=True)
            return
        if self.finished:
            await interaction.response.send_message("Предложение уже обработано.", ephemeral=True)
            return

        if store.get_marriage(self.guild_id, self.proposer_id) is not None or store.get_marriage(self.guild_id, self.target_id) is not None:
            self.finished = True
            self._lock_buttons()
            await interaction.response.edit_message(content="Брак не создан: один из пользователей уже в паре.", view=self)
            return

        if not store.set_marriage(self.guild_id, self.proposer_id, self.target_id):
            self.finished = True
            self._lock_buttons()
            await interaction.response.edit_message(content="Не удалось создать пару. Попробуйте еще раз.", view=self)
            return

        self.finished = True
        self._lock_buttons()
        await interaction.response.edit_message(
            content=f"Новая пара: <@{self.proposer_id}> и <@{self.target_id}>. Поздравляю!",
            view=self,
        )

    @discord.ui.button(label="Отказать", style=discord.ButtonStyle.secondary)
    async def decline(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != self.target_id:
            await interaction.response.send_message("Ответить может только тот, кому сделали предложение.", ephemeral=True)
            return
        if self.finished:
            await interaction.response.send_message("Предложение уже обработано.", ephemeral=True)
            return

        self.finished = True
        self._lock_buttons()
        await interaction.response.edit_message(content="Предложение отклонено.", view=self)

    async def on_timeout(self) -> None:
        self.finished = True
        self._lock_buttons()


async def create_ticket_channel(interaction: discord.Interaction) -> None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Тикеты доступны только на сервере.", ephemeral=True)
        return

    guild = interaction.guild
    member = interaction.user

    existing = discord.utils.find(
        lambda c: isinstance(c, discord.TextChannel)
        and c.topic
        and c.topic.startswith(f"ticket_owner:{member.id}"),
        guild.channels,
    )
    if existing is not None:
        await interaction.response.send_message(f"У тебя уже есть тикет: {existing.mention}", ephemeral=True)
        return

    category_id = get_id_setting(guild.id, "ticket_category_id")
    category = guild.get_channel(category_id) if category_id else None
    if category is not None and not isinstance(category, discord.CategoryChannel):
        category = None

    support_role_id = get_id_setting(guild.id, "ticket_support_role_id")
    support_role = guild.get_role(support_role_id) if support_role_id else None

    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }

    me = guild.me
    if me is not None:
        overwrites[me] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True,
            manage_messages=True,
        )

    if support_role is not None:
        overwrites[support_role] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
        )

    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "-", member.display_name.lower())
    channel_name = f"ticket-{safe_name}"[:90]

    try:
        ticket_channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            topic=f"ticket_owner:{member.id}",
            overwrites=overwrites,
            reason=f"Ticket created by {member}",
        )
    except discord.HTTPException:
        await interaction.response.send_message("Не удалось создать тикет. Проверь права бота.", ephemeral=True)
        return

    embed = discord.Embed(
        title="Тикет создан",
        description="Опиши проблему. Для закрытия используй кнопку ниже или команду /ticket_close.",
        color=discord.Color.blurple(),
    )

    await ticket_channel.send(content=member.mention, embed=embed, view=TicketCloseView())
    await interaction.response.send_message(f"Тикет создан: {ticket_channel.mention}", ephemeral=True)


async def export_ticket_transcript(channel: discord.TextChannel) -> discord.File:
    lines: list[str] = []
    async for msg in channel.history(limit=2000, oldest_first=True):
        ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
        author = f"{msg.author} ({msg.author.id})"
        content = msg.content.replace("\n", " ") if msg.content else "<embed/attachment>"
        lines.append(f"[{ts}] {author}: {content}")

    text = "\n".join(lines) if lines else "Transcript empty"
    buffer = io.BytesIO(text.encode("utf-8"))
    filename = f"ticket-{channel.id}.txt"
    return discord.File(buffer, filename=filename)


async def close_ticket_channel(channel: discord.TextChannel, closed_by: discord.abc.User, reason: str) -> None:
    guild = channel.guild
    transcript = await export_ticket_transcript(channel)

    log_channel_id = get_id_setting(guild.id, "ticket_log_channel_id")
    log_channel = guild.get_channel(log_channel_id) if log_channel_id else None
    if log_channel is not None and isinstance(log_channel, (discord.TextChannel, discord.Thread)):
        try:
            await log_channel.send(
                content=f"Тикет {channel.name} закрыт пользователем <@{closed_by.id}>. Причина: {reason}",
                file=transcript,
            )
        except discord.HTTPException:
            pass

    try:
        await channel.delete(reason=f"Ticket closed by {closed_by} | {reason}")
    except discord.HTTPException:
        pass


class TicketCreateView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Создать тикет", style=discord.ButtonStyle.green, custom_id="ticket:create")
    async def create_ticket(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await create_ticket_channel(interaction)


class TicketCloseView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Закрыть тикет", style=discord.ButtonStyle.red, custom_id="ticket:close")
    async def close_ticket(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Эта кнопка работает только в текстовом тикете.", ephemeral=True)
            return

        topic = interaction.channel.topic or ""
        if not topic.startswith("ticket_owner:"):
            await interaction.response.send_message("Это не тикет-канал.", ephemeral=True)
            return

        owner_id = None
        try:
            owner_id = int(topic.split(":", 1)[1])
        except (ValueError, IndexError):
            pass

        support_role_id = get_id_setting(interaction.guild.id, "ticket_support_role_id")
        support_role = interaction.guild.get_role(support_role_id) if support_role_id else None

        is_owner = owner_id is not None and interaction.user.id == owner_id
        is_admin = isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator
        is_support = (
            isinstance(interaction.user, discord.Member)
            and support_role is not None
            and support_role in interaction.user.roles
        )

        if not (is_owner or is_admin or is_support or _is_owner_override(interaction.user.id)):
            await interaction.response.send_message("Нет прав закрыть этот тикет.", ephemeral=True)
            return

        await interaction.response.send_message("Закрываю тикет и экспортирую переписку...", ephemeral=True)
        await close_ticket_channel(interaction.channel, interaction.user, reason="button close")


async def ensure_text_channel_or_thread(interaction: discord.Interaction) -> Optional[discord.abc.Messageable]:
    channel = interaction.channel
    if isinstance(channel, (discord.TextChannel, discord.Thread)):
        return channel
    return None


@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    original = error.original if isinstance(error, app_commands.CommandInvokeError) else error
    if isinstance(original, discord.NotFound):
        logger.warning("Interaction истек во время slash-команды: %s", original)
        return

    logger.exception("Ошибка slash-команды: %s", error)
    text = "Произошла ошибка при выполнении команды."

    if isinstance(error, app_commands.CheckFailure):
        text = "Недостаточно прав для выполнения команды."

    await safe_reply_interaction(interaction, text, ephemeral=True, context="tree.error")


async def sync_app_commands() -> None:
    global commands_synced
    if commands_synced:
        return

    try:
        voice_channel = client.get_channel(VOICE_CHANNEL_ID)
        if voice_channel is None:
            voice_channel = await client.fetch_channel(VOICE_CHANNEL_ID)

        if isinstance(voice_channel, (discord.VoiceChannel, discord.StageChannel)):
            guild_obj = discord.Object(id=voice_channel.guild.id)
            tree.copy_global_to(guild=guild_obj)
            synced = await tree.sync(guild=guild_obj)
            logger.info("Синхронизировано slash-команд для сервера %s: %s", voice_channel.guild.id, len(synced))
        else:
            synced = await tree.sync()
            logger.info("Синхронизировано глобальных slash-команд: %s", len(synced))

        commands_synced = True
    except Exception:
        logger.exception("Не удалось синхронизировать slash-команды")


# -----------------------------
# Base utility & setup commands
# -----------------------------


@tree.command(name="say", description="Отправить сообщение от лица бота")
@app_commands.describe(message="Текст", channel="Канал (опционально)")
async def say(
    interaction: discord.Interaction,
    message: str,
    channel: Optional[discord.TextChannel] = None,
) -> None:
    if not await ensure_admin(interaction):
        return

    target = channel or interaction.channel
    if not isinstance(target, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message("Команда работает только в текстовых каналах.", ephemeral=True)
        return

    await target.send(message)
    await interaction.response.send_message("Отправлено.", ephemeral=True)


@tree.command(name="set_modlog", description="Назначить канал mod-log")
async def set_modlog(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    if not await ensure_admin(interaction):
        return
    store.set_setting(interaction.guild_id, "modlog_channel_id", str(channel.id))
    await interaction.response.send_message(f"Mod-log канал: {channel.mention}", ephemeral=True)


@tree.command(name="set_alert_channel", description="Назначить канал алертов anti-raid/automod")
async def set_alert_channel(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    if not await ensure_admin(interaction):
        return
    store.set_setting(interaction.guild_id, "alert_channel_id", str(channel.id))
    await interaction.response.send_message(f"Alert канал: {channel.mention}", ephemeral=True)


@tree.command(name="set_backup_channel", description="Назначить канал для резервных копий")
async def set_backup_channel(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    if not await ensure_admin(interaction):
        return
    store.set_setting(interaction.guild_id, "backup_channel_id", str(channel.id))
    await interaction.response.send_message(f"Backup канал: {channel.mention}", ephemeral=True)


def resolve_owner_members(guild: discord.Guild) -> list[discord.Member]:
    members: list[discord.Member] = []
    for owner_id in sorted(OWNER_USER_IDS):
        member = guild.get_member(owner_id)
        if member is not None:
            members.append(member)
    return members


async def publish_owner_panels(guild: discord.Guild) -> tuple[Optional[discord.TextChannel], Optional[discord.TextChannel]]:
    temp_channel_id = get_id_setting(guild.id, "temp_create_text_channel_id")
    admin_channel_id = get_id_setting(guild.id, "owner_admin_channel_id")

    temp_channel = guild.get_channel(temp_channel_id) if temp_channel_id else None
    admin_channel = guild.get_channel(admin_channel_id) if admin_channel_id else None

    if isinstance(temp_channel, discord.TextChannel):
        embed = discord.Embed(
            title="Создание временных voice-комнат",
            description="Нажми кнопку, чтобы создать личную временную voice-комнату.",
            color=discord.Color.green(),
        )
        await temp_channel.send(embed=embed, view=TempVoiceCreatePanelView())

    if isinstance(admin_channel, discord.TextChannel):
        owners_text = ", ".join(f"<@{owner_id}>" for owner_id in sorted(OWNER_USER_IDS)) or "не заданы"
        embed = discord.Embed(
            title="Control Center",
            description=(
                "Приватная панель управления сервером.\n"
                f"Owners: {owners_text}\n"
                "Выбирай действие в меню ниже. Интерфейс без лишнего спама кнопок."
            ),
            color=discord.Color.from_rgb(35, 140, 255),
        )
        embed.set_footer(text="Owner-only panel")
        await admin_channel.send(embed=embed, view=OwnerAdminPanelView())

        await admin_channel.send(
            embed=discord.Embed(
                title="Actions",
                description=(
                    "Moderation • Config • Scheduler\n"
                    "Moderation flow: выбери action -> тегни пользователя -> напиши причину"
                ),
                color=discord.Color.dark_blue(),
            ),
            view=OwnerControlCenterView(),
        )

    return (
        temp_channel if isinstance(temp_channel, discord.TextChannel) else None,
        admin_channel if isinstance(admin_channel, discord.TextChannel) else None,
    )


@tree.command(name="setup_owner_hub", description="Создать 2 канала: public temp-room и private owner-admin")
@app_commands.describe(
    temp_channel_name="Название канала создания временных комнат",
    admin_channel_name="Название приватного owner-канала",
    category_name="Название категории для этих каналов",
)
async def setup_owner_hub(
    interaction: discord.Interaction,
    temp_channel_name: str = "создать-комнату",
    admin_channel_name: str = "owner-admin",
    category_name: str = "bot-hub",
) -> None:
    if not await ensure_admin(interaction):
        return
    if interaction.guild is None:
        await safe_reply_interaction(interaction, "Команда доступна только на сервере.", ephemeral=True, context="setup_owner_hub:guild")
        return

    if not await safe_defer_interaction(interaction, ephemeral=True, thinking=True, context="setup_owner_hub"):
        return

    guild = interaction.guild

    if not OWNER_USER_IDS:
        await safe_reply_interaction(
            interaction,
            "OWNER_USER_ID не задан в .env. Добавь хотя бы один owner ID и перезапусти бота.",
            ephemeral=True,
            context="setup_owner_hub:no_owner_ids",
        )
        return

    owners = resolve_owner_members(guild)
    if not owners:
        await safe_reply_interaction(
            interaction,
            "Ни один owner ID из .env не найден на сервере. Проверь OWNER_USER_ID.",
            ephemeral=True,
            context="setup_owner_hub:no_owner_members",
        )
        return

    category_id = get_id_setting(guild.id, "owner_hub_category_id")
    category = guild.get_channel(category_id) if category_id else None
    if category is None or not isinstance(category, discord.CategoryChannel):
        category = await guild.create_category(name=category_name[:90], reason="Setup owner hub")
        store.set_setting(guild.id, "owner_hub_category_id", str(category.id))

    temp_channel_id = get_id_setting(guild.id, "temp_create_text_channel_id")
    temp_channel = guild.get_channel(temp_channel_id) if temp_channel_id else None

    if temp_channel is None or not isinstance(temp_channel, discord.TextChannel):
        public_overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        me = guild.me
        if me is not None:
            public_overwrites[me] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                manage_messages=True,
            )

        temp_channel = await guild.create_text_channel(
            name=temp_channel_name[:90],
            category=category,
            overwrites=public_overwrites,
            reason="Setup owner hub: temp create channel",
        )
        store.set_setting(guild.id, "temp_create_text_channel_id", str(temp_channel.id))

    admin_channel_id = get_id_setting(guild.id, "owner_admin_channel_id")
    admin_channel = guild.get_channel(admin_channel_id) if admin_channel_id else None

    if admin_channel is None or not isinstance(admin_channel, discord.TextChannel):
        private_overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
        }
        me = guild.me
        if me is not None:
            private_overwrites[me] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                manage_messages=True,
            )

        for owner_member in owners:
            private_overwrites[owner_member] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            )

        admin_channel = await guild.create_text_channel(
            name=admin_channel_name[:90],
            category=category,
            overwrites=private_overwrites,
            reason="Setup owner hub: private owner admin channel",
        )
        store.set_setting(guild.id, "owner_admin_channel_id", str(admin_channel.id))

    published_temp, published_admin = await publish_owner_panels(guild)

    temp_text = published_temp.mention if published_temp else "(не удалось)"
    admin_text = published_admin.mention if published_admin else "(не удалось)"

    await safe_reply_interaction(
        interaction,
        f"Готово. Публичный канал: {temp_text}\nПриватный owner-канал: {admin_text}",
        ephemeral=True,
        context="setup_owner_hub:done",
    )


@tree.command(name="bind_temp_create_channel", description="Привязать существующий канал как public temp-create")
async def bind_temp_create_channel(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    if not await ensure_admin(interaction):
        return
    if interaction.guild is None:
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return

    store.set_setting(interaction.guild.id, "temp_create_text_channel_id", str(channel.id))
    await channel.send(
        embed=discord.Embed(
            title="Создание временных voice-комнат",
            description="Нажми кнопку ниже, чтобы создать личную временную voice-комнату.",
            color=discord.Color.green(),
        ),
        view=TempVoiceCreatePanelView(),
    )
    await interaction.response.send_message(f"Канал привязан: {channel.mention}", ephemeral=True)


@tree.command(name="bind_owner_admin_channel", description="Привязать существующий канал как private owner-admin")
async def bind_owner_admin_channel(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    if not await ensure_admin(interaction):
        return
    if interaction.guild is None:
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return
    if not OWNER_USER_IDS:
        await interaction.response.send_message("OWNER_USER_ID пуст. Заполни .env и перезапусти бота.", ephemeral=True)
        return

    guild = interaction.guild
    owners = resolve_owner_members(guild)
    if not owners:
        await interaction.response.send_message("Owner ID из .env не найдены на сервере.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    await channel.set_permissions(guild.default_role, overwrite=discord.PermissionOverwrite(view_channel=False))
    for owner_member in owners:
        await channel.set_permissions(
            owner_member,
            overwrite=discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        )

    me = guild.me
    if me is not None:
        await channel.set_permissions(
            me,
            overwrite=discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                manage_messages=True,
            ),
        )

    store.set_setting(guild.id, "owner_admin_channel_id", str(channel.id))

    await publish_owner_panels(guild)

    await interaction.followup.send(f"Owner-канал привязан: {channel.mention}", ephemeral=True)


@tree.command(name="set_raid_limits", description="Настроить лимиты anti-raid")
@app_commands.describe(
    join_per_minute="Лимит входов/мин",
    channel_create_per_minute="Лимит создания каналов/мин",
    role_create_per_minute="Лимит создания ролей/мин",
    mention_limit="Лимит упоминаний в сообщении",
)
async def set_raid_limits(
    interaction: discord.Interaction,
    join_per_minute: app_commands.Range[int, 3, 50],
    channel_create_per_minute: app_commands.Range[int, 1, 30],
    role_create_per_minute: app_commands.Range[int, 1, 20],
    mention_limit: app_commands.Range[int, 3, 30],
) -> None:
    if not await ensure_admin(interaction):
        return

    gid = interaction.guild_id
    store.set_setting(gid, "raid_join_limit", str(join_per_minute))
    store.set_setting(gid, "raid_channel_create_limit", str(channel_create_per_minute))
    store.set_setting(gid, "raid_role_create_limit", str(role_create_per_minute))
    store.set_setting(gid, "raid_mention_limit", str(mention_limit))

    await interaction.response.send_message("Лимиты anti-raid обновлены.", ephemeral=True)


@tree.command(name="set_automod_words", description="Список запрещенных слов (через запятую)")
async def set_automod_words(interaction: discord.Interaction, words: str) -> None:
    if not await ensure_admin(interaction):
        return

    normalized = [w.strip().casefold() for w in words.split(",") if w.strip()]
    store.set_csv_setting(interaction.guild_id, "automod_bad_words", normalized)
    await interaction.response.send_message(f"Обновлено слов: {len(normalized)}", ephemeral=True)


@tree.command(name="set_automod_whitelist", description="Whitelist доменов (через запятую)")
async def set_automod_whitelist(interaction: discord.Interaction, domains: str) -> None:
    if not await ensure_admin(interaction):
        return

    normalized = [d.strip().lower().replace("www.", "") for d in domains.split(",") if d.strip()]
    store.set_csv_setting(interaction.guild_id, "automod_whitelist_domains", normalized)
    await interaction.response.send_message(f"Whitelist доменов обновлен: {len(normalized)}", ephemeral=True)


@tree.command(name="set_automod_exempt_channel", description="Добавить/удалить exempt-канал для AutoMod")
@app_commands.describe(mode="add/remove")
@app_commands.choices(mode=[
    app_commands.Choice(name="add", value="add"),
    app_commands.Choice(name="remove", value="remove"),
])
async def set_automod_exempt_channel(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    mode: app_commands.Choice[str],
) -> None:
    if not await ensure_admin(interaction):
        return

    values = store.get_id_set_setting(interaction.guild_id, "automod_exempt_channel_ids")
    if mode.value == "add":
        values.add(channel.id)
    else:
        values.discard(channel.id)

    store.set_id_set_setting(interaction.guild_id, "automod_exempt_channel_ids", values)
    await interaction.response.send_message("Exempt-каналы обновлены.", ephemeral=True)


@tree.command(name="set_automod_exempt_role", description="Добавить/удалить exempt-роль для AutoMod")
@app_commands.describe(mode="add/remove")
@app_commands.choices(mode=[
    app_commands.Choice(name="add", value="add"),
    app_commands.Choice(name="remove", value="remove"),
])
async def set_automod_exempt_role(
    interaction: discord.Interaction,
    role: discord.Role,
    mode: app_commands.Choice[str],
) -> None:
    if not await ensure_admin(interaction):
        return

    values = store.get_id_set_setting(interaction.guild_id, "automod_exempt_role_ids")
    if mode.value == "add":
        values.add(role.id)
    else:
        values.discard(role.id)

    store.set_id_set_setting(interaction.guild_id, "automod_exempt_role_ids", values)
    await interaction.response.send_message("Exempt-роли обновлены.", ephemeral=True)


# -----------------------------
# Moderation commands + cases
# -----------------------------


@tree.command(name="ban", description="Забанить участника")
@app_commands.describe(member="Участник", reason="Причина", delete_days="Удалить сообщения за N дней (0-7)")
async def ban_member(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: Optional[str] = None,
    delete_days: app_commands.Range[int, 0, 7] = 0,
) -> None:
    if not await ensure_admin(interaction):
        return
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return

    ok, why = can_moderate_target(interaction.user, member)
    if not ok:
        await interaction.response.send_message(why, ephemeral=True)
        return

    await interaction.guild.ban(member, reason=reason or "Без причины", delete_message_days=delete_days)
    case_id = await record_case(
        interaction.guild,
        "ban",
        interaction.user.id,
        member.id,
        reason or "Без причины",
        {"delete_days": delete_days},
    )
    await interaction.response.send_message(f"{member.mention} забанен. Case #{case_id}", ephemeral=True)


@tree.command(name="unban", description="Разбанить пользователя по ID")
async def unban_user(interaction: discord.Interaction, user_id: str, reason: Optional[str] = None) -> None:
    if not await ensure_admin(interaction):
        return
    if interaction.guild is None:
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return

    try:
        parsed = int(user_id)
    except ValueError:
        await interaction.response.send_message("user_id должен быть числом.", ephemeral=True)
        return

    user = await client.fetch_user(parsed)
    await interaction.guild.unban(user, reason=reason or "Без причины")
    case_id = await record_case(
        interaction.guild,
        "unban",
        interaction.user.id,
        parsed,
        reason or "Без причины",
        None,
    )
    await interaction.response.send_message(f"Разбан выполнен. Case #{case_id}", ephemeral=True)


@tree.command(name="kick", description="Кикнуть участника")
async def kick_member(interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = None) -> None:
    if not await ensure_admin(interaction):
        return
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return

    ok, why = can_moderate_target(interaction.user, member)
    if not ok:
        await interaction.response.send_message(why, ephemeral=True)
        return

    await member.kick(reason=reason or "Без причины")
    case_id = await record_case(
        interaction.guild,
        "kick",
        interaction.user.id,
        member.id,
        reason or "Без причины",
        None,
    )
    await interaction.response.send_message(f"{member.mention} кикнут. Case #{case_id}", ephemeral=True)


@tree.command(name="timeout", description="Выдать timeout")
async def timeout_member(
    interaction: discord.Interaction,
    member: discord.Member,
    minutes: app_commands.Range[int, 1, 40320],
    reason: Optional[str] = None,
) -> None:
    if not await ensure_admin(interaction):
        return
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return

    ok, why = can_moderate_target(interaction.user, member)
    if not ok:
        await interaction.response.send_message(why, ephemeral=True)
        return

    await member.edit(timed_out_until=utcnow() + timedelta(minutes=minutes), reason=reason or "Без причины")
    case_id = await record_case(
        interaction.guild,
        "timeout",
        interaction.user.id,
        member.id,
        reason or "Без причины",
        {"minutes": minutes},
    )
    await interaction.response.send_message(f"Timeout выдан. Case #{case_id}", ephemeral=True)


@tree.command(name="untimeout", description="Снять timeout")
async def untimeout_member(interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = None) -> None:
    if not await ensure_admin(interaction):
        return
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return

    ok, why = can_moderate_target(interaction.user, member)
    if not ok:
        await interaction.response.send_message(why, ephemeral=True)
        return

    await member.edit(timed_out_until=None, reason=reason or "Без причины")
    case_id = await record_case(
        interaction.guild,
        "untimeout",
        interaction.user.id,
        member.id,
        reason or "Без причины",
        None,
    )
    await interaction.response.send_message(f"Timeout снят. Case #{case_id}", ephemeral=True)


@tree.command(name="voice_ban", description="Запретить вход во все voice/stage")
async def voice_ban_member(interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = None) -> None:
    if not await ensure_admin(interaction):
        return
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return

    ok, why = can_moderate_target(interaction.user, member)
    if not ok:
        await interaction.response.send_message(why, ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    changed = await set_voice_ban_for_member(interaction.guild, member, True, reason or "voice ban")
    if member.voice is not None:
        await member.move_to(None, reason="voice ban")

    case_id = await record_case(
        interaction.guild,
        "voice_ban",
        interaction.user.id,
        member.id,
        reason or "Без причины",
        {"channels_updated": changed},
    )
    await interaction.followup.send(f"Voice ban выдан. Case #{case_id}", ephemeral=True)


@tree.command(name="voice_unban", description="Снять запрет входа во все voice/stage")
async def voice_unban_member(interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = None) -> None:
    if not await ensure_admin(interaction):
        return
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return

    ok, why = can_moderate_target(interaction.user, member)
    if not ok:
        await interaction.response.send_message(why, ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    changed = await set_voice_ban_for_member(interaction.guild, member, False, reason or "voice unban")

    case_id = await record_case(
        interaction.guild,
        "voice_unban",
        interaction.user.id,
        member.id,
        reason or "Без причины",
        {"channels_updated": changed},
    )
    await interaction.followup.send(f"Voice ban снят. Case #{case_id}", ephemeral=True)


@tree.command(name="voice_mute", description="Замутить в voice")
async def voice_mute_member(interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = None) -> None:
    if not await ensure_admin(interaction):
        return
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return

    ok, why = can_moderate_target(interaction.user, member)
    if not ok:
        await interaction.response.send_message(why, ephemeral=True)
        return

    await member.edit(mute=True, reason=reason or "Без причины")
    case_id = await record_case(interaction.guild, "voice_mute", interaction.user.id, member.id, reason or "Без причины")
    await interaction.response.send_message(f"Voice mute выполнен. Case #{case_id}", ephemeral=True)


@tree.command(name="voice_unmute", description="Снять mute в voice")
async def voice_unmute_member(interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = None) -> None:
    if not await ensure_admin(interaction):
        return
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return

    ok, why = can_moderate_target(interaction.user, member)
    if not ok:
        await interaction.response.send_message(why, ephemeral=True)
        return

    await member.edit(mute=False, reason=reason or "Без причины")
    case_id = await record_case(interaction.guild, "voice_unmute", interaction.user.id, member.id, reason or "Без причины")
    await interaction.response.send_message(f"Voice unmute выполнен. Case #{case_id}", ephemeral=True)


@tree.command(name="voice_deafen", description="Заглушить в voice")
async def voice_deafen_member(interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = None) -> None:
    if not await ensure_admin(interaction):
        return
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return

    ok, why = can_moderate_target(interaction.user, member)
    if not ok:
        await interaction.response.send_message(why, ephemeral=True)
        return

    await member.edit(deafen=True, reason=reason or "Без причины")
    case_id = await record_case(interaction.guild, "voice_deafen", interaction.user.id, member.id, reason or "Без причины")
    await interaction.response.send_message(f"Voice deafen выполнен. Case #{case_id}", ephemeral=True)


@tree.command(name="voice_undeafen", description="Снять заглушение в voice")
async def voice_undeafen_member(interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = None) -> None:
    if not await ensure_admin(interaction):
        return
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return

    ok, why = can_moderate_target(interaction.user, member)
    if not ok:
        await interaction.response.send_message(why, ephemeral=True)
        return

    await member.edit(deafen=False, reason=reason or "Без причины")
    case_id = await record_case(interaction.guild, "voice_undeafen", interaction.user.id, member.id, reason or "Без причины")
    await interaction.response.send_message(f"Voice undeafen выполнен. Case #{case_id}", ephemeral=True)


@tree.command(name="voice_move", description="Переместить участника в voice")
async def voice_move_member(
    interaction: discord.Interaction,
    member: discord.Member,
    channel: discord.VoiceChannel,
    reason: Optional[str] = None,
) -> None:
    if not await ensure_admin(interaction):
        return
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return

    ok, why = can_moderate_target(interaction.user, member)
    if not ok:
        await interaction.response.send_message(why, ephemeral=True)
        return

    await member.move_to(channel, reason=reason or "Без причины")
    case_id = await record_case(
        interaction.guild,
        "voice_move",
        interaction.user.id,
        member.id,
        reason or "Без причины",
        {"to_channel_id": channel.id},
    )
    await interaction.response.send_message(f"Перемещен в {channel.mention}. Case #{case_id}", ephemeral=True)


@tree.command(name="clear", description="Удалить последние сообщения")
async def clear_messages(
    interaction: discord.Interaction,
    amount: app_commands.Range[int, 1, 200],
    channel: Optional[discord.TextChannel] = None,
) -> None:
    if not await ensure_admin(interaction):
        return

    target = channel or interaction.channel
    if not isinstance(target, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message("Нужен текстовый канал.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    deleted = await target.purge(limit=amount, reason=f"clear by {interaction.user}")
    case_id = await record_case(
        interaction.guild,
        "clear",
        interaction.user.id,
        None,
        "Clear messages",
        {"count": len(deleted), "channel_id": target.id},
    )
    await interaction.followup.send(f"Удалено сообщений: {len(deleted)}. Case #{case_id}", ephemeral=True)


@tree.command(name="lock", description="Закрыть канал для @everyone")
async def lock_channel(
    interaction: discord.Interaction,
    channel: Optional[discord.TextChannel] = None,
    reason: Optional[str] = None,
) -> None:
    if not await ensure_admin(interaction):
        return

    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        await interaction.response.send_message("Нужен текстовый канал.", ephemeral=True)
        return

    overwrite = target.overwrites_for(target.guild.default_role)
    overwrite.send_messages = False
    await target.set_permissions(target.guild.default_role, overwrite=overwrite, reason=reason or "lock")

    case_id = await record_case(
        interaction.guild,
        "lock",
        interaction.user.id,
        None,
        reason or "Без причины",
        {"channel_id": target.id},
    )
    await interaction.response.send_message(f"Канал закрыт. Case #{case_id}", ephemeral=True)


@tree.command(name="unlock", description="Открыть канал для @everyone")
async def unlock_channel(
    interaction: discord.Interaction,
    channel: Optional[discord.TextChannel] = None,
    reason: Optional[str] = None,
) -> None:
    if not await ensure_admin(interaction):
        return

    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        await interaction.response.send_message("Нужен текстовый канал.", ephemeral=True)
        return

    overwrite = target.overwrites_for(target.guild.default_role)
    overwrite.send_messages = None
    await target.set_permissions(target.guild.default_role, overwrite=overwrite, reason=reason or "unlock")

    case_id = await record_case(
        interaction.guild,
        "unlock",
        interaction.user.id,
        None,
        reason or "Без причины",
        {"channel_id": target.id},
    )
    await interaction.response.send_message(f"Канал открыт. Case #{case_id}", ephemeral=True)


@tree.command(name="case_info", description="Показать информацию по case ID")
async def case_info(interaction: discord.Interaction, case_id: int) -> None:
    if not await ensure_admin(interaction):
        return

    row = store.get_case(interaction.guild_id, case_id)
    if row is None:
        await interaction.response.send_message("Case не найден.", ephemeral=True)
        return

    embed = discord.Embed(title=f"Case #{case_id}", color=discord.Color.gold())
    embed.add_field(name="Action", value=row["action"], inline=True)
    embed.add_field(name="Moderator", value=f"<@{row['moderator_id']}>" if row["moderator_id"] else "system", inline=True)
    embed.add_field(name="Target", value=f"<@{row['target_id']}>" if row["target_id"] else "-", inline=True)
    embed.add_field(name="Reason", value=row["reason"] or "Без причины", inline=False)
    embed.add_field(name="Created", value=row["created_at"], inline=False)
    embed.add_field(name="Reverted", value="yes" if row["reverted"] else "no", inline=True)

    meta = row["metadata"] or "{}"
    if meta and meta != "{}":
        embed.add_field(name="Metadata", value=f"```json\n{meta[:900]}\n```", inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="case_undo", description="Отменить действие по case ID")
async def case_undo(interaction: discord.Interaction, case_id: int, reason: Optional[str] = None) -> None:
    if not await ensure_admin(interaction):
        return
    if interaction.guild is None:
        await interaction.response.send_message("Команда только для сервера.", ephemeral=True)
        return

    row = store.get_case(interaction.guild.id, case_id)
    if row is None:
        await interaction.response.send_message("Case не найден.", ephemeral=True)
        return
    if int(row["reverted"]) == 1:
        await interaction.response.send_message("Case уже отменен ранее.", ephemeral=True)
        return

    action = str(row["action"])
    target_id = row["target_id"]
    if target_id is None:
        await interaction.response.send_message("У case нет target, отмена невозможна.", ephemeral=True)
        return

    target_member = interaction.guild.get_member(int(target_id))

    undo_reason = reason or f"Undo case #{case_id}"

    try:
        if action in {"timeout", "warn_auto_timeout"}:
            if target_member is None:
                await interaction.response.send_message("Пользователь не найден на сервере.", ephemeral=True)
                return
            await target_member.edit(timed_out_until=None, reason=undo_reason)
        elif action in {"ban", "warn_auto_ban", "automod_ban"}:
            user = await client.fetch_user(int(target_id))
            await interaction.guild.unban(user, reason=undo_reason)
        elif action in {"voice_ban", "warn_auto_voice_ban", "automod_voice_ban"}:
            if target_member is None:
                await interaction.response.send_message("Пользователь не найден на сервере.", ephemeral=True)
                return
            await set_voice_ban_for_member(interaction.guild, target_member, False, undo_reason)
        elif action == "voice_mute":
            if target_member is None:
                await interaction.response.send_message("Пользователь не найден на сервере.", ephemeral=True)
                return
            await target_member.edit(mute=False, reason=undo_reason)
        elif action == "voice_deafen":
            if target_member is None:
                await interaction.response.send_message("Пользователь не найден на сервере.", ephemeral=True)
                return
            await target_member.edit(deafen=False, reason=undo_reason)
        else:
            await interaction.response.send_message("Для этого типа case автоматическая отмена не поддерживается.", ephemeral=True)
            return
    except discord.HTTPException:
        await interaction.response.send_message("Discord API отклонил отмену case.", ephemeral=True)
        return

    store.mark_case_reverted(interaction.guild.id, case_id)
    new_case = await record_case(
        interaction.guild,
        "case_undo",
        interaction.user.id,
        int(target_id),
        f"Undo for case #{case_id}: {undo_reason}",
        {"original_case": case_id, "original_action": action},
    )
    await interaction.response.send_message(f"Case #{case_id} отменен. Новый case #{new_case}", ephemeral=True)


# -----------------------------
# Warn system
# -----------------------------


@tree.command(name="warn", description="Выдать warn с баллами")
async def warn_member(
    interaction: discord.Interaction,
    member: discord.Member,
    points: app_commands.Range[int, 1, 10],
    reason: Optional[str] = None,
) -> None:
    if not await ensure_admin(interaction):
        return
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return

    ok, why = can_moderate_target(interaction.user, member)
    if not ok:
        await interaction.response.send_message(why, ephemeral=True)
        return

    warn_reason = reason or "Без причины"
    warn_id = store.add_warn(interaction.guild.id, member.id, interaction.user.id, points, warn_reason)
    total = store.get_warn_total(interaction.guild.id, member.id)

    case_id = await record_case(
        interaction.guild,
        "warn",
        interaction.user.id,
        member.id,
        warn_reason,
        {"warn_id": warn_id, "points": points, "total_points": total},
    )

    extra_actions: list[str] = []
    try:
        extra_actions = await apply_warn_threshold_action(interaction.guild, member, total, interaction.user.id, warn_reason)
    except discord.HTTPException:
        extra_actions.append("ошибка авто-наказания (проверь права бота)")

    suffix = f" | авто: {', '.join(extra_actions)}" if extra_actions else ""
    await interaction.response.send_message(
        f"Warn выдан: id={warn_id}, total={total}, case #{case_id}{suffix}",
        ephemeral=True,
    )


@tree.command(name="unwarn", description="Снять warn по ID")
async def unwarn_member(interaction: discord.Interaction, warn_id: int, reason: Optional[str] = None) -> None:
    if not await ensure_admin(interaction):
        return
    if interaction.guild is None:
        await interaction.response.send_message("Команда только для сервера.", ephemeral=True)
        return

    row = store.get_warn(interaction.guild.id, warn_id)
    if row is None:
        await interaction.response.send_message("Warn не найден.", ephemeral=True)
        return

    if int(row["active"]) == 0:
        await interaction.response.send_message("Warn уже снят.", ephemeral=True)
        return

    store.deactivate_warn(interaction.guild.id, warn_id)
    total = store.get_warn_total(interaction.guild.id, int(row["user_id"]))

    # recalibrate warn level marker to avoid repeated auto actions after unwarn.
    level = 0
    if total >= store.get_int_setting(interaction.guild.id, "warn_ban_points"):
        level = 3
    elif total >= store.get_int_setting(interaction.guild.id, "warn_voice_ban_points"):
        level = 2
    elif total >= store.get_int_setting(interaction.guild.id, "warn_timeout_points"):
        level = 1
    store.set_warn_level(interaction.guild.id, int(row["user_id"]), level)

    case_id = await record_case(
        interaction.guild,
        "unwarn",
        interaction.user.id,
        int(row["user_id"]),
        reason or "Без причины",
        {"warn_id": warn_id, "new_total": total},
    )
    await interaction.response.send_message(f"Warn снят. New total={total}. Case #{case_id}", ephemeral=True)


@tree.command(name="warns", description="Список warn пользователя")
async def warns_list(interaction: discord.Interaction, member: discord.Member) -> None:
    if not await ensure_admin(interaction):
        return

    rows = store.list_warns(interaction.guild_id, member.id)
    total = store.get_warn_total(interaction.guild_id, member.id)

    if not rows:
        await interaction.response.send_message(f"У {member.mention} нет warn-ов.", ephemeral=True)
        return

    lines = []
    for row in rows:
        status = "active" if int(row["active"]) == 1 else "inactive"
        lines.append(
            f"#{row['id']} | {status} | +{row['points']} | mod={row['moderator_id']} | {row['reason']}"
        )

    content = "\n".join(lines[:20])
    await interaction.response.send_message(
        f"Warns для {member.mention} (total={total}):\n```\n{content}\n```",
        ephemeral=True,
    )


# -----------------------------
# Ticket system
# -----------------------------


@tree.command(name="profile", description="Информация о пользователе")
async def profile(interaction: discord.Interaction, member: Optional[discord.Member] = None) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return

    target = member or interaction.guild.get_member(interaction.user.id)
    if target is None:
        await interaction.response.send_message("Пользователь не найден.", ephemeral=True)
        return

    profile_row = store.get_profile(interaction.guild.id, target.id)
    balance = int(profile_row["balance"])
    daily_streak = int(profile_row["daily_streak"])
    reports_count = store.count_reports_for_target(interaction.guild.id, target.id)

    partner_id = get_marriage_partner_id(interaction.guild.id, target.id)
    partner_text = f"<@{partner_id}>" if partner_id else "нет"

    personal_role_id = store.get_personal_role_id(interaction.guild.id, target.id)
    personal_role = interaction.guild.get_role(personal_role_id) if personal_role_id else None
    role_text = personal_role.mention if personal_role else "нет"

    daily_last = parse_iso_datetime(profile_row["daily_last"])
    daily_text = "готово"
    if daily_last is not None:
        next_daily = daily_last + timedelta(hours=ECONOMY_DAILY_COOLDOWN_HOURS)
        if utcnow() < next_daily:
            daily_text = f"через {format_remaining(next_daily - utcnow())}"

    embed = discord.Embed(
        title=f"Профиль: {target.display_name}",
        color=discord.Color.blurple(),
        timestamp=utcnow(),
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="Баланс", value=f"{balance} монет", inline=True)
    embed.add_field(name="Daily streak", value=str(daily_streak), inline=True)
    embed.add_field(name="Пара", value=partner_text, inline=True)
    embed.add_field(name="Личная роль", value=role_text, inline=True)
    embed.add_field(name="Жалоб", value=str(reports_count), inline=True)
    embed.add_field(name="/timely", value=daily_text, inline=True)
    embed.add_field(
        name="Статистика",
        value=(
            f"Дуэли: {int(profile_row['total_duels'])}\n"
            f"Победы в дуэлях: {int(profile_row['duel_wins'])}\n"
            f"Победы в slots: {int(profile_row['slots_wins'])}\n"
            f"Победы в rps: {int(profile_row['rps_wins'])}"
        ),
        inline=False,
    )
    await interaction.response.send_message(embed=embed)


@tree.command(name="report", description="Подать жалобу на пользователя")
@app_commands.describe(member="На кого жалоба", reason="Причина жалобы")
async def report_member(interaction: discord.Interaction, member: discord.Member, reason: str) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return

    if member.id == interaction.user.id:
        await interaction.response.send_message("Нельзя подать жалобу на самого себя.", ephemeral=True)
        return
    if member.bot:
        await interaction.response.send_message("На ботов жалобы не принимаются.", ephemeral=True)
        return

    report_id = store.add_report(interaction.guild.id, interaction.user.id, member.id, reason.strip()[:500] or "Без причины")

    channel_id = get_alert_channel_id(interaction.guild.id)
    if channel_id:
        channel = interaction.guild.get_channel(channel_id)
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            embed = discord.Embed(title=f"Report #{report_id}", color=discord.Color.red(), timestamp=utcnow())
            embed.add_field(name="Reporter", value=f"<@{interaction.user.id}>", inline=True)
            embed.add_field(name="Target", value=f"<@{member.id}>", inline=True)
            embed.add_field(name="Reason", value=reason[:1000], inline=False)
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                pass

    await interaction.response.send_message(f"Жалоба отправлена. Номер: #{report_id}", ephemeral=True)


@tree.command(name="balance", description="Проверить баланс профиля")
async def balance(interaction: discord.Interaction, member: Optional[discord.Member] = None) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return

    target = member or interaction.guild.get_member(interaction.user.id)
    if target is None:
        await interaction.response.send_message("Пользователь не найден.", ephemeral=True)
        return

    coins = store.get_balance(interaction.guild.id, target.id)
    await interaction.response.send_message(f"Баланс {target.mention}: {coins} монет")


@tree.command(name="timely", description="Ежедневная награда")
async def timely(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return

    member = interaction.guild.get_member(interaction.user.id)
    if member is None:
        await interaction.response.send_message("Пользователь не найден.", ephemeral=True)
        return

    profile_row = store.get_profile(interaction.guild.id, member.id)
    now = utcnow()
    last_claim = parse_iso_datetime(profile_row["daily_last"])

    if last_claim is not None:
        next_claim = last_claim + timedelta(hours=ECONOMY_DAILY_COOLDOWN_HOURS)
        if now < next_claim:
            await interaction.response.send_message(
                f"Следующая награда будет доступна через {format_remaining(next_claim - now)}.",
                ephemeral=True,
            )
            return

    old_streak = int(profile_row["daily_streak"])
    if last_claim is None:
        new_streak = 1
    elif now - last_claim <= timedelta(hours=ECONOMY_DAILY_COOLDOWN_HOURS * 2):
        new_streak = old_streak + 1
    else:
        new_streak = 1

    reward = ECONOMY_DAILY_BASE_REWARD + min(new_streak - 1, ECONOMY_DAILY_STREAK_CAP) * ECONOMY_DAILY_STREAK_BONUS
    balance_now = store.add_balance(interaction.guild.id, member.id, reward, min_balance=0)
    store.set_daily_claim(interaction.guild.id, member.id, now, new_streak)

    await interaction.response.send_message(
        f"Daily получен: +{reward} монет. Твой баланс: {balance_now}. Streak: {new_streak}"
    )


@tree.command(name="give", description="Перевести серверную валюту")
async def give(interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 1_000_000]) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return
    if member.id == interaction.user.id:
        await interaction.response.send_message("Нельзя переводить монеты самому себе.", ephemeral=True)
        return
    if member.bot:
        await interaction.response.send_message("Ботам переводы запрещены.", ephemeral=True)
        return

    try:
        from_balance, to_balance = store.transfer_balance(interaction.guild.id, interaction.user.id, member.id, int(amount))
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    await interaction.response.send_message(
        f"Перевод выполнен: {member.mention} получил {amount} монет. Твой баланс: {from_balance}, его: {to_balance}"
    )


@tree.command(name="duel", description="Вызвать человека на дуэль")
@app_commands.describe(member="Кого вызываешь", stake="Ставка в монетах")
async def duel(interaction: discord.Interaction, member: discord.Member, stake: app_commands.Range[int, 10, 1_000_000] = 50) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return
    if member.id == interaction.user.id:
        await interaction.response.send_message("С самим собой дуэлиться нельзя.", ephemeral=True)
        return
    if member.bot:
        await interaction.response.send_message("Дуэль с ботом недоступна.", ephemeral=True)
        return

    challenger_balance = store.get_balance(interaction.guild.id, interaction.user.id)
    target_balance = store.get_balance(interaction.guild.id, member.id)
    if challenger_balance < stake:
        await interaction.response.send_message("У тебя недостаточно монет для ставки.", ephemeral=True)
        return
    if target_balance < stake:
        await interaction.response.send_message("У соперника недостаточно монет для этой ставки.", ephemeral=True)
        return

    view = DuelInviteView(interaction.guild.id, interaction.user.id, member.id, int(stake))
    await interaction.response.send_message(
        f"{member.mention}, тебе вызов на дуэль от {interaction.user.mention}. Ставка: {stake} монет.",
        view=view,
    )


@tree.command(name="shop", description="Магазин ролей сервера")
@app_commands.describe(mode="browse / buy / add / remove", role="Роль", price="Цена для add")
@app_commands.choices(mode=[
    app_commands.Choice(name="browse", value="browse"),
    app_commands.Choice(name="buy", value="buy"),
    app_commands.Choice(name="add", value="add"),
    app_commands.Choice(name="remove", value="remove"),
])
async def shop(
    interaction: discord.Interaction,
    mode: app_commands.Choice[str],
    role: Optional[discord.Role] = None,
    price: Optional[app_commands.Range[int, 1, 1_000_000]] = None,
) -> None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return

    selected = mode.value

    if selected == "browse":
        rows = store.list_shop_roles(interaction.guild.id)
        if not rows:
            await interaction.response.send_message("Магазин пуст.", ephemeral=True)
            return

        lines: list[str] = []
        for row in rows[:25]:
            shop_role = interaction.guild.get_role(int(row["role_id"]))
            if shop_role is None:
                continue
            lines.append(f"{shop_role.mention} — {int(row['price'])} монет")

        if not lines:
            await interaction.response.send_message("В магазине нет доступных ролей.", ephemeral=True)
            return

        await interaction.response.send_message("Магазин ролей:\n" + "\n".join(lines), ephemeral=True)
        return

    if selected == "buy":
        if role is None:
            await interaction.response.send_message("Укажи роль для покупки.", ephemeral=True)
            return

        row = store.get_shop_role(interaction.guild.id, role.id)
        if row is None:
            await interaction.response.send_message("Этой роли нет в магазине.", ephemeral=True)
            return

        if role in interaction.user.roles:
            await interaction.response.send_message("У тебя уже есть эта роль.", ephemeral=True)
            return

        ok, reason = is_role_manageable(interaction.guild, role)
        if not ok:
            await interaction.response.send_message(reason, ephemeral=True)
            return

        cost = int(row["price"])
        try:
            new_balance = store.add_balance(interaction.guild.id, interaction.user.id, -cost, min_balance=0)
        except ValueError:
            await interaction.response.send_message("Недостаточно монет для покупки.", ephemeral=True)
            return

        try:
            await interaction.user.add_roles(role, reason=f"Shop purchase by {interaction.user}")
        except discord.HTTPException:
            store.add_balance(interaction.guild.id, interaction.user.id, cost, min_balance=0)
            await interaction.response.send_message("Не удалось выдать роль. Монеты возвращены.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"Покупка успешна: {role.mention}. Списано {cost} монет, осталось {new_balance}.",
            ephemeral=True,
        )
        return

    if selected in {"add", "remove"} and not await ensure_admin(interaction):
        return

    if selected == "add":
        if role is None or price is None:
            await interaction.response.send_message("Для add укажи role и price.", ephemeral=True)
            return

        ok, reason = is_role_manageable(interaction.guild, role)
        if not ok:
            await interaction.response.send_message(reason, ephemeral=True)
            return

        store.upsert_shop_role(interaction.guild.id, role.id, int(price))
        await interaction.response.send_message(f"Роль {role.mention} добавлена в магазин. Цена: {int(price)}", ephemeral=True)
        return

    if selected == "remove":
        if role is None:
            await interaction.response.send_message("Для remove укажи role.", ephemeral=True)
            return

        removed = store.remove_shop_role(interaction.guild.id, role.id)
        if removed:
            await interaction.response.send_message(f"Роль {role.mention} удалена из магазина.", ephemeral=True)
        else:
            await interaction.response.send_message("Этой роли нет в магазине.", ephemeral=True)
        return

    await interaction.response.send_message("Неизвестный режим shop.", ephemeral=True)


@tree.command(name="top", description="Топ людей на сервере")
async def top(interaction: discord.Interaction, limit: app_commands.Range[int, 3, 20] = 10) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return

    rows = store.list_top_profiles(interaction.guild.id, int(limit))
    if not rows:
        await interaction.response.send_message("Пока нет данных профилей.", ephemeral=True)
        return

    lines: list[str] = []
    for index, row in enumerate(rows, start=1):
        user_id = int(row["user_id"])
        member = interaction.guild.get_member(user_id)
        name = member.display_name if member else f"user:{user_id}"
        lines.append(f"{index}. {name} — {int(row['balance'])} монет")

    await interaction.response.send_message("Топ по балансу:\n" + "\n".join(lines))


@tree.command(name="marry", description="Стать парой с другим пользователем")
@app_commands.describe(member="Пользователь для предложения", mode="propose / divorce / info")
@app_commands.choices(mode=[
    app_commands.Choice(name="propose", value="propose"),
    app_commands.Choice(name="divorce", value="divorce"),
    app_commands.Choice(name="info", value="info"),
])
async def marry(
    interaction: discord.Interaction,
    member: Optional[discord.Member] = None,
    mode: Optional[app_commands.Choice[str]] = None,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return

    selected = mode.value if mode is not None else "propose"

    if selected == "info":
        partner_id = get_marriage_partner_id(interaction.guild.id, interaction.user.id)
        if partner_id is None:
            await interaction.response.send_message("Ты пока не состоишь в паре.", ephemeral=True)
            return
        created_at = get_marriage_created_at(interaction.guild.id, interaction.user.id)
        date_text = created_at.strftime("%Y-%m-%d") if created_at else "неизвестно"
        await interaction.response.send_message(f"Твоя пара: <@{partner_id}> (с {date_text})", ephemeral=True)
        return

    if selected == "divorce":
        removed = store.clear_marriage(interaction.guild.id, interaction.user.id)
        if removed:
            await interaction.response.send_message("Пара расторгнута.")
        else:
            await interaction.response.send_message("Ты не состоишь в паре.", ephemeral=True)
        return

    if member is None:
        await interaction.response.send_message("Укажи пользователя для предложения.", ephemeral=True)
        return
    if member.id == interaction.user.id:
        await interaction.response.send_message("Нельзя сделать предложение самому себе.", ephemeral=True)
        return
    if member.bot:
        await interaction.response.send_message("Ботам предложение сделать нельзя.", ephemeral=True)
        return
    if store.get_marriage(interaction.guild.id, interaction.user.id) is not None:
        await interaction.response.send_message("Ты уже состоишь в паре.", ephemeral=True)
        return
    if store.get_marriage(interaction.guild.id, member.id) is not None:
        await interaction.response.send_message("Этот пользователь уже состоит в паре.", ephemeral=True)
        return

    view = MarryProposalView(interaction.guild.id, interaction.user.id, member.id)
    await interaction.response.send_message(
        f"{member.mention}, тебе предложение от {interaction.user.mention}.",
        view=view,
    )


@tree.command(name="myrole", description="Управление личной ролью")
@app_commands.describe(mode="create / rename / color / delete", name="Имя роли", color_hex="Цвет #RRGGBB")
@app_commands.choices(mode=[
    app_commands.Choice(name="create", value="create"),
    app_commands.Choice(name="rename", value="rename"),
    app_commands.Choice(name="color", value="color"),
    app_commands.Choice(name="delete", value="delete"),
])
async def myrole(
    interaction: discord.Interaction,
    mode: app_commands.Choice[str],
    name: Optional[str] = None,
    color_hex: Optional[str] = None,
) -> None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return

    me = interaction.guild.me
    if me is None or not me.guild_permissions.manage_roles:
        await interaction.response.send_message("Боту нужно право Manage Roles.", ephemeral=True)
        return

    existing_role_id = store.get_personal_role_id(interaction.guild.id, interaction.user.id)
    existing_role = interaction.guild.get_role(existing_role_id) if existing_role_id else None
    selected = mode.value

    if selected == "create":
        role_name = (name or f"role-{interaction.user.display_name}").strip()[:100]
        parsed_color = parse_color_hex(color_hex) if color_hex else discord.Colour.random()
        if parsed_color is None:
            await interaction.response.send_message("Цвет должен быть в формате #RRGGBB.", ephemeral=True)
            return

        role = existing_role
        if role is None:
            try:
                role = await interaction.guild.create_role(name=role_name, color=parsed_color, mentionable=True, reason="myrole create")
            except discord.HTTPException:
                await interaction.response.send_message("Не удалось создать роль.", ephemeral=True)
                return
            store.set_personal_role(interaction.guild.id, interaction.user.id, role.id)
        else:
            ok, reason = is_role_manageable(interaction.guild, role)
            if not ok:
                await interaction.response.send_message(reason, ephemeral=True)
                return
            try:
                await role.edit(name=role_name, color=parsed_color, reason="myrole update")
            except discord.HTTPException:
                await interaction.response.send_message("Не удалось обновить роль.", ephemeral=True)
                return

        try:
            await interaction.user.add_roles(role, reason="myrole assign")
        except discord.HTTPException:
            await interaction.response.send_message("Роль создана, но выдать ее не удалось.", ephemeral=True)
            return

        await interaction.response.send_message(f"Личная роль готова: {role.mention}", ephemeral=True)
        return

    if existing_role is None:
        await interaction.response.send_message("Сначала создай роль через mode=create.", ephemeral=True)
        return

    ok, reason = is_role_manageable(interaction.guild, existing_role)
    if not ok:
        await interaction.response.send_message(reason, ephemeral=True)
        return

    if selected == "rename":
        if not (name or "").strip():
            await interaction.response.send_message("Укажи новое имя роли.", ephemeral=True)
            return
        try:
            await existing_role.edit(name=name.strip()[:100], reason="myrole rename")
        except discord.HTTPException:
            await interaction.response.send_message("Не удалось переименовать роль.", ephemeral=True)
            return
        await interaction.response.send_message("Имя роли обновлено.", ephemeral=True)
        return

    if selected == "color":
        if not color_hex:
            await interaction.response.send_message("Укажи цвет в формате #RRGGBB.", ephemeral=True)
            return
        parsed_color = parse_color_hex(color_hex)
        if parsed_color is None:
            await interaction.response.send_message("Цвет должен быть в формате #RRGGBB.", ephemeral=True)
            return
        try:
            await existing_role.edit(color=parsed_color, reason="myrole color")
        except discord.HTTPException:
            await interaction.response.send_message("Не удалось изменить цвет роли.", ephemeral=True)
            return
        await interaction.response.send_message("Цвет роли обновлен.", ephemeral=True)
        return

    if selected == "delete":
        try:
            await existing_role.delete(reason="myrole delete")
        except discord.HTTPException:
            await interaction.response.send_message("Не удалось удалить роль.", ephemeral=True)
            return
        store.clear_personal_role(interaction.guild.id, interaction.user.id)
        await interaction.response.send_message("Личная роль удалена.", ephemeral=True)
        return

    await interaction.response.send_message("Неизвестный режим myrole.", ephemeral=True)


@tree.command(name="slots", description="Мини-игра: слоты")
async def slots(interaction: discord.Interaction, bet: app_commands.Range[int, 10, 1_000_000] = 50) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return

    symbols = ["🍒", "🍋", "🔔", "💎", "7️⃣"]
    rolled = [random.choice(symbols) for _ in range(3)]
    line = " ".join(rolled)

    try:
        store.add_balance(interaction.guild.id, interaction.user.id, -int(bet), min_balance=0)
    except ValueError:
        await interaction.response.send_message("Недостаточно монет для ставки.", ephemeral=True)
        return

    payout = 0
    if rolled[0] == rolled[1] == rolled[2]:
        if rolled[0] == "7️⃣":
            payout = int(bet) * 8
        elif rolled[0] == "💎":
            payout = int(bet) * 5
        else:
            payout = int(bet) * 3
    elif rolled[0] == rolled[1] or rolled[1] == rolled[2] or rolled[0] == rolled[2]:
        payout = int(int(bet) * 1.8)

    if payout > 0:
        balance_now = store.add_balance(interaction.guild.id, interaction.user.id, payout, min_balance=0)
        if payout > int(bet):
            store.increment_profile_counter(interaction.guild.id, interaction.user.id, "slots_wins", 1)
        result = f"Выигрыш: +{payout - int(bet)}"
    else:
        balance_now = store.get_balance(interaction.guild.id, interaction.user.id)
        result = f"Проигрыш: -{int(bet)}"

    await interaction.response.send_message(
        f"Слоты: {line}\n{result}\nТекущий баланс: {balance_now}"
    )


@tree.command(name="rps", description="Сыграть в камень, ножницы, бумагу")
@app_commands.choices(choice=[
    app_commands.Choice(name="камень", value="rock"),
    app_commands.Choice(name="ножницы", value="scissors"),
    app_commands.Choice(name="бумага", value="paper"),
])
async def rps(interaction: discord.Interaction, choice: app_commands.Choice[str]) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return

    bot_choice = random.choice(["rock", "paper", "scissors"])
    icons = {"rock": "🪨", "paper": "📄", "scissors": "✂"}

    player = choice.value
    if player == bot_choice:
        await interaction.response.send_message(f"Ничья: ты {icons[player]} vs бот {icons[bot_choice]}")
        return

    wins_against = {"rock": "scissors", "scissors": "paper", "paper": "rock"}
    if wins_against[player] == bot_choice:
        new_balance = store.add_balance(interaction.guild.id, interaction.user.id, 30, min_balance=0)
        store.increment_profile_counter(interaction.guild.id, interaction.user.id, "rps_wins", 1)
        await interaction.response.send_message(
            f"Победа: ты {icons[player]} vs бот {icons[bot_choice]}. +30 монет. Баланс: {new_balance}"
        )
        return

    current_balance = store.get_balance(interaction.guild.id, interaction.user.id)
    penalty = min(10, current_balance)
    if penalty > 0:
        new_balance = store.add_balance(interaction.guild.id, interaction.user.id, -penalty, min_balance=0)
    else:
        new_balance = current_balance
    await interaction.response.send_message(
        f"Поражение: ты {icons[player]} vs бот {icons[bot_choice]}. -{penalty} монет. Баланс: {new_balance}"
    )


@tree.command(name="play", description="Запустить музыку в голосовом канале")
@app_commands.describe(url="Прямая ссылка на аудио поток")
async def play(interaction: discord.Interaction, url: str) -> None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return
    if not url.startswith(("http://", "https://")):
        await interaction.response.send_message("Нужна прямая http/https ссылка на аудио.", ephemeral=True)
        return
    if shutil.which("ffmpeg") is None:
        await interaction.response.send_message("ffmpeg не найден в системе. Установи ffmpeg и попробуй снова.", ephemeral=True)
        return
    if interaction.user.voice is None or interaction.user.voice.channel is None:
        await interaction.response.send_message("Сначала зайди в voice-канал.", ephemeral=True)
        return
    if interaction.user.voice.channel.id != VOICE_CHANNEL_ID:
        await interaction.response.send_message(
            f"Для /play зайди в основной voice-канал бота: <#{VOICE_CHANNEL_ID}>.",
            ephemeral=True,
        )
        return

    target_channel = interaction.guild.get_channel(VOICE_CHANNEL_ID)
    if not isinstance(target_channel, (discord.VoiceChannel, discord.StageChannel)):
        await interaction.response.send_message("Основной voice-канал не найден.", ephemeral=True)
        return

    vc = discord.utils.get(client.voice_clients, guild=interaction.guild)
    try:
        if vc is None or not vc.is_connected():
            vc = await target_channel.connect(reconnect=True, self_deaf=False, self_mute=False)
        elif vc.channel and vc.channel.id != target_channel.id:
            await vc.move_to(target_channel)
    except discord.HTTPException:
        await interaction.response.send_message("Не удалось подключиться к voice-каналу.", ephemeral=True)
        return

    try:
        if vc.is_playing():
            vc.stop()

        source = discord.FFmpegPCMAudio(
            url,
            before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
            options="-vn",
        )
        vc.play(source)
    except Exception:
        await interaction.response.send_message(
            "Не удалось запустить воспроизведение. Проверь ссылку: нужна прямая аудио-ссылка.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message("Музыка запущена в voice-канале бота.")


@tree.command(name="set_ticket_category", description="Категория для тикетов")
async def set_ticket_category(interaction: discord.Interaction, category: discord.CategoryChannel) -> None:
    if not await ensure_admin(interaction):
        return
    store.set_setting(interaction.guild_id, "ticket_category_id", str(category.id))
    await interaction.response.send_message(f"Ticket category: {category.name}", ephemeral=True)


@tree.command(name="set_ticket_log", description="Канал логов тикетов")
async def set_ticket_log(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    if not await ensure_admin(interaction):
        return
    store.set_setting(interaction.guild_id, "ticket_log_channel_id", str(channel.id))
    await interaction.response.send_message(f"Ticket log channel: {channel.mention}", ephemeral=True)


@tree.command(name="set_ticket_support", description="Роль поддержки тикетов")
async def set_ticket_support(interaction: discord.Interaction, role: discord.Role) -> None:
    if not await ensure_admin(interaction):
        return
    store.set_setting(interaction.guild_id, "ticket_support_role_id", str(role.id))
    await interaction.response.send_message(f"Ticket support role: {role.mention}", ephemeral=True)


@tree.command(name="ticket_panel", description="Опубликовать панель создания тикета")
async def ticket_panel(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None) -> None:
    if not await ensure_admin(interaction):
        return

    target = channel or interaction.channel
    if not isinstance(target, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message("Нужен текстовый канал.", ephemeral=True)
        return

    embed = discord.Embed(
        title="Поддержка",
        description="Нажми кнопку ниже, чтобы создать приватный тикет.",
        color=discord.Color.blurple(),
    )
    await target.send(embed=embed, view=TicketCreateView())
    await interaction.response.send_message("Панель тикетов отправлена.", ephemeral=True)


@tree.command(name="ticket_close", description="Закрыть текущий тикет")
async def ticket_close(interaction: discord.Interaction, reason: Optional[str] = None) -> None:
    if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("Команда доступна только в текстовом канале тикета.", ephemeral=True)
        return

    topic = interaction.channel.topic or ""
    if not topic.startswith("ticket_owner:"):
        await interaction.response.send_message("Это не тикет-канал.", ephemeral=True)
        return

    owner_id = None
    try:
        owner_id = int(topic.split(":", 1)[1])
    except (ValueError, IndexError):
        pass

    support_role_id = get_id_setting(interaction.guild.id, "ticket_support_role_id")
    support_role = interaction.guild.get_role(support_role_id) if support_role_id else None

    is_owner = owner_id is not None and interaction.user.id == owner_id
    is_admin = isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator
    is_support = (
        isinstance(interaction.user, discord.Member)
        and support_role is not None
        and support_role in interaction.user.roles
    )

    if not (is_owner or is_admin or is_support or _is_owner_override(interaction.user.id)):
        await interaction.response.send_message("Нет прав закрыть этот тикет.", ephemeral=True)
        return

    await interaction.response.send_message("Закрываю тикет...", ephemeral=True)
    await close_ticket_channel(interaction.channel, interaction.user, reason or "ticket_close")


# -----------------------------
# Temp voice rooms
# -----------------------------


@tree.command(name="set_temp_lobby", description="Voice-канал, при входе в который создается личная комната")
async def set_temp_lobby(interaction: discord.Interaction, channel: discord.VoiceChannel) -> None:
    if not await ensure_admin(interaction):
        return
    store.set_setting(interaction.guild_id, "temp_voice_lobby_id", str(channel.id))
    await interaction.response.send_message(f"Temp lobby: {channel.mention}", ephemeral=True)


@tree.command(name="set_temp_category", description="Категория для временных voice-комнат")
async def set_temp_category(interaction: discord.Interaction, category: discord.CategoryChannel) -> None:
    if not await ensure_admin(interaction):
        return
    store.set_setting(interaction.guild_id, "temp_voice_category_id", str(category.id))
    await interaction.response.send_message(f"Temp room category: {category.name}", ephemeral=True)


def get_owned_temp_room(member: discord.Member) -> Optional[discord.VoiceChannel]:
    if member.voice is None or not isinstance(member.voice.channel, discord.VoiceChannel):
        return None
    owner_id = store.get_temp_room_owner(member.voice.channel.id)
    if owner_id is None:
        return None
    if owner_id != member.id and not _is_admin_member(member):
        return None
    return member.voice.channel


@tree.command(name="room_lock", description="Закрыть свою временную voice-комнату")
async def room_lock(interaction: discord.Interaction) -> None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда только для сервера.", ephemeral=True)
        return

    room = get_owned_temp_room(interaction.user)
    if room is None:
        await interaction.response.send_message("Ты должен быть владельцем временной комнаты.", ephemeral=True)
        return

    overwrite = room.overwrites_for(interaction.guild.default_role)
    overwrite.connect = False
    await room.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason="room_lock")
    await interaction.response.send_message("Комната закрыта.", ephemeral=True)


@tree.command(name="room_unlock", description="Открыть свою временную voice-комнату")
async def room_unlock(interaction: discord.Interaction) -> None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда только для сервера.", ephemeral=True)
        return

    room = get_owned_temp_room(interaction.user)
    if room is None:
        await interaction.response.send_message("Ты должен быть владельцем временной комнаты.", ephemeral=True)
        return

    overwrite = room.overwrites_for(interaction.guild.default_role)
    overwrite.connect = None
    await room.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason="room_unlock")
    await interaction.response.send_message("Комната открыта.", ephemeral=True)


@tree.command(name="room_rename", description="Переименовать свою временную voice-комнату")
async def room_rename(interaction: discord.Interaction, name: str) -> None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда только для сервера.", ephemeral=True)
        return

    room = get_owned_temp_room(interaction.user)
    if room is None:
        await interaction.response.send_message("Ты должен быть владельцем временной комнаты.", ephemeral=True)
        return

    await room.edit(name=name[:90], reason="room_rename")
    await interaction.response.send_message("Комната переименована.", ephemeral=True)


@tree.command(name="room_limit", description="Установить лимит пользователей в своей временной voice-комнате")
async def room_limit(interaction: discord.Interaction, limit: app_commands.Range[int, 0, 99]) -> None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда только для сервера.", ephemeral=True)
        return

    room = get_owned_temp_room(interaction.user)
    if room is None:
        await interaction.response.send_message("Ты должен быть владельцем временной комнаты.", ephemeral=True)
        return

    await room.edit(user_limit=limit, reason="room_limit")
    await interaction.response.send_message("Лимит обновлен.", ephemeral=True)


# -----------------------------
# Scheduler
# -----------------------------


def parse_utc_datetime(value: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


@tree.command(name="schedule_reminder", description="Одноразовое напоминание через N минут")
async def schedule_reminder(
    interaction: discord.Interaction,
    minutes: app_commands.Range[int, 1, 10080],
    message: str,
    channel: Optional[discord.TextChannel] = None,
) -> None:
    if not await ensure_admin(interaction):
        return

    target = channel or interaction.channel
    if not isinstance(target, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message("Нужен текстовый канал.", ephemeral=True)
        return

    next_run = int((utcnow() + timedelta(minutes=minutes)).timestamp())
    schedule_id = store.add_schedule(
        interaction.guild_id,
        target.id,
        message,
        next_run,
        interval_seconds=None,
        created_by=interaction.user.id,
    )
    await interaction.response.send_message(f"Напоминание создано. ID: {schedule_id}", ephemeral=True)


@tree.command(name="schedule_every", description="Повторять объявление каждые N минут")
async def schedule_every(
    interaction: discord.Interaction,
    minutes: app_commands.Range[int, 1, 10080],
    message: str,
    channel: discord.TextChannel,
) -> None:
    if not await ensure_admin(interaction):
        return

    interval = int(timedelta(minutes=minutes).total_seconds())
    next_run = int((utcnow() + timedelta(minutes=minutes)).timestamp())
    schedule_id = store.add_schedule(
        interaction.guild_id,
        channel.id,
        message,
        next_run,
        interval_seconds=interval,
        created_by=interaction.user.id,
    )
    await interaction.response.send_message(f"Периодическое объявление создано. ID: {schedule_id}", ephemeral=True)


@tree.command(name="schedule_at", description="Создать событие на конкретное UTC-время")
@app_commands.describe(when_utc="Формат: YYYY-MM-DD HH:MM (UTC)")
async def schedule_at(
    interaction: discord.Interaction,
    when_utc: str,
    message: str,
    channel: discord.TextChannel,
) -> None:
    if not await ensure_admin(interaction):
        return

    dt = parse_utc_datetime(when_utc)
    if dt is None:
        await interaction.response.send_message("Неверный формат даты. Используй YYYY-MM-DD HH:MM (UTC).", ephemeral=True)
        return
    if dt <= utcnow():
        await interaction.response.send_message("Время должно быть в будущем.", ephemeral=True)
        return

    schedule_id = store.add_schedule(
        interaction.guild_id,
        channel.id,
        message,
        int(dt.timestamp()),
        interval_seconds=None,
        created_by=interaction.user.id,
    )
    await interaction.response.send_message(f"Событие создано. ID: {schedule_id}", ephemeral=True)


@tree.command(name="schedule_list", description="Список активных расписаний")
async def schedule_list(interaction: discord.Interaction) -> None:
    if not await ensure_admin(interaction):
        return

    rows = store.list_schedules(interaction.guild_id)
    if not rows:
        await interaction.response.send_message("Активных расписаний нет.", ephemeral=True)
        return

    lines = []
    for row in rows:
        next_dt = datetime.fromtimestamp(int(row["next_run_at"]), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        interval = row["interval_seconds"]
        interval_text = f"every {int(interval) // 60}m" if interval else "once"
        lines.append(f"#{row['id']} | {interval_text} | {next_dt} | ch={row['channel_id']} | {row['content'][:40]}")

    await interaction.response.send_message(f"```\n{'\n'.join(lines)}\n```", ephemeral=True)


@tree.command(name="schedule_remove", description="Отключить расписание по ID")
async def schedule_remove(interaction: discord.Interaction, schedule_id: int) -> None:
    if not await ensure_admin(interaction):
        return

    row = store.get_schedule(interaction.guild_id, schedule_id)
    if row is None:
        await interaction.response.send_message("Schedule не найден.", ephemeral=True)
        return

    store.remove_schedule(interaction.guild_id, schedule_id)
    await interaction.response.send_message("Schedule отключен.", ephemeral=True)


@tasks.loop(seconds=30)
async def schedule_runner() -> None:
    now_ts = int(utcnow().timestamp())
    rows = store.get_due_schedules(now_ts)

    for row in rows:
        guild = client.get_guild(int(row["guild_id"]))
        if guild is None:
            store.mark_schedule_ran(int(row["id"]), None)
            continue

        channel = guild.get_channel(int(row["channel_id"]))
        if channel is None or not isinstance(channel, (discord.TextChannel, discord.Thread)):
            store.mark_schedule_ran(int(row["id"]), None)
            continue

        try:
            await channel.send(str(row["content"]))
        except discord.HTTPException:
            pass

        interval = row["interval_seconds"]
        if interval is None:
            store.mark_schedule_ran(int(row["id"]), None)
        else:
            next_run = int(row["next_run_at"]) + int(interval)
            while next_run <= now_ts:
                next_run += int(interval)
            store.mark_schedule_ran(int(row["id"]), next_run)


# -----------------------------
# Backups
# -----------------------------


@tree.command(name="backup_create", description="Создать резервную копию конфигов/данных сервера")
async def backup_create(interaction: discord.Interaction) -> None:
    if not await ensure_admin(interaction):
        return
    if interaction.guild is None:
        await interaction.response.send_message("Команда только для сервера.", ephemeral=True)
        return

    payload = {
        "guild_id": interaction.guild.id,
        "created_at": iso_now(),
        "data": store.backup_guild_data(interaction.guild.id),
    }

    raw = json.dumps(payload, ensure_ascii=False, indent=2)
    file_name = f"backup-guild-{interaction.guild.id}-{int(utcnow().timestamp())}.json"
    file = discord.File(io.BytesIO(raw.encode("utf-8")), filename=file_name)

    backup_channel_id = get_id_setting(interaction.guild.id, "backup_channel_id")
    if backup_channel_id:
        channel = interaction.guild.get_channel(backup_channel_id)
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            try:
                await channel.send(content=f"Backup by <@{interaction.user.id}>", file=file)
            except discord.HTTPException:
                pass

    file_for_user = discord.File(io.BytesIO(raw.encode("utf-8")), filename=file_name)
    await interaction.response.send_message("Backup создан.", file=file_for_user, ephemeral=True)


@tree.command(name="backup_restore", description="Восстановить конфиги/данные из backup JSON")
async def backup_restore(interaction: discord.Interaction, file: discord.Attachment) -> None:
    if not await ensure_admin(interaction):
        return
    if interaction.guild is None:
        await interaction.response.send_message("Команда только для сервера.", ephemeral=True)
        return

    if not file.filename.lower().endswith(".json"):
        await interaction.response.send_message("Нужен JSON файл backup-а.", ephemeral=True)
        return

    raw = await file.read()
    try:
        payload = json.loads(raw.decode("utf-8"))
        data = payload["data"]
    except (ValueError, KeyError):
        await interaction.response.send_message("Некорректный backup формат.", ephemeral=True)
        return

    store.restore_guild_data(interaction.guild.id, data)
    await interaction.response.send_message("Backup восстановлен. Перезапусти бота для полной синхронизации runtime-состояния.", ephemeral=True)


# -----------------------------
# Voice sitter
# -----------------------------


async def connect_or_move_to_target_channel() -> None:
    async with connect_lock:
        try:
            channel = client.get_channel(VOICE_CHANNEL_ID)
            if channel is None:
                channel = await client.fetch_channel(VOICE_CHANNEL_ID)
        except discord.Forbidden:
            logger.error(
                "Нет доступа к каналу %s (Missing Access). Проверь права View Channel/Connect.",
                VOICE_CHANNEL_ID,
            )
            return
        except discord.NotFound:
            logger.error("Канал с ID %s не найден. Проверь VOICE_CHANNEL_ID.", VOICE_CHANNEL_ID)
            return
        except discord.HTTPException:
            logger.exception("Ошибка API Discord при получении канала %s", VOICE_CHANNEL_ID)
            return

        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            logger.error("VOICE_CHANNEL_ID должен указывать на voice/stage канал.")
            return

        existing = discord.utils.get(client.voice_clients, guild=channel.guild)

        if existing and existing.is_connected():
            if existing.channel and existing.channel.id == channel.id:
                return
            await existing.move_to(channel)
            return

        try:
            await channel.connect(reconnect=True, self_deaf=False, self_mute=False)
        except discord.ClientException as exc:
            if "Already connected" in str(exc):
                return
            logger.exception("ClientException при подключении к voice: %s", exc)
        except discord.HTTPException:
            logger.exception("Не удалось подключиться к voice-каналу")


@tasks.loop(seconds=30)
async def keep_connected() -> None:
    try:
        await connect_or_move_to_target_channel()
    except Exception:
        logger.exception("Не удалось проверить/восстановить voice-подключение")


# -----------------------------
# Events: anti-raid, automod, temp voice
# -----------------------------


@client.event
async def on_member_join(member: discord.Member) -> None:
    if member.bot:
        return

    guild = member.guild
    window = join_windows[guild.id]
    now = utcnow()
    window.append(now)

    cutoff = now - timedelta(minutes=1)
    while window and window[0] < cutoff:
        window.popleft()

    limit = store.get_int_setting(guild.id, "raid_join_limit")
    if len(window) <= limit:
        return

    await apply_auto_timeout(
        guild,
        member,
        minutes=30,
        reason="Anti-raid: join spike",
        action_name="anti_raid_join",
        metadata={"joins_last_minute": len(window), "limit": limit},
    )
    await send_alert(
        guild,
        f"Join spike: {len(window)} входов/мин (лимит {limit}). Новый участник <@{member.id}> получил timeout.",
    )


@client.event
async def on_guild_channel_create(channel: discord.abc.GuildChannel) -> None:
    guild = channel.guild
    now = utcnow()

    window = channel_create_windows[guild.id]
    window.append(now)
    cutoff = now - timedelta(minutes=1)
    while window and window[0] < cutoff:
        window.popleft()

    limit = store.get_int_setting(guild.id, "raid_channel_create_limit")
    if len(window) <= limit:
        return

    offender = await fetch_audit_executor(guild, discord.AuditLogAction.channel_create, channel.id)
    offender_id = offender.id if offender else None

    if offender is not None:
        await apply_auto_timeout(
            guild,
            offender,
            minutes=30,
            reason="Anti-nuke: channel create spike",
            action_name="anti_nuke_channel_create",
            metadata={"count": len(window), "limit": limit, "channel_id": channel.id},
        )

    try:
        await channel.delete(reason="Anti-nuke: auto cleanup")
    except discord.HTTPException:
        pass

    await send_alert(
        guild,
        f"Anti-nuke: всплеск создания каналов ({len(window)}/мин). Offender: <@{offender_id}>" if offender_id else f"Anti-nuke: всплеск создания каналов ({len(window)}/мин).",
    )


@client.event
async def on_guild_role_create(role: discord.Role) -> None:
    guild = role.guild
    now = utcnow()

    window = role_create_windows[guild.id]
    window.append(now)
    cutoff = now - timedelta(minutes=1)
    while window and window[0] < cutoff:
        window.popleft()

    limit = store.get_int_setting(guild.id, "raid_role_create_limit")
    if len(window) <= limit:
        return

    offender = await fetch_audit_executor(guild, discord.AuditLogAction.role_create, role.id)
    offender_id = offender.id if offender else None

    if offender is not None:
        await apply_auto_timeout(
            guild,
            offender,
            minutes=30,
            reason="Anti-nuke: role create spike",
            action_name="anti_nuke_role_create",
            metadata={"count": len(window), "limit": limit, "role_id": role.id},
        )

    try:
        await role.delete(reason="Anti-nuke: auto cleanup")
    except discord.HTTPException:
        pass

    await send_alert(
        guild,
        f"Anti-nuke: всплеск создания ролей ({len(window)}/мин). Offender: <@{offender_id}>" if offender_id else f"Anti-nuke: всплеск создания ролей ({len(window)}/мин).",
    )


@client.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or message.guild is None:
        return

    if await maybe_handle_owner_panel_flow_message(message):
        return

    if await maybe_handle_mass_mentions(message):
        return

    await check_automod(message)


async def maybe_create_temp_room(member: discord.Member, after: discord.VoiceState) -> None:
    if after.channel is None or not isinstance(after.channel, discord.VoiceChannel):
        return

    lobby_id = get_id_setting(member.guild.id, "temp_voice_lobby_id")
    if lobby_id is None or after.channel.id != lobby_id:
        return

    category_id = get_id_setting(member.guild.id, "temp_voice_category_id")
    category = member.guild.get_channel(category_id) if category_id else after.channel.category
    if category is not None and not isinstance(category, discord.CategoryChannel):
        category = None

    overwrites = {
        member.guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=False),
        member: discord.PermissionOverwrite(
            view_channel=True,
            connect=True,
            speak=True,
            stream=True,
            use_soundboard=True,
            move_members=True,
            mute_members=True,
            deafen_members=True,
            manage_channels=True,
        ),
    }

    me = member.guild.me
    if me is not None:
        overwrites[me] = discord.PermissionOverwrite(
            view_channel=True,
            connect=True,
            speak=True,
            stream=True,
            use_soundboard=True,
            move_members=True,
            manage_channels=True,
        )

    room_name = f"room-{member.display_name}"[:90]

    try:
        room = await member.guild.create_voice_channel(
            name=room_name,
            category=category,
            overwrites=overwrites,
            reason="Temp room create",
        )
    except discord.HTTPException:
        return

    store.add_temp_room(member.guild.id, room.id, member.id)

    try:
        await member.move_to(room, reason="Temp room move")
    except discord.HTTPException:
        pass


async def maybe_cleanup_temp_room(before: discord.VoiceState) -> None:
    if before.channel is None or not isinstance(before.channel, discord.VoiceChannel):
        return

    owner_id = store.get_temp_room_owner(before.channel.id)
    if owner_id is None:
        return

    if before.channel.members:
        return

    store.remove_temp_room(before.channel.id)
    try:
        await before.channel.delete(reason="Temp room empty")
    except discord.HTTPException:
        pass


@client.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    # Keep bot in target voice channel.
    if client.user and member.id == client.user.id:
        if before.channel is not None and after.channel is None:
            await asyncio.sleep(3)
            await connect_or_move_to_target_channel()
        return

    if member.bot:
        return

    await maybe_create_temp_room(member, after)
    await maybe_cleanup_temp_room(before)


@client.event
async def on_ready() -> None:
    logger.info("Бот запущен как %s (id=%s)", client.user, client.user.id if client.user else "unknown")

    if not USE_PRIVILEGED_INTENTS:
        logger.warning(
            "Запуск в ограниченном режиме (USE_PRIVILEGED_INTENTS=0): anti-raid join-spike и часть AutoMod (контент сообщений) будут ограничены."
        )

    await ensure_views_registered()
    await sync_app_commands()

    if not schedule_runner.is_running():
        schedule_runner.start()

    try:
        await connect_or_move_to_target_channel()
    except Exception:
        logger.exception("Не удалось подключиться к voice-каналу в on_ready")

    if not keep_connected.is_running():
        keep_connected.start()


def validate_env() -> None:
    if not BOT_TOKEN:
        raise ValueError("Переменная BOT_TOKEN не задана.")


def ensure_data_dir() -> None:
    db_file = Path(DB_PATH)
    if db_file.parent and str(db_file.parent) not in ("", "."):
        db_file.parent.mkdir(parents=True, exist_ok=True)


def is_retryable_start_error(exc: Exception) -> bool:
    if isinstance(exc, (aiohttp.ClientError, asyncio.TimeoutError, ConnectionResetError, OSError, discord.GatewayNotFound)):
        return True

    if isinstance(exc, AttributeError) and "'NoneType' object has no attribute 'sequence'" in str(exc):
        return True

    text = str(exc).lower()
    network_markers = (
        "cannot connect to host gateway.discord.gg",
        "cannot connect to host discord.com",
        "clientconnectorerror",
        "connection reset",
        "gateway",
    )
    return any(marker in text for marker in network_markers)


def main() -> None:
    validate_env()
    ensure_data_dir()
    retry_delay = 1

    while True:
        try:
            client.run(BOT_TOKEN, log_handler=None)
            logger.warning("Discord client остановлен без исключения. Завершаю процесс.")
            return
        except KeyboardInterrupt:
            logger.info("Остановка по Ctrl+C")
            return
        except discord.LoginFailure:
            logger.error("Неверный BOT_TOKEN. Проверь токен в .env")
            raise
        except discord.PrivilegedIntentsRequired:
            logger.error(
                "Не включены privileged intents в Discord Developer Portal. Включи SERVER MEMBERS INTENT и MESSAGE CONTENT INTENT для полного функционала."
            )
            logger.error(
                "Временный обход: добавь USE_PRIVILEGED_INTENTS=0 в .env для ограниченного режима без этих intents."
            )
            raise
        except Exception as exc:
            if not is_retryable_start_error(exc):
                logger.exception("Фатальная ошибка старта бота")
                raise

            logger.warning(
                "Сбой подключения к Discord gateway (%s). Повтор через %sс.",
                exc.__class__.__name__,
                retry_delay,
            )
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)


if __name__ == "__main__":
    main()
