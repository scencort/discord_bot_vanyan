"""SQLite-хранилище — настройки, кейсы, варны, экономика и др."""

from __future__ import annotations

import json
import sqlite3
from typing import Optional

from .config import DEFAULTS_INT, iso_now


class Store:
    """Синхронное SQLite-хранилище (один процесс, одно подключение)."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    # ── Схема ────────────────────────────────────────────────────────
    def _init_schema(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            guild_id INTEGER NOT NULL,
            key      TEXT    NOT NULL,
            value    TEXT    NOT NULL,
            PRIMARY KEY (guild_id, key)
        );
        CREATE TABLE IF NOT EXISTS cases (
            case_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id     INTEGER NOT NULL,
            action       TEXT    NOT NULL,
            moderator_id INTEGER,
            target_id    INTEGER,
            reason       TEXT,
            created_at   TEXT    NOT NULL,
            metadata     TEXT,
            reverted     INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS warns (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id     INTEGER NOT NULL,
            user_id      INTEGER NOT NULL,
            moderator_id INTEGER,
            points       INTEGER NOT NULL,
            reason       TEXT,
            created_at   TEXT    NOT NULL,
            active       INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS warn_state (
            guild_id INTEGER NOT NULL,
            user_id  INTEGER NOT NULL,
            level    INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS automod_offenses (
            guild_id INTEGER NOT NULL,
            user_id  INTEGER NOT NULL,
            count    INTEGER NOT NULL DEFAULT 0,
            last_at  TEXT    NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS schedules (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id         INTEGER NOT NULL,
            channel_id       INTEGER NOT NULL,
            content          TEXT    NOT NULL,
            next_run_at      INTEGER NOT NULL,
            interval_seconds INTEGER,
            enabled          INTEGER NOT NULL DEFAULT 1,
            created_by       INTEGER,
            created_at       TEXT    NOT NULL
        );
        CREATE TABLE IF NOT EXISTS temp_rooms (
            channel_id INTEGER PRIMARY KEY,
            guild_id   INTEGER NOT NULL,
            owner_id   INTEGER NOT NULL,
            created_at TEXT    NOT NULL
        );
        CREATE TABLE IF NOT EXISTS economy_profiles (
            guild_id     INTEGER NOT NULL,
            user_id      INTEGER NOT NULL,
            balance      INTEGER NOT NULL DEFAULT 0,
            daily_last   TEXT,
            daily_streak INTEGER NOT NULL DEFAULT 0,
            total_duels  INTEGER NOT NULL DEFAULT 0,
            duel_wins    INTEGER NOT NULL DEFAULT 0,
            rps_wins     INTEGER NOT NULL DEFAULT 0,
            slots_wins   INTEGER NOT NULL DEFAULT 0,
            created_at   TEXT    NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS reports (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id    INTEGER NOT NULL,
            reporter_id INTEGER NOT NULL,
            target_id   INTEGER NOT NULL,
            reason      TEXT    NOT NULL,
            status      TEXT    NOT NULL DEFAULT 'open',
            created_at  TEXT    NOT NULL
        );
        CREATE TABLE IF NOT EXISTS shop_roles (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id   INTEGER NOT NULL,
            role_id    INTEGER NOT NULL,
            price      INTEGER NOT NULL,
            created_at TEXT    NOT NULL,
            UNIQUE(guild_id, role_id)
        );
        CREATE TABLE IF NOT EXISTS marriages (
            guild_id   INTEGER NOT NULL,
            user1_id   INTEGER NOT NULL,
            user2_id   INTEGER NOT NULL,
            created_at TEXT    NOT NULL,
            PRIMARY KEY (guild_id, user1_id),
            UNIQUE(guild_id, user2_id)
        );
        CREATE TABLE IF NOT EXISTS personal_roles (
            guild_id   INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            role_id    INTEGER NOT NULL,
            created_at TEXT    NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        );
        """)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ── Настройки ────────────────────────────────────────────────────
    def get(self, gid: int, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute(
            "SELECT value FROM settings WHERE guild_id=? AND key=?", (gid, key),
        ).fetchone()
        return str(row["value"]) if row else default

    def put(self, gid: int, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings(guild_id,key,value) VALUES(?,?,?) "
            "ON CONFLICT(guild_id,key) DO UPDATE SET value=excluded.value",
            (gid, key, value),
        )
        self.conn.commit()

    def get_int(self, gid: int, key: str) -> int:
        v = self.get(gid, key)
        if v is None:
            return DEFAULTS_INT[key]
        try:
            return int(v)
        except ValueError:
            return DEFAULTS_INT[key]

    def get_csv(self, gid: int, key: str) -> list[str]:
        raw = self.get(gid, key, "") or ""
        return [s.strip() for s in raw.split(",") if s.strip()]

    def put_csv(self, gid: int, key: str, values: list[str]) -> None:
        self.put(gid, key, ",".join(values))

    def get_id_set(self, gid: int, key: str) -> set[int]:
        out: set[int] = set()
        for s in self.get_csv(gid, key):
            try:
                out.add(int(s))
            except ValueError:
                pass
        return out

    def put_id_set(self, gid: int, key: str, ids: set[int]) -> None:
        self.put(gid, key, ",".join(str(i) for i in sorted(ids)))

    # ── Cases ────────────────────────────────────────────────────────
    def add_case(self, gid: int, action: str, mod_id: Optional[int],
                 target_id: Optional[int], reason: str,
                 meta: Optional[dict] = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO cases(guild_id,action,moderator_id,target_id,reason,created_at,metadata) "
            "VALUES(?,?,?,?,?,?,?)",
            (gid, action, mod_id, target_id, reason, iso_now(),
             json.dumps(meta or {}, ensure_ascii=False)),
        )
        self.conn.commit()
        return int(cur.lastrowid)  # type: ignore[arg-type]

    def get_case(self, gid: int, cid: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM cases WHERE guild_id=? AND case_id=?", (gid, cid),
        ).fetchone()

    def revert_case(self, gid: int, cid: int) -> None:
        self.conn.execute(
            "UPDATE cases SET reverted=1 WHERE guild_id=? AND case_id=?", (gid, cid),
        )
        self.conn.commit()

    # ── Warns ────────────────────────────────────────────────────────
    def add_warn(self, gid: int, uid: int, mod_id: Optional[int],
                 points: int, reason: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO warns(guild_id,user_id,moderator_id,points,reason,created_at,active) "
            "VALUES(?,?,?,?,?,?,1)",
            (gid, uid, mod_id, points, reason, iso_now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)  # type: ignore[arg-type]

    def get_warn(self, gid: int, wid: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM warns WHERE guild_id=? AND id=?", (gid, wid),
        ).fetchone()

    def deactivate_warn(self, gid: int, wid: int) -> None:
        self.conn.execute(
            "UPDATE warns SET active=0 WHERE guild_id=? AND id=?", (gid, wid),
        )
        self.conn.commit()

    def warn_total(self, gid: int, uid: int) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(points),0) AS t FROM warns "
            "WHERE guild_id=? AND user_id=? AND active=1", (gid, uid),
        ).fetchone()
        return int(row["t"]) if row else 0

    def list_warns(self, gid: int, uid: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM warns WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 20",
            (gid, uid),
        ).fetchall())

    def get_warn_level(self, gid: int, uid: int) -> int:
        row = self.conn.execute(
            "SELECT level FROM warn_state WHERE guild_id=? AND user_id=?", (gid, uid),
        ).fetchone()
        return int(row["level"]) if row else 0

    def set_warn_level(self, gid: int, uid: int, level: int) -> None:
        self.conn.execute(
            "INSERT INTO warn_state(guild_id,user_id,level) VALUES(?,?,?) "
            "ON CONFLICT(guild_id,user_id) DO UPDATE SET level=excluded.level",
            (gid, uid, level),
        )
        self.conn.commit()

    # ── AutoMod offenses ─────────────────────────────────────────────
    def inc_offense(self, gid: int, uid: int) -> int:
        row = self.conn.execute(
            "SELECT count FROM automod_offenses WHERE guild_id=? AND user_id=?",
            (gid, uid),
        ).fetchone()
        n = (int(row["count"]) + 1) if row else 1
        self.conn.execute(
            "INSERT INTO automod_offenses(guild_id,user_id,count,last_at) VALUES(?,?,?,?) "
            "ON CONFLICT(guild_id,user_id) DO UPDATE SET count=excluded.count, last_at=excluded.last_at",
            (gid, uid, n, iso_now()),
        )
        self.conn.commit()
        return n

    # ── Schedules ────────────────────────────────────────────────────
    def add_schedule(self, gid: int, ch_id: int, content: str,
                     next_run: int, interval: Optional[int], by: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO schedules(guild_id,channel_id,content,next_run_at,"
            "interval_seconds,enabled,created_by,created_at) VALUES(?,?,?,?,?,1,?,?)",
            (gid, ch_id, content, next_run, interval, by, iso_now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)  # type: ignore[arg-type]

    def due_schedules(self, ts: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM schedules WHERE enabled=1 AND next_run_at<=?", (ts,),
        ).fetchall())

    def list_schedules(self, gid: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM schedules WHERE guild_id=? AND enabled=1 "
            "ORDER BY next_run_at ASC LIMIT 25", (gid,),
        ).fetchall())

    def get_schedule(self, gid: int, sid: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM schedules WHERE guild_id=? AND id=?", (gid, sid),
        ).fetchone()

    def remove_schedule(self, gid: int, sid: int) -> None:
        self.conn.execute(
            "UPDATE schedules SET enabled=0 WHERE guild_id=? AND id=?", (gid, sid),
        )
        self.conn.commit()

    def mark_schedule_ran(self, sid: int, next_run: Optional[int]) -> None:
        if next_run is None:
            self.conn.execute("UPDATE schedules SET enabled=0 WHERE id=?", (sid,))
        else:
            self.conn.execute(
                "UPDATE schedules SET next_run_at=? WHERE id=?", (next_run, sid),
            )
        self.conn.commit()

    # ── Temp rooms ───────────────────────────────────────────────────
    def add_temp_room(self, gid: int, ch_id: int, owner: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO temp_rooms(channel_id,guild_id,owner_id,created_at) "
            "VALUES(?,?,?,?)", (ch_id, gid, owner, iso_now()),
        )
        self.conn.commit()

    def remove_temp_room(self, ch_id: int) -> None:
        self.conn.execute("DELETE FROM temp_rooms WHERE channel_id=?", (ch_id,))
        self.conn.commit()

    def temp_room_owner(self, ch_id: int) -> Optional[int]:
        row = self.conn.execute(
            "SELECT owner_id FROM temp_rooms WHERE channel_id=?", (ch_id,),
        ).fetchone()
        return int(row["owner_id"]) if row else None

    # ── Economy ──────────────────────────────────────────────────────
    def ensure_profile(self, gid: int, uid: int) -> None:
        self.conn.execute(
            "INSERT INTO economy_profiles(guild_id,user_id,balance,daily_last,daily_streak,"
            "total_duels,duel_wins,rps_wins,slots_wins,created_at) "
            "VALUES(?,?,0,NULL,0,0,0,0,0,?) ON CONFLICT(guild_id,user_id) DO NOTHING",
            (gid, uid, iso_now()),
        )
        self.conn.commit()

    def profile(self, gid: int, uid: int) -> sqlite3.Row:
        self.ensure_profile(gid, uid)
        row = self.conn.execute(
            "SELECT * FROM economy_profiles WHERE guild_id=? AND user_id=?",
            (gid, uid),
        ).fetchone()
        if row is None:
            raise RuntimeError("Не удалось получить профиль")
        return row

    def balance(self, gid: int, uid: int) -> int:
        return int(self.profile(gid, uid)["balance"])

    def add_balance(self, gid: int, uid: int, amount: int, floor: int = 0) -> int:
        p = self.profile(gid, uid)
        new = int(p["balance"]) + amount
        if new < floor:
            raise ValueError("Недостаточно средств")
        self.conn.execute(
            "UPDATE economy_profiles SET balance=? WHERE guild_id=? AND user_id=?",
            (new, gid, uid),
        )
        self.conn.commit()
        return new

    def transfer(self, gid: int, src: int, dst: int, amount: int) -> tuple[int, int]:
        if amount <= 0:
            raise ValueError("Сумма должна быть больше 0")
        if src == dst:
            raise ValueError("Нельзя переводить самому себе")
        if self.balance(gid, src) < amount:
            raise ValueError("Недостаточно средств")
        a = self.add_balance(gid, src, -amount)
        b = self.add_balance(gid, dst, amount)
        return a, b

    def set_daily(self, gid: int, uid: int, at: str, streak: int) -> None:
        self.profile(gid, uid)
        self.conn.execute(
            "UPDATE economy_profiles SET daily_last=?, daily_streak=? "
            "WHERE guild_id=? AND user_id=?", (at, streak, gid, uid),
        )
        self.conn.commit()

    def inc_counter(self, gid: int, uid: int, field: str, n: int = 1) -> None:
        allowed = {"total_duels", "duel_wins", "rps_wins", "slots_wins"}
        if field not in allowed:
            raise ValueError("Недопустимый счётчик")
        self.profile(gid, uid)
        self.conn.execute(
            f"UPDATE economy_profiles SET {field}={field}+? "
            "WHERE guild_id=? AND user_id=?", (n, gid, uid),
        )
        self.conn.commit()

    # ── Reports ──────────────────────────────────────────────────────
    def add_report(self, gid: int, reporter: int, target: int, reason: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO reports(guild_id,reporter_id,target_id,reason,status,created_at) "
            "VALUES(?,?,?,?,'open',?)", (gid, reporter, target, reason, iso_now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)  # type: ignore[arg-type]

    def report_count(self, gid: int, target: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM reports WHERE guild_id=? AND target_id=?",
            (gid, target),
        ).fetchone()
        return int(row["c"]) if row else 0

    # ── Shop ─────────────────────────────────────────────────────────
    def upsert_shop_role(self, gid: int, rid: int, price: int) -> None:
        self.conn.execute(
            "INSERT INTO shop_roles(guild_id,role_id,price,created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(guild_id,role_id) DO UPDATE SET price=excluded.price",
            (gid, rid, max(1, price), iso_now()),
        )
        self.conn.commit()

    def remove_shop_role(self, gid: int, rid: int) -> bool:
        cur = self.conn.execute(
            "DELETE FROM shop_roles WHERE guild_id=? AND role_id=?", (gid, rid),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def shop_role(self, gid: int, rid: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM shop_roles WHERE guild_id=? AND role_id=?", (gid, rid),
        ).fetchone()

    def list_shop(self, gid: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM shop_roles WHERE guild_id=? ORDER BY price ASC", (gid,),
        ).fetchall())

    # ── Marriages ────────────────────────────────────────────────────
    def marriage(self, gid: int, uid: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM marriages WHERE guild_id=? AND (user1_id=? OR user2_id=?)",
            (gid, uid, uid),
        ).fetchone()

    def marry(self, gid: int, a: int, b: int) -> bool:
        if a == b:
            return False
        if self.marriage(gid, a) or self.marriage(gid, b):
            return False
        first, second = sorted((a, b))
        self.conn.execute(
            "INSERT INTO marriages(guild_id,user1_id,user2_id,created_at) VALUES(?,?,?,?)",
            (gid, first, second, iso_now()),
        )
        self.conn.commit()
        return True

    def divorce(self, gid: int, uid: int) -> bool:
        cur = self.conn.execute(
            "DELETE FROM marriages WHERE guild_id=? AND (user1_id=? OR user2_id=?)",
            (gid, uid, uid),
        )
        self.conn.commit()
        return cur.rowcount > 0

    # ── Personal roles ───────────────────────────────────────────────
    def personal_role(self, gid: int, uid: int) -> Optional[int]:
        row = self.conn.execute(
            "SELECT role_id FROM personal_roles WHERE guild_id=? AND user_id=?",
            (gid, uid),
        ).fetchone()
        return int(row["role_id"]) if row else None

    def set_personal_role(self, gid: int, uid: int, rid: int) -> None:
        self.conn.execute(
            "INSERT INTO personal_roles(guild_id,user_id,role_id,created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(guild_id,user_id) DO UPDATE SET role_id=excluded.role_id",
            (gid, uid, rid, iso_now()),
        )
        self.conn.commit()

    def clear_personal_role(self, gid: int, uid: int) -> None:
        self.conn.execute(
            "DELETE FROM personal_roles WHERE guild_id=? AND user_id=?", (gid, uid),
        )
        self.conn.commit()

    # ── Top ──────────────────────────────────────────────────────────
    def top_profiles(self, gid: int, limit: int = 10) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM economy_profiles WHERE guild_id=? "
            "ORDER BY balance DESC LIMIT ?", (gid, max(1, limit)),
        ).fetchall())

    # ── Backup / Restore ─────────────────────────────────────────────
    def backup(self, gid: int) -> dict:
        data: dict[str, list[dict]] = {}
        tables = [
            "settings", "cases", "warns", "warn_state", "automod_offenses",
            "schedules", "temp_rooms", "economy_profiles", "reports",
            "shop_roles", "marriages", "personal_roles",
        ]
        for t in tables:
            rows = self.conn.execute(
                f"SELECT * FROM {t} WHERE guild_id=?", (gid,),
            ).fetchall()
            data[t] = [dict(r) for r in rows]
        return data

    def restore(self, gid: int, data: dict) -> None:
        tables = [
            "settings", "cases", "warns", "warn_state", "automod_offenses",
            "schedules", "temp_rooms", "economy_profiles", "reports",
            "shop_roles", "marriages", "personal_roles",
        ]
        for t in tables:
            self.conn.execute(f"DELETE FROM {t} WHERE guild_id=?", (gid,))

        for r in data.get("settings", []):
            self.conn.execute(
                "INSERT INTO settings(guild_id,key,value) VALUES(?,?,?)",
                (gid, r["key"], r["value"]),
            )
        for r in data.get("cases", []):
            self.conn.execute(
                "INSERT INTO cases(case_id,guild_id,action,moderator_id,target_id,"
                "reason,created_at,metadata,reverted) VALUES(?,?,?,?,?,?,?,?,?)",
                (r.get("case_id"), gid, r.get("action"), r.get("moderator_id"),
                 r.get("target_id"), r.get("reason"), r.get("created_at"),
                 r.get("metadata"), r.get("reverted", 0)),
            )
        for r in data.get("warns", []):
            self.conn.execute(
                "INSERT INTO warns(id,guild_id,user_id,moderator_id,points,reason,"
                "created_at,active) VALUES(?,?,?,?,?,?,?,?)",
                (r.get("id"), gid, r.get("user_id"), r.get("moderator_id"),
                 r.get("points"), r.get("reason"), r.get("created_at"),
                 r.get("active", 1)),
            )
        for r in data.get("warn_state", []):
            self.conn.execute(
                "INSERT INTO warn_state(guild_id,user_id,level) VALUES(?,?,?)",
                (gid, r.get("user_id"), r.get("level", 0)),
            )
        for r in data.get("automod_offenses", []):
            self.conn.execute(
                "INSERT INTO automod_offenses(guild_id,user_id,count,last_at) VALUES(?,?,?,?)",
                (gid, r.get("user_id"), r.get("count", 0), r.get("last_at", iso_now())),
            )
        for r in data.get("schedules", []):
            self.conn.execute(
                "INSERT INTO schedules(id,guild_id,channel_id,content,next_run_at,"
                "interval_seconds,enabled,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (r.get("id"), gid, r.get("channel_id"), r.get("content"),
                 r.get("next_run_at"), r.get("interval_seconds"),
                 r.get("enabled", 1), r.get("created_by"), r.get("created_at", iso_now())),
            )
        for r in data.get("temp_rooms", []):
            self.conn.execute(
                "INSERT INTO temp_rooms(channel_id,guild_id,owner_id,created_at) VALUES(?,?,?,?)",
                (r.get("channel_id"), gid, r.get("owner_id"), r.get("created_at", iso_now())),
            )
        for r in data.get("economy_profiles", []):
            self.conn.execute(
                "INSERT INTO economy_profiles(guild_id,user_id,balance,daily_last,"
                "daily_streak,total_duels,duel_wins,rps_wins,slots_wins,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (gid, r.get("user_id"), r.get("balance", 0), r.get("daily_last"),
                 r.get("daily_streak", 0), r.get("total_duels", 0), r.get("duel_wins", 0),
                 r.get("rps_wins", 0), r.get("slots_wins", 0), r.get("created_at", iso_now())),
            )
        for r in data.get("reports", []):
            self.conn.execute(
                "INSERT INTO reports(id,guild_id,reporter_id,target_id,reason,status,"
                "created_at) VALUES(?,?,?,?,?,?,?)",
                (r.get("id"), gid, r.get("reporter_id"), r.get("target_id"),
                 r.get("reason", "—"), r.get("status", "open"), r.get("created_at", iso_now())),
            )
        for r in data.get("shop_roles", []):
            self.conn.execute(
                "INSERT INTO shop_roles(id,guild_id,role_id,price,created_at) VALUES(?,?,?,?,?)",
                (r.get("id"), gid, r.get("role_id"), r.get("price", 1),
                 r.get("created_at", iso_now())),
            )
        for r in data.get("marriages", []):
            self.conn.execute(
                "INSERT INTO marriages(guild_id,user1_id,user2_id,created_at) VALUES(?,?,?,?)",
                (gid, r.get("user1_id"), r.get("user2_id"), r.get("created_at", iso_now())),
            )
        for r in data.get("personal_roles", []):
            self.conn.execute(
                "INSERT INTO personal_roles(guild_id,user_id,role_id,created_at) VALUES(?,?,?,?)",
                (gid, r.get("user_id"), r.get("role_id"), r.get("created_at", iso_now())),
            )
        self.conn.commit()
