from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import os
import re
import sqlite3


SCHEMA_VERSION = 2
LEGACY_IMPORT_ALGORITHM_VERSION = 1
SQLITE_MAX_INTEGER = 9_223_372_036_854_775_807
MAX_DAILY_DURATION_MICROSECONDS = 86_400_000_000
UTC = timezone.utc
KST = timezone(timedelta(hours=9), name="KST")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_LEGACY_YEAR_PATTERN = re.compile(r"^(?P<year>[0-9]{4})년$")
_LEGACY_MONTH_PATTERN = re.compile(r"^(?P<month>[1-9]|1[0-2])월$")
_LEGACY_WEEK_PATTERN = re.compile(r"^(?P<week>[1-5])주차$")


def normalize_kst(value: datetime) -> datetime:
    """Return an aware KST datetime.

    Naive values are interpreted as KST so tests and legacy callers do not depend on
    the host machine's local timezone.
    """

    if value.tzinfo is None:
        return value.replace(tzinfo=KST)
    return value.astimezone(KST)


def datetime_to_epoch_microseconds(value: datetime) -> int:
    delta = normalize_kst(value).astimezone(UTC) - _EPOCH
    return (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )


def epoch_microseconds_to_kst(value: int) -> datetime:
    return (_EPOCH + timedelta(microseconds=value)).astimezone(KST)


def split_interval_by_kst_date(start_us: int, end_us: int):
    """Split the half-open UTC interval into (KST date, microseconds) rows."""

    if end_us <= start_us:
        return []

    rows = []
    cursor_us = start_us
    while cursor_us < end_us:
        cursor = epoch_microseconds_to_kst(cursor_us)
        next_midnight = datetime.combine(
            cursor.date() + timedelta(days=1),
            time.min,
            tzinfo=KST,
        )
        boundary_us = min(end_us, datetime_to_epoch_microseconds(next_midnight))
        rows.append((cursor.date(), boundary_us - cursor_us))
        cursor_us = boundary_us
    return rows


def _next_month_start(year: int, month: int) -> date:
    if year == 9999 and month == 12:
        raise ValueError("9999년 12월은 가져올 수 없습니다.")
    if month == 12:
        return date(year + 1, 1, 1)
    return date(year, month + 1, 1)


def legacy_week_range(year: int, month: int, week_number: int):
    """Return the Monday-based range represented by a legacy N주차 key."""

    first_day = date(year, month, 1)
    first_thursday = first_day + timedelta(
        days=(3 - first_day.weekday() + 7) % 7
    )
    target_thursday = first_thursday + timedelta(weeks=week_number - 1)
    if target_thursday.month != month:
        raise ValueError(
            f"{year}년 {month}월에는 {week_number}주차가 없습니다."
        )
    start_date = target_thursday - timedelta(days=3)
    return start_date, start_date + timedelta(days=7)


def _legacy_duration_microseconds(value, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{location}은 0 이상의 정수 초여야 합니다.")
    if value > SQLITE_MAX_INTEGER // 1_000_000:
        raise ValueError(f"{location}이 SQLite 정수 범위를 초과합니다.")
    return value * 1_000_000


def _append_legacy_period_rows(
    rows,
    period_data,
    *,
    period_kind,
    period_start,
    period_end,
    source_kind,
    location,
):
    if not isinstance(period_data, dict):
        raise ValueError(f"{location} 값은 객체여야 합니다.")
    for legacy_user_id, user_data in period_data.items():
        if not isinstance(legacy_user_id, str) or not legacy_user_id:
            raise ValueError(f"{location}의 사용자 키가 올바르지 않습니다.")
        if not isinstance(user_data, dict):
            raise ValueError(f"{location}의 {legacy_user_id} 기록은 객체여야 합니다.")
        nickname_key = "name" if source_kind == "carryover" else "nickname"
        nickname = user_data.get(nickname_key)
        if not isinstance(nickname, str):
            raise ValueError(
                f"{location}의 {legacy_user_id} {nickname_key}은 문자열이어야 합니다."
            )
        time_key = "time_week" if period_kind == "week" and source_kind == "carryover" else (
            "time_month" if source_kind == "carryover" else "time"
        )
        duration = _legacy_duration_microseconds(
            user_data.get(time_key),
            f"{location}의 {legacy_user_id} {time_key}",
        )
        rows.append(
            (
                period_kind,
                period_start.isoformat(),
                period_end.isoformat(),
                legacy_user_id,
                nickname,
                duration,
                source_kind,
            )
        )


def normalize_legacy_period_rows(payload, cutover_at):
    """Convert legacy aggregates to non-overlapping reporting bases.

    Weekly and monthly values intentionally remain separate.  The old JSON has no
    daily facts, so assigning its aggregates to invented dates would corrupt either
    weekly or monthly reports.
    """

    if not isinstance(payload, dict):
        raise ValueError("최상위 JSON 값은 객체여야 합니다.")
    rows = []
    for year_key, year_data in payload.items():
        if year_key == "_in_progress":
            continue
        year_match = _LEGACY_YEAR_PATTERN.fullmatch(year_key)
        if year_match is None or not isinstance(year_data, dict):
            raise ValueError(f"기존 연도 키가 올바르지 않습니다: {year_key!r}")
        year = int(year_match.group("year"))
        for month_key, month_data in year_data.items():
            month_match = _LEGACY_MONTH_PATTERN.fullmatch(month_key)
            if month_match is None or not isinstance(month_data, dict):
                raise ValueError(
                    f"기존 월 키가 올바르지 않습니다: {year_key} {month_key!r}"
                )
            month = int(month_match.group("month"))
            month_start = date(year, month, 1)
            month_end = _next_month_start(year, month)
            for period_key, period_data in month_data.items():
                location = f"{year_key} {month_key} {period_key}"
                if period_key == "total":
                    period_kind = "month"
                    period_start, period_end = month_start, month_end
                else:
                    week_match = _LEGACY_WEEK_PATTERN.fullmatch(period_key)
                    if week_match is None:
                        raise ValueError(f"기존 기간 키가 올바르지 않습니다: {location}")
                    period_kind = "week"
                    period_start, period_end = legacy_week_range(
                        year,
                        month,
                        int(week_match.group("week")),
                    )
                _append_legacy_period_rows(
                    rows,
                    period_data,
                    period_kind=period_kind,
                    period_start=period_start,
                    period_end=period_end,
                    source_kind="finalized",
                    location=location,
                )

    in_progress = payload.get("_in_progress", {})
    if not isinstance(in_progress, dict):
        raise ValueError("_in_progress 값은 객체여야 합니다.")
    cutover = normalize_kst(cutover_at)
    cutover_date = cutover.date()
    week_start = cutover_date - timedelta(days=cutover_date.weekday())
    month_start = cutover_date.replace(day=1)
    for period_kind, period_start, period_end in (
        ("week", week_start, week_start + timedelta(days=7)),
        ("month", month_start, _next_month_start(month_start.year, month_start.month)),
    ):
        _append_legacy_period_rows(
            rows,
            in_progress,
            period_kind=period_kind,
            period_start=period_start,
            period_end=period_end,
            source_kind="carryover",
            location="_in_progress",
        )
    return rows


class VoiceStatsStore:
    """SQLite-backed daily voice-time ledger.

    Daily rows are the sole source for new weekly, monthly, and annual totals.
    Imported legacy aggregates remain separate because they contain no daily facts.
    Active-session checkpoints and daily increments are committed together.
    """

    def __init__(self, database_file="voice_time.db"):
        if sqlite3.sqlite_version_info < (3, 24, 0):
            raise RuntimeError("SQLite 3.24 이상이 필요합니다.")
        self.database_file = os.fspath(database_file)
        if self.database_file == ":memory:":
            raise ValueError(":memory: 대신 파일 기반 SQLite 경로를 사용해 주세요.")
        parent = os.path.dirname(os.path.abspath(self.database_file))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(
            self.database_file,
            timeout=5,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self):
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute("BEGIN IMMEDIATE")
            current_version = connection.execute("PRAGMA user_version").fetchone()[0]
            if current_version > SCHEMA_VERSION:
                raise RuntimeError(
                    "데이터베이스 버전이 현재 봇보다 최신입니다: "
                    f"{current_version} > {SCHEMA_VERSION}"
                )

            if current_version < 1:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS member_profiles (
                        guild_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        nickname TEXT NOT NULL,
                        updated_at_us INTEGER NOT NULL,
                        PRIMARY KEY (guild_id, user_id)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS daily_voice_times (
                        guild_id TEXT NOT NULL,
                        activity_date TEXT NOT NULL
                            CHECK (
                                activity_date = date(activity_date)
                                AND length(activity_date) = 10
                            ),
                        user_id TEXT NOT NULL,
                        duration_microseconds INTEGER NOT NULL
                            CHECK (
                                typeof(duration_microseconds) = 'integer'
                                AND duration_microseconds >= 0
                                AND duration_microseconds <= 86400000000
                            ),
                        updated_at_us INTEGER NOT NULL,
                        PRIMARY KEY (guild_id, activity_date, user_id),
                        FOREIGN KEY (guild_id, user_id)
                            REFERENCES member_profiles (guild_id, user_id)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_daily_voice_member_date
                    ON daily_voice_times (guild_id, user_id, activity_date)
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS active_voice_sessions (
                        guild_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        started_at_us INTEGER NOT NULL,
                        accounted_through_us INTEGER NOT NULL,
                        PRIMARY KEY (guild_id, user_id),
                        CHECK (accounted_through_us >= started_at_us),
                        FOREIGN KEY (guild_id, user_id)
                            REFERENCES member_profiles (guild_id, user_id)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS legacy_json_archives (
                        guild_id TEXT PRIMARY KEY,
                        source_name TEXT NOT NULL,
                        source_sha256 TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        imported_at_us INTEGER NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY,
                        applied_at_us INTEGER NOT NULL
                    )
                    """
                )
                applied_at_us = datetime_to_epoch_microseconds(datetime.now(KST))
                connection.execute(
                    """
                    INSERT OR IGNORE INTO schema_migrations(version, applied_at_us)
                    VALUES (?, ?)
                    """,
                    (1, applied_at_us),
                )
                current_version = 1
                connection.execute("PRAGMA user_version = 1")

            if current_version < 2:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS legacy_imports (
                        guild_id TEXT PRIMARY KEY,
                        source_name TEXT NOT NULL,
                        source_sha256 TEXT NOT NULL,
                        algorithm_version INTEGER NOT NULL,
                        cutover_at_us INTEGER NOT NULL,
                        imported_at_us INTEGER NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS legacy_period_totals (
                        guild_id TEXT NOT NULL,
                        period_kind TEXT NOT NULL
                            CHECK (period_kind IN ('week', 'month')),
                        period_start TEXT NOT NULL
                            CHECK (
                                period_start = date(period_start)
                                AND length(period_start) = 10
                            ),
                        period_end TEXT NOT NULL
                            CHECK (
                                period_end = date(period_end)
                                AND length(period_end) = 10
                                AND period_end > period_start
                            ),
                        legacy_user_id TEXT NOT NULL,
                        nickname TEXT NOT NULL,
                        duration_microseconds INTEGER NOT NULL
                            CHECK (
                                typeof(duration_microseconds) = 'integer'
                                AND duration_microseconds >= 0
                            ),
                        source_kind TEXT NOT NULL
                            CHECK (source_kind IN ('finalized', 'carryover')),
                        PRIMARY KEY (
                            guild_id,
                            period_kind,
                            period_start,
                            legacy_user_id,
                            source_kind
                        ),
                        FOREIGN KEY (guild_id)
                            REFERENCES legacy_imports (guild_id)
                            ON DELETE CASCADE
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_legacy_period_range
                    ON legacy_period_totals (
                        guild_id,
                        period_kind,
                        period_start,
                        period_end
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS legacy_user_aliases (
                        guild_id TEXT NOT NULL,
                        legacy_user_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        updated_at_us INTEGER NOT NULL,
                        PRIMARY KEY (guild_id, legacy_user_id),
                        FOREIGN KEY (guild_id)
                            REFERENCES legacy_imports (guild_id)
                            ON DELETE CASCADE,
                        FOREIGN KEY (guild_id, user_id)
                            REFERENCES member_profiles (guild_id, user_id)
                    )
                    """
                )
                applied_at_us = datetime_to_epoch_microseconds(datetime.now(KST))
                connection.execute(
                    """
                    INSERT OR IGNORE INTO schema_migrations(version, applied_at_us)
                    VALUES (?, ?)
                    """,
                    (2, applied_at_us),
                )
                current_version = 2
                connection.execute("PRAGMA user_version = 2")
            self._verify_schema(connection)

    @staticmethod
    def _verify_schema(connection):
        required_columns = {
            "member_profiles": {
                "guild_id",
                "user_id",
                "nickname",
                "updated_at_us",
            },
            "daily_voice_times": {
                "guild_id",
                "activity_date",
                "user_id",
                "duration_microseconds",
                "updated_at_us",
            },
            "active_voice_sessions": {
                "guild_id",
                "user_id",
                "started_at_us",
                "accounted_through_us",
            },
            "legacy_json_archives": {
                "guild_id",
                "source_name",
                "source_sha256",
                "payload_json",
                "imported_at_us",
            },
            "legacy_imports": {
                "guild_id",
                "source_name",
                "source_sha256",
                "algorithm_version",
                "cutover_at_us",
                "imported_at_us",
            },
            "legacy_period_totals": {
                "guild_id",
                "period_kind",
                "period_start",
                "period_end",
                "legacy_user_id",
                "nickname",
                "duration_microseconds",
                "source_kind",
            },
            "legacy_user_aliases": {
                "guild_id",
                "legacy_user_id",
                "user_id",
                "updated_at_us",
            },
            "schema_migrations": {"version", "applied_at_us"},
        }
        for table_name, expected in required_columns.items():
            actual = {
                row["name"]
                for row in connection.execute(
                    f'PRAGMA table_info("{table_name}")'
                ).fetchall()
            }
            if actual != expected:
                raise RuntimeError(
                    f"SQLite 스키마가 올바르지 않습니다: {table_name}"
                )

    @staticmethod
    def _upsert_profile(connection, guild_id, user_id, nickname, updated_at_us):
        connection.execute(
            """
            INSERT INTO member_profiles(guild_id, user_id, nickname, updated_at_us)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                nickname = excluded.nickname,
                updated_at_us = excluded.updated_at_us
            WHERE excluded.updated_at_us >= member_profiles.updated_at_us
            """,
            (guild_id, user_id, nickname, updated_at_us),
        )

    @staticmethod
    def _ensure_no_cutover_overlap(connection, guild_id, cutover):
        """Reject daily rows that cannot be separated from legacy carryover."""

        cutoff_date = cutover.date().isoformat()
        if cutover.time() == time.min:
            comparison = "<"
            boundary_description = "전환일 이전"
        else:
            comparison = "<="
            boundary_description = "전환일 당일 또는 이전"
        row = connection.execute(
            f"""
            SELECT activity_date
            FROM daily_voice_times
            WHERE guild_id = ? AND activity_date {comparison} ?
            ORDER BY activity_date DESC
            LIMIT 1
            """,
            (str(guild_id), cutoff_date),
        ).fetchone()
        if row is not None:
            raise ValueError(
                f"{boundary_description} 일일 기록({row['activity_date']})이 이미 있어 "
                "legacy 이월값과 겹칠 수 있습니다. 기존 봇을 멈춘 뒤 SQL 기록을 "
                "시작하기 전에 전환하세요."
            )

    @staticmethod
    def _upsert_daily(
        connection,
        guild_id,
        activity_date,
        user_id,
        duration_microseconds,
        updated_at_us,
    ):
        if duration_microseconds <= 0:
            return
        connection.execute(
            """
            INSERT INTO daily_voice_times(
                guild_id,
                activity_date,
                user_id,
                duration_microseconds,
                updated_at_us
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, activity_date, user_id) DO UPDATE SET
                duration_microseconds =
                    daily_voice_times.duration_microseconds
                    + excluded.duration_microseconds,
                updated_at_us = excluded.updated_at_us
            """,
            (
                guild_id,
                activity_date.isoformat(),
                user_id,
                duration_microseconds,
                updated_at_us,
            ),
        )

    def start_session(self, guild_id, user_id, nickname, started_at):
        guild_id = str(guild_id)
        user_id = str(user_id)
        started_at_us = datetime_to_epoch_microseconds(started_at)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._upsert_profile(
                connection,
                guild_id,
                user_id,
                nickname,
                started_at_us,
            )
            cursor = connection.execute(
                """
                INSERT INTO active_voice_sessions(
                    guild_id,
                    user_id,
                    started_at_us,
                    accounted_through_us
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO NOTHING
                """,
                (guild_id, user_id, started_at_us, started_at_us),
            )
            return cursor.rowcount == 1

    def checkpoint_sessions(self, guild_id, members, checkpoint_at):
        """Account all listed active members through one timestamp atomically."""

        guild_id = str(guild_id)
        checkpoint_at_us = datetime_to_epoch_microseconds(checkpoint_at)
        elapsed_by_user = {}
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for user_id, nickname in members:
                user_id = str(user_id)
                self._upsert_profile(
                    connection,
                    guild_id,
                    user_id,
                    nickname,
                    checkpoint_at_us,
                )
                row = connection.execute(
                    """
                    SELECT accounted_through_us
                    FROM active_voice_sessions
                    WHERE guild_id = ? AND user_id = ?
                    """,
                    (guild_id, user_id),
                ).fetchone()
                if row is None:
                    continue

                start_us = row["accounted_through_us"]
                end_us = max(start_us, checkpoint_at_us)
                for activity_date, duration_us in split_interval_by_kst_date(
                    start_us,
                    end_us,
                ):
                    self._upsert_daily(
                        connection,
                        guild_id,
                        activity_date,
                        user_id,
                        duration_us,
                        checkpoint_at_us,
                    )
                connection.execute(
                    """
                    UPDATE active_voice_sessions
                    SET accounted_through_us = ?
                    WHERE guild_id = ? AND user_id = ?
                    """,
                    (end_us, guild_id, user_id),
                )
                elapsed_by_user[user_id] = end_us - start_us
        return elapsed_by_user

    def end_session(self, guild_id, user_id, nickname, ended_at):
        guild_id = str(guild_id)
        user_id = str(user_id)
        ended_at_us = datetime_to_epoch_microseconds(ended_at)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._upsert_profile(
                connection,
                guild_id,
                user_id,
                nickname,
                ended_at_us,
            )
            row = connection.execute(
                """
                SELECT started_at_us, accounted_through_us
                FROM active_voice_sessions
                WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
            ).fetchone()
            if row is None:
                return 0

            if ended_at_us < row["started_at_us"]:
                return None

            start_us = row["accounted_through_us"]
            end_us = max(start_us, ended_at_us)
            for activity_date, duration_us in split_interval_by_kst_date(
                start_us,
                end_us,
            ):
                self._upsert_daily(
                    connection,
                    guild_id,
                    activity_date,
                    user_id,
                    duration_us,
                    ended_at_us,
                )
            connection.execute(
                """
                DELETE FROM active_voice_sessions
                WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
            )
            return end_us - start_us

    def reset_active_sessions(self, guild_id, members, started_at):
        """Conservatively reconcile persisted sessions after a process restart.

        Time after the last committed checkpoint is not guessed. Current members
        receive a fresh checkpoint at startup.
        """

        guild_id = str(guild_id)
        started_at_us = datetime_to_epoch_microseconds(started_at)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM active_voice_sessions WHERE guild_id = ?",
                (guild_id,),
            )
            for user_id, nickname in members:
                user_id = str(user_id)
                self._upsert_profile(
                    connection,
                    guild_id,
                    user_id,
                    nickname,
                    started_at_us,
                )
                connection.execute(
                    """
                    INSERT INTO active_voice_sessions(
                        guild_id,
                        user_id,
                        started_at_us,
                        accounted_through_us
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (guild_id, user_id, started_at_us, started_at_us),
                )

    def discard_session(self, guild_id, user_id):
        """Remove a session without guessing time after its last checkpoint."""

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                DELETE FROM active_voice_sessions
                WHERE guild_id = ? AND user_id = ?
                """,
                (str(guild_id), str(user_id)),
            )

    def aggregate(
        self,
        guild_id,
        start_date,
        end_date,
        *,
        legacy_period_kind=None,
    ):
        """Sum new daily facts and, when requested, one legacy reporting basis."""

        if legacy_period_kind not in (None, "week", "month"):
            raise ValueError("legacy_period_kind는 week 또는 month여야 합니다.")

        with self._connection() as connection:
            daily_start_date = start_date
            if legacy_period_kind is not None:
                legacy_import = connection.execute(
                    """
                    SELECT cutover_at_us
                    FROM legacy_imports
                    WHERE guild_id = ?
                    """,
                    (str(guild_id),),
                ).fetchone()
                if legacy_import is not None:
                    cutover_date = epoch_microseconds_to_kst(
                        legacy_import["cutover_at_us"]
                    ).date()
                    daily_start_date = max(daily_start_date, cutover_date)
            daily_rows = connection.execute(
                """
                SELECT
                    daily.user_id,
                    profiles.nickname,
                    SUM(daily.duration_microseconds) AS duration_microseconds
                FROM daily_voice_times AS daily
                JOIN member_profiles AS profiles
                  ON profiles.guild_id = daily.guild_id
                 AND profiles.user_id = daily.user_id
                WHERE daily.guild_id = ?
                  AND daily.activity_date >= ?
                  AND daily.activity_date < ?
                GROUP BY daily.user_id, profiles.nickname
                ORDER BY duration_microseconds DESC, daily.user_id ASC
                """,
                (str(guild_id), daily_start_date.isoformat(), end_date.isoformat()),
            ).fetchall()

            legacy_rows = []
            if legacy_period_kind is not None:
                legacy_rows = connection.execute(
                    """
                    SELECT
                        legacy.legacy_user_id,
                        aliases.user_id AS mapped_user_id,
                        COALESCE(profiles.nickname, legacy.nickname) AS nickname,
                        legacy.duration_microseconds
                    FROM legacy_period_totals AS legacy
                    LEFT JOIN legacy_user_aliases AS aliases
                      ON aliases.guild_id = legacy.guild_id
                     AND aliases.legacy_user_id = legacy.legacy_user_id
                    LEFT JOIN member_profiles AS profiles
                      ON profiles.guild_id = aliases.guild_id
                     AND profiles.user_id = aliases.user_id
                    WHERE legacy.guild_id = ?
                      AND legacy.period_kind = ?
                      AND legacy.period_start >= ?
                      AND legacy.period_end <= ?
                    ORDER BY legacy.period_start, legacy.legacy_user_id
                    """,
                    (
                        str(guild_id),
                        legacy_period_kind,
                        start_date.isoformat(),
                        end_date.isoformat(),
                    ),
                ).fetchall()

        totals = {}
        for row in daily_rows:
            totals[row["user_id"]] = {
                "duration_microseconds": row["duration_microseconds"],
                "nickname": row["nickname"],
            }
        for row in legacy_rows:
            user_id = row["mapped_user_id"]
            if user_id is None:
                user_id = f"legacy:{row['legacy_user_id']}"
            total = totals.setdefault(
                user_id,
                {"duration_microseconds": 0, "nickname": row["nickname"]},
            )
            total["duration_microseconds"] += row["duration_microseconds"]
            if row["mapped_user_id"] is not None:
                total["nickname"] = row["nickname"]

        return dict(
            sorted(
                totals.items(),
                key=lambda item: (-item[1]["duration_microseconds"], item[0]),
            )
        )

    def map_legacy_users(self, guild_id, mappings, *, overwrite=False):
        """Map legacy usernames, preserving automatic mappings unless explicit."""

        guild_id = str(guild_id)
        normalized = {}
        for legacy_user_id, user_id, nickname in mappings:
            legacy_user_id = str(legacy_user_id)
            user_id = str(user_id)
            if not legacy_user_id or not user_id or not isinstance(nickname, str):
                raise ValueError("legacy 사용자 매핑 값이 올바르지 않습니다.")
            previous = normalized.get(legacy_user_id)
            if previous is not None and previous[0] != user_id:
                raise ValueError(
                    f"legacy 사용자 {legacy_user_id!r}가 둘 이상의 ID와 연결됐습니다."
                )
            normalized[legacy_user_id] = (user_id, nickname)

        if not normalized:
            return 0
        updated_at_us = datetime_to_epoch_microseconds(datetime.now(KST))
        mapped = 0
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            has_import = connection.execute(
                "SELECT 1 FROM legacy_imports WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
            if has_import is None:
                return 0
            for legacy_user_id, (user_id, nickname) in normalized.items():
                existing_alias = connection.execute(
                    """
                    SELECT user_id
                    FROM legacy_user_aliases
                    WHERE guild_id = ? AND legacy_user_id = ?
                    """,
                    (guild_id, legacy_user_id),
                ).fetchone()
                if existing_alias is not None and not overwrite:
                    continue
                exists = connection.execute(
                    """
                    SELECT 1
                    FROM legacy_period_totals
                    WHERE guild_id = ? AND legacy_user_id = ?
                    LIMIT 1
                    """,
                    (guild_id, legacy_user_id),
                ).fetchone()
                if exists is None:
                    continue
                self._upsert_profile(
                    connection,
                    guild_id,
                    user_id,
                    nickname,
                    updated_at_us,
                )
                if overwrite:
                    cursor = connection.execute(
                        """
                        INSERT INTO legacy_user_aliases(
                            guild_id,
                            legacy_user_id,
                            user_id,
                            updated_at_us
                        )
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(guild_id, legacy_user_id) DO UPDATE SET
                            user_id = excluded.user_id,
                            updated_at_us = excluded.updated_at_us
                        """,
                        (guild_id, legacy_user_id, user_id, updated_at_us),
                    )
                else:
                    cursor = connection.execute(
                        """
                        INSERT INTO legacy_user_aliases(
                            guild_id,
                            legacy_user_id,
                            user_id,
                            updated_at_us
                        )
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(guild_id, legacy_user_id) DO NOTHING
                        """,
                        (guild_id, legacy_user_id, user_id, updated_at_us),
                    )
                mapped += cursor.rowcount
        return mapped

    def get_daily_records(self, guild_id):
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    daily.activity_date,
                    daily.user_id,
                    profiles.nickname,
                    daily.duration_microseconds
                FROM daily_voice_times AS daily
                JOIN member_profiles AS profiles
                  ON profiles.guild_id = daily.guild_id
                 AND profiles.user_id = daily.user_id
                WHERE daily.guild_id = ?
                ORDER BY daily.activity_date ASC, daily.user_id ASC
                """,
                (str(guild_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _normalize_daily_records(records):
        normalized = []
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                raise ValueError(f"records[{index}] 값은 객체여야 합니다.")
            raw_date = record.get("activity_date")
            if not isinstance(raw_date, str):
                raise ValueError(f"records[{index}]의 activity_date가 올바르지 않습니다.")
            try:
                parsed_date = date.fromisoformat(raw_date)
            except ValueError as error:
                raise ValueError(
                    f"records[{index}]의 activity_date가 올바르지 않습니다."
                ) from error
            if parsed_date.isoformat() != raw_date:
                raise ValueError(
                    f"records[{index}]의 activity_date는 YYYY-MM-DD 형식이어야 합니다."
                )

            user_id = record.get("user_id")
            nickname = record.get("nickname")
            duration = record.get("duration_microseconds")
            if not isinstance(user_id, str) or not user_id:
                raise ValueError(f"records[{index}]의 user_id가 올바르지 않습니다.")
            if not isinstance(nickname, str):
                raise ValueError(f"records[{index}]의 nickname이 올바르지 않습니다.")
            if (
                isinstance(duration, bool)
                or not isinstance(duration, int)
                or duration < 0
                or duration > MAX_DAILY_DURATION_MICROSECONDS
            ):
                raise ValueError(
                    f"records[{index}]의 duration_microseconds가 올바르지 않습니다."
                )
            normalized.append(
                {
                    "activity_date": raw_date,
                    "user_id": user_id,
                    "nickname": nickname,
                    "duration_microseconds": duration,
                }
            )
        return normalized

    def replace_daily_records(
        self,
        guild_id,
        records,
        legacy_payload=None,
        legacy_import=None,
        checkpoint_at=None,
    ):
        guild_id = str(guild_id)
        records = self._normalize_daily_records(records)
        if legacy_import is not None and legacy_payload is None:
            raise ValueError("legacy 변환 메타데이터에는 legacy_stats가 필요합니다.")
        updated_at_us = datetime_to_epoch_microseconds(datetime.now(KST))
        checkpoint_at_us = datetime_to_epoch_microseconds(
            checkpoint_at or datetime.now(KST)
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM daily_voice_times WHERE guild_id = ?",
                (guild_id,),
            )
            connection.execute(
                "DELETE FROM legacy_imports WHERE guild_id = ?",
                (guild_id,),
            )
            for record in records:
                user_id = str(record["user_id"])
                nickname = record["nickname"]
                self._upsert_profile(
                    connection,
                    guild_id,
                    user_id,
                    nickname,
                    updated_at_us,
                )
                connection.execute(
                    """
                    INSERT INTO daily_voice_times(
                        guild_id,
                        activity_date,
                        user_id,
                        duration_microseconds,
                        updated_at_us
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        guild_id,
                        record["activity_date"],
                        user_id,
                        record["duration_microseconds"],
                        updated_at_us,
                    ),
                )

            connection.execute(
                "DELETE FROM legacy_json_archives WHERE guild_id = ?",
                (guild_id,),
            )
            if legacy_payload is not None:
                payload_json = json.dumps(
                    legacy_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                self._insert_legacy_archive(
                    connection,
                    guild_id,
                    "uploaded_legacy_stats.json",
                    payload_json,
                    updated_at_us,
                )
            if legacy_import is not None:
                connection.execute(
                    """
                    INSERT INTO legacy_imports(
                        guild_id,
                        source_name,
                        source_sha256,
                        algorithm_version,
                        cutover_at_us,
                        imported_at_us
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        guild_id,
                        legacy_import["source_name"],
                        legacy_import["source_sha256"],
                        legacy_import["algorithm_version"],
                        legacy_import["cutover_at_us"],
                        updated_at_us,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO legacy_period_totals(
                        guild_id,
                        period_kind,
                        period_start,
                        period_end,
                        legacy_user_id,
                        nickname,
                        duration_microseconds,
                        source_kind
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (guild_id, *period_row)
                        for period_row in legacy_import["period_rows"]
                    ],
                )
                for legacy_user_id, user_id, nickname in legacy_import["aliases"]:
                    self._upsert_profile(
                        connection,
                        guild_id,
                        user_id,
                        nickname,
                        updated_at_us,
                    )
                    connection.execute(
                        """
                        INSERT INTO legacy_user_aliases(
                            guild_id,
                            legacy_user_id,
                            user_id,
                            updated_at_us
                        )
                        VALUES (?, ?, ?, ?)
                        """,
                        (guild_id, legacy_user_id, user_id, updated_at_us),
                    )
            connection.execute(
                """
                UPDATE active_voice_sessions
                SET accounted_through_us = MAX(accounted_through_us, ?)
                WHERE guild_id = ?
                """,
                (checkpoint_at_us, guild_id),
            )

    @staticmethod
    def _insert_legacy_archive(
        connection,
        guild_id,
        source_name,
        payload_json,
        imported_at_us,
    ):
        digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        return connection.execute(
            """
            INSERT INTO legacy_json_archives(
                guild_id,
                source_name,
                source_sha256,
                payload_json,
                imported_at_us
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO NOTHING
            """,
            (guild_id, source_name, digest, payload_json, imported_at_us),
        )

    def import_legacy_stats(
        self,
        guild_id,
        source_name,
        payload,
        cutover_at,
        *,
        force=False,
    ):
        """Atomically materialize one legacy JSON file for mixed-period reports.

        Repeating the same JSON is a no-op.  A changed source requires ``force`` so
        an accidental glob or wrong guild ID cannot silently replace old totals.
        """

        guild_id = str(guild_id)
        cutover = normalize_kst(cutover_at)
        period_rows = normalize_legacy_period_rows(payload, cutover)
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        cutover_at_us = datetime_to_epoch_microseconds(cutover)
        imported_at_us = datetime_to_epoch_microseconds(datetime.now(KST))

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT source_sha256, algorithm_version, cutover_at_us
                FROM legacy_imports
                WHERE guild_id = ?
                """,
                (guild_id,),
            ).fetchone()
            requires_overlap_check = (
                existing is None or existing["cutover_at_us"] != cutover_at_us
            )
            aliases = []
            if existing is not None:
                same_materialization = (
                    existing["source_sha256"] == digest
                    and existing["algorithm_version"]
                    == LEGACY_IMPORT_ALGORITHM_VERSION
                    and existing["cutover_at_us"] == cutover_at_us
                )
                if same_materialization:
                    return False
                if not force:
                    raise ValueError(
                        f"서버 {guild_id}에는 다른 legacy 변환이 이미 반영돼 있습니다. "
                        "교체하려면 --force를 사용하세요."
                    )
                aliases = connection.execute(
                    """
                    SELECT legacy_user_id, user_id, updated_at_us
                    FROM legacy_user_aliases
                    WHERE guild_id = ?
                    """,
                    (guild_id,),
                ).fetchall()
                connection.execute(
                    "DELETE FROM legacy_imports WHERE guild_id = ?",
                    (guild_id,),
                )

            if requires_overlap_check:
                self._ensure_no_cutover_overlap(connection, guild_id, cutover)

            connection.execute(
                """
                INSERT INTO legacy_imports(
                    guild_id,
                    source_name,
                    source_sha256,
                    algorithm_version,
                    cutover_at_us,
                    imported_at_us
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    os.fspath(source_name),
                    digest,
                    LEGACY_IMPORT_ALGORITHM_VERSION,
                    cutover_at_us,
                    imported_at_us,
                ),
            )
            connection.executemany(
                """
                INSERT INTO legacy_period_totals(
                    guild_id,
                    period_kind,
                    period_start,
                    period_end,
                    legacy_user_id,
                    nickname,
                    duration_microseconds,
                    source_kind
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [(guild_id, *row) for row in period_rows],
            )

            for alias in aliases:
                exists = connection.execute(
                    """
                    SELECT 1
                    FROM legacy_period_totals
                    WHERE guild_id = ? AND legacy_user_id = ?
                    LIMIT 1
                    """,
                    (guild_id, alias["legacy_user_id"]),
                ).fetchone()
                if exists is not None:
                    connection.execute(
                        """
                        INSERT INTO legacy_user_aliases(
                            guild_id,
                            legacy_user_id,
                            user_id,
                            updated_at_us
                        )
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            guild_id,
                            alias["legacy_user_id"],
                            alias["user_id"],
                            alias["updated_at_us"],
                        ),
                    )

            connection.execute(
                "DELETE FROM legacy_json_archives WHERE guild_id = ?",
                (guild_id,),
            )
            self._insert_legacy_archive(
                connection,
                guild_id,
                os.fspath(source_name),
                payload_json,
                imported_at_us,
            )
        return True

    def check_legacy_import(self, guild_id, payload, cutover_at, *, force=False):
        """Validate a target conflict before a multi-file CLI starts writing."""

        guild_id = str(guild_id)
        cutover = normalize_kst(cutover_at)
        normalize_legacy_period_rows(payload, cutover)
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        cutover_at_us = datetime_to_epoch_microseconds(cutover)
        with self._connection() as connection:
            existing = connection.execute(
                """
                SELECT source_sha256, algorithm_version, cutover_at_us
                FROM legacy_imports
                WHERE guild_id = ?
                """,
                (guild_id,),
            ).fetchone()
            if existing is None:
                self._ensure_no_cutover_overlap(connection, guild_id, cutover)
                return True
            same_materialization = (
                existing["source_sha256"] == digest
                and existing["algorithm_version"]
                == LEGACY_IMPORT_ALGORITHM_VERSION
                and existing["cutover_at_us"] == cutover_at_us
            )
            if same_materialization:
                return False
            if force:
                if existing["cutover_at_us"] != cutover_at_us:
                    self._ensure_no_cutover_overlap(connection, guild_id, cutover)
                return True
        raise ValueError(
            f"서버 {guild_id}에는 다른 legacy 변환이 이미 반영돼 있습니다. "
            "교체하려면 --force를 사용하세요."
        )

    def archive_legacy_stats(self, guild_id, source_name, payload):
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        imported_at_us = datetime_to_epoch_microseconds(datetime.now(KST))
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = self._insert_legacy_archive(
                connection,
                str(guild_id),
                os.fspath(source_name),
                payload_json,
                imported_at_us,
            )
            return cursor.rowcount == 1

    def replace_legacy_stats(self, guild_id, source_name, payload):
        """Replace only the archived legacy payload, leaving daily rows intact."""

        guild_id = str(guild_id)
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        imported_at_us = datetime_to_epoch_microseconds(datetime.now(KST))
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            materialized = connection.execute(
                """
                SELECT source_sha256
                FROM legacy_imports
                WHERE guild_id = ?
                """,
                (guild_id,),
            ).fetchone()
            if materialized is not None and materialized["source_sha256"] != digest:
                connection.execute(
                    "DELETE FROM legacy_imports WHERE guild_id = ?",
                    (guild_id,),
                )
            connection.execute(
                "DELETE FROM legacy_json_archives WHERE guild_id = ?",
                (guild_id,),
            )
            self._insert_legacy_archive(
                connection,
                guild_id,
                os.fspath(source_name),
                payload_json,
                imported_at_us,
            )

    def get_legacy_stats(self, guild_id):
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM legacy_json_archives
                WHERE guild_id = ?
                """,
                (str(guild_id),),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["payload_json"])

    def get_legacy_import_metadata(self, guild_id):
        guild_id = str(guild_id)
        with self._connection() as connection:
            imported = connection.execute(
                """
                SELECT
                    source_name,
                    source_sha256,
                    algorithm_version,
                    cutover_at_us
                FROM legacy_imports
                WHERE guild_id = ?
                """,
                (guild_id,),
            ).fetchone()
            if imported is None:
                return None
            aliases = connection.execute(
                """
                SELECT
                    aliases.legacy_user_id,
                    aliases.user_id,
                    profiles.nickname
                FROM legacy_user_aliases AS aliases
                JOIN member_profiles AS profiles
                  ON profiles.guild_id = aliases.guild_id
                 AND profiles.user_id = aliases.user_id
                WHERE aliases.guild_id = ?
                ORDER BY aliases.legacy_user_id
                """,
                (guild_id,),
            ).fetchall()
        return {
            "source_name": imported["source_name"],
            "source_sha256": imported["source_sha256"],
            "algorithm_version": imported["algorithm_version"],
            "cutover_at": epoch_microseconds_to_kst(
                imported["cutover_at_us"]
            ).isoformat(timespec="microseconds"),
            "aliases": [dict(alias) for alias in aliases],
        }

    def get_schema_version(self):
        with self._connection() as connection:
            return connection.execute("PRAGMA user_version").fetchone()[0]

    def get_active_user_ids(self, guild_id):
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT user_id
                FROM active_voice_sessions
                WHERE guild_id = ?
                ORDER BY user_id
                """,
                (str(guild_id),),
            ).fetchall()
        return [row["user_id"] for row in rows]
