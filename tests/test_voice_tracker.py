import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone

from voice_stats_store import KST, SCHEMA_VERSION, VoiceStatsStore
from voice_tracker import EXPORT_FORMAT_VERSION, MemberManager


SECOND = 1_000_000


def kst(year, month, day, hour=0, minute=0, second=0, microsecond=0):
    return datetime(
        year,
        month,
        day,
        hour,
        minute,
        second,
        microsecond,
        tzinfo=KST,
    )


def daily_export(records, *, guild_id="guild", legacy_stats=None):
    return {
        "format_version": EXPORT_FORMAT_VERSION,
        "timezone": "Asia/Seoul",
        "duration_unit": "microseconds",
        "guild_id": guild_id,
        "daily_voice_times": records,
        "legacy_stats": legacy_stats,
    }


class SQLiteStoreTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_file = os.path.join(
            self.temporary_directory.name,
            "voice-time.sqlite3",
        )
        self.store = VoiceStatsStore(self.database_file)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def make_manager(self, guild_id="guild", now=None, database_file=None):
        fixed_now = now or kst(2026, 1, 1)
        return MemberManager(
            file_name=None,
            guild_id=guild_id,
            database_file=database_file or self.database_file,
            now_provider=lambda: fixed_now,
        )


class SchemaUpsertAndIsolationTests(SQLiteStoreTestCase):
    def test_schema_is_initialized_once_with_expected_tables(self):
        # Reopening the same file must be a no-op migration, not a CREATE failure.
        VoiceStatsStore(self.database_file)

        with closing(sqlite3.connect(self.database_file)) as connection:
            user_version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            migration_versions = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()

        self.assertEqual(user_version, SCHEMA_VERSION)
        self.assertEqual(migration_versions, [(1,), (SCHEMA_VERSION,)])
        self.assertTrue(
            {
                "member_profiles",
                "daily_voice_times",
                "active_voice_sessions",
                "legacy_json_archives",
                "legacy_imports",
                "legacy_period_totals",
                "legacy_user_aliases",
                "schema_migrations",
            }.issubset(tables)
        )

    def test_matching_partial_version_zero_schema_is_completed_atomically(self):
        partial_file = os.path.join(self.temporary_directory.name, "partial.sqlite3")
        with closing(sqlite3.connect(partial_file)) as connection:
            connection.execute(
                """
                CREATE TABLE member_profiles (
                    guild_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    updated_at_us INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, user_id)
                )
                """
            )
            connection.commit()

        recovered = VoiceStatsStore(partial_file)

        self.assertEqual(recovered.get_schema_version(), SCHEMA_VERSION)
        with closing(sqlite3.connect(partial_file)) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertIn("daily_voice_times", tables)
        self.assertIn("active_voice_sessions", tables)

    def test_version_one_database_upgrades_without_losing_daily_rows(self):
        old_file = os.path.join(self.temporary_directory.name, "version-one.sqlite3")
        old_store = VoiceStatsStore(old_file)
        old_store.start_session("guild", "user", "기존 사용자", kst(2026, 8, 18, 9))
        old_store.end_session("guild", "user", "기존 사용자", kst(2026, 8, 18, 9, 1))
        with closing(sqlite3.connect(old_file)) as connection:
            connection.execute("DROP TABLE legacy_user_aliases")
            connection.execute("DROP TABLE legacy_period_totals")
            connection.execute("DROP TABLE legacy_imports")
            connection.execute("DELETE FROM schema_migrations WHERE version = 2")
            connection.execute("PRAGMA user_version = 1")
            connection.commit()

        upgraded = VoiceStatsStore(old_file)

        self.assertEqual(upgraded.get_schema_version(), SCHEMA_VERSION)
        self.assertEqual(
            upgraded.get_daily_records("guild"),
            [
                {
                    "activity_date": "2026-08-18",
                    "user_id": "user",
                    "nickname": "기존 사용자",
                    "duration_microseconds": 60 * SECOND,
                }
            ],
        )

    def test_memory_database_is_rejected_because_each_operation_is_transactional(self):
        with self.assertRaisesRegex(ValueError, "파일 기반"):
            VoiceStatsStore(":memory:")

    def test_daily_upsert_accumulates_and_guilds_are_isolated(self):
        self.store.start_session("guild-a", "user", "첫 이름", kst(2026, 8, 18, 10))
        self.store.end_session("guild-a", "user", "둘째 이름", kst(2026, 8, 18, 10, 5))
        self.store.start_session("guild-a", "user", "둘째 이름", kst(2026, 8, 18, 11))
        self.store.end_session("guild-a", "user", "최종 이름", kst(2026, 8, 18, 11, 7))

        self.store.start_session("guild-b", "user", "다른 서버 이름", kst(2026, 8, 18, 10))
        self.store.end_session("guild-b", "user", "다른 서버 이름", kst(2026, 8, 18, 10, 3))

        guild_a = self.store.get_daily_records("guild-a")
        guild_b = self.store.get_daily_records("guild-b")

        self.assertEqual(
            guild_a,
            [
                {
                    "activity_date": "2026-08-18",
                    "user_id": "user",
                    "nickname": "최종 이름",
                    "duration_microseconds": 12 * 60 * SECOND,
                }
            ],
        )
        self.assertEqual(
            guild_b,
            [
                {
                    "activity_date": "2026-08-18",
                    "user_id": "user",
                    "nickname": "다른 서버 이름",
                    "duration_microseconds": 3 * 60 * SECOND,
                }
            ],
        )
        self.assertEqual(
            self.store.aggregate("guild-a", date(2026, 8, 18), date(2026, 8, 19)),
            {
                "user": {
                    "nickname": "최종 이름",
                    "duration_microseconds": 12 * 60 * SECOND,
                }
            },
        )


class DailyBoundaryTests(SQLiteStoreTestCase):
    def test_utc_interval_is_split_at_kst_midnight_and_month_end(self):
        # These UTC instants are 2026-01-31 23:59:30 through
        # 2026-02-01 00:00:30 in Korea.
        utc = timezone.utc
        started_at = datetime(2026, 1, 31, 14, 59, 30, tzinfo=utc)
        ended_at = datetime(2026, 1, 31, 15, 0, 30, tzinfo=utc)

        self.store.start_session("guild", "user", "테스터", started_at)
        elapsed = self.store.end_session("guild", "user", "테스터", ended_at)

        self.assertEqual(elapsed, 60 * SECOND)
        self.assertEqual(
            self.store.get_daily_records("guild"),
            [
                {
                    "activity_date": "2026-01-31",
                    "user_id": "user",
                    "nickname": "테스터",
                    "duration_microseconds": 30 * SECOND,
                },
                {
                    "activity_date": "2026-02-01",
                    "user_id": "user",
                    "nickname": "테스터",
                    "duration_microseconds": 30 * SECOND,
                },
            ],
        )


class VoiceEventIdempotencyTests(SQLiteStoreTestCase):
    def test_duplicate_in_and_out_events_do_not_double_count(self):
        manager = self.make_manager(now=kst(2026, 8, 18, 9))

        manager.enter_exit("테스터", "user", "out", now=kst(2026, 8, 18, 9, 59))
        manager.enter_exit("테스터", "user", "in", now=kst(2026, 8, 18, 10))
        manager.enter_exit("테스터", "user", "in", now=kst(2026, 8, 18, 10, 1))
        manager.enter_exit("테스터", "user", "out", now=kst(2026, 8, 18, 10, 10))
        manager.enter_exit("테스터", "user", "out", now=kst(2026, 8, 18, 10, 11))

        self.assertEqual(manager.members["user"].time, timedelta(minutes=10))
        self.assertEqual(self.store.get_active_user_ids("guild"), [])
        self.assertEqual(
            self.store.get_daily_records("guild")[0]["duration_microseconds"],
            10 * 60 * SECOND,
        )

    def test_late_out_from_an_old_session_does_not_close_the_new_session(self):
        manager = self.make_manager(now=kst(2026, 8, 18, 10))
        manager.enter_exit("테스터", "user", "in", now=kst(2026, 8, 18, 10))
        manager.enter_exit("테스터", "user", "out", now=kst(2026, 8, 18, 10, 5))
        manager.enter_exit("테스터", "user", "in", now=kst(2026, 8, 18, 10, 10))

        manager.enter_exit(
            "테스터",
            "user",
            "out",
            now=kst(2026, 8, 18, 10, 6),
        )
        self.assertTrue(manager.members["user"]._ing)
        self.assertEqual(self.store.get_active_user_ids("guild"), ["user"])

        manager.enter_exit("테스터", "user", "out", now=kst(2026, 8, 18, 10, 20))
        self.assertEqual(manager.members["user"].time, timedelta(minutes=15))
        self.assertEqual(
            self.store.get_daily_records("guild")[0]["duration_microseconds"],
            15 * 60 * SECOND,
        )


class DailySettlementTests(SQLiteStoreTestCase):
    def setUp(self):
        super().setUp()
        self.manager = self.make_manager(now=kst(2026, 2, 2))
        self.manager.replace_stats(
            daily_export(
                [
                    {
                        "date": "2025-12-31",
                        "user_id": "user",
                        "nickname": "테스터",
                        "duration_microseconds": 1 * SECOND,
                    },
                    {
                        "date": "2026-01-25",
                        "user_id": "user",
                        "nickname": "테스터",
                        "duration_microseconds": 5 * SECOND,
                    },
                    {
                        "date": "2026-01-26",
                        "user_id": "user",
                        "nickname": "테스터",
                        "duration_microseconds": 10 * SECOND,
                    },
                    {
                        "date": "2026-01-31",
                        "user_id": "user",
                        "nickname": "테스터",
                        "duration_microseconds": 20 * SECOND,
                    },
                    {
                        "date": "2026-02-01",
                        "user_id": "user",
                        "nickname": "테스터",
                        "duration_microseconds": 30 * SECOND,
                    },
                    {
                        "date": "2026-02-02",
                        "user_id": "user",
                        "nickname": "테스터",
                        "duration_microseconds": 40 * SECOND,
                    },
                    {
                        "date": "2027-01-01",
                        "user_id": "user",
                        "nickname": "테스터",
                        "duration_microseconds": 50 * SECOND,
                    },
                ]
            )
        )

    def test_week_month_and_year_are_summed_from_daily_rows_without_mutation(self):
        before = self.manager.export_stats()

        weekly_first = self.manager.print_week(now=kst(2026, 2, 2))
        weekly_second = self.manager.print_week(now=kst(2026, 2, 2))
        monthly_first = self.manager.print_month(now=kst(2026, 2, 1))
        monthly_second = self.manager.print_month(now=kst(2026, 2, 1))
        yearly_first = self.manager.print_year(now=kst(2027, 1, 1))
        yearly_second = self.manager.print_year(now=kst(2027, 1, 1))

        self.assertEqual(weekly_first, weekly_second)
        self.assertIn("(2026-01-26 ~ 2026-02-01)", weekly_first)
        self.assertIn("0:01:00 : 테스터(user)", weekly_first)

        self.assertEqual(monthly_first, monthly_second)
        self.assertIn("(2026-01-01 ~ 2026-01-31)", monthly_first)
        self.assertIn("0:00:35 : 테스터(user)", monthly_first)

        self.assertEqual(yearly_first, yearly_second)
        self.assertIn("(2026-01-01 ~ 2026-12-31)", yearly_first)
        self.assertIn("0:01:45 : 테스터(user)", yearly_first)

        self.assertEqual(self.manager.export_stats(), before)


class ExportImportTests(SQLiteStoreTestCase):
    def test_version_two_daily_backup_remains_accepted(self):
        manager = self.make_manager(now=kst(2026, 8, 18))
        backup = daily_export(
            [
                {
                    "date": "2026-08-18",
                    "user_id": "user",
                    "nickname": "기존 백업 사용자",
                    "duration_microseconds": 12 * SECOND,
                }
            ]
        )
        backup["format_version"] = 2

        self.assertEqual(manager.replace_stats(backup), "daily")
        self.assertEqual(
            manager.store.get_daily_records("guild")[0]["duration_microseconds"],
            12 * SECOND,
        )

    def test_export_import_round_trip_preserves_daily_and_legacy_data(self):
        legacy = {
            "2025년": {
                "12월": {
                    "total": {
                        "old-user": {"time": 12, "nickname": "옛 이름"},
                    }
                }
            }
        }
        source = self.make_manager(
            guild_id="guild",
            now=kst(2026, 8, 18),
            database_file=os.path.join(self.temporary_directory.name, "source.sqlite3"),
        )
        source.replace_stats(
            daily_export(
                [
                    {
                        "date": "2026-08-17",
                        "user_id": "user-a",
                        "nickname": "사용자 A",
                        "duration_microseconds": 123_456_789,
                    },
                    {
                        "date": "2026-08-18",
                        "user_id": "user-b",
                        "nickname": "사용자 B",
                        "duration_microseconds": 987_654_321,
                    },
                ],
                legacy_stats=legacy,
            )
        )
        exported = source.export_stats()

        destination = self.make_manager(
            guild_id="guild",
            now=kst(2026, 8, 18),
            database_file=os.path.join(
                self.temporary_directory.name,
                "destination.sqlite3",
            ),
        )
        import_type = destination.replace_stats(deepcopy(exported))

        self.assertEqual(import_type, "daily")
        self.assertEqual(destination.export_stats(), exported)

    def test_invalid_import_keeps_existing_rows_unchanged(self):
        manager = self.make_manager(now=kst(2026, 8, 18))
        manager.replace_stats(
            daily_export(
                [
                    {
                        "date": "2026-08-18",
                        "user_id": "user",
                        "nickname": "기존 사용자",
                        "duration_microseconds": 60 * SECOND,
                    }
                ]
            )
        )
        before = manager.export_stats()
        invalid = deepcopy(before)
        invalid["daily_voice_times"][0]["duration_microseconds"] = -1

        with self.assertRaises(ValueError):
            manager.replace_stats(invalid)

        self.assertEqual(manager.export_stats(), before)

    def test_import_rejects_a_backup_from_another_guild(self):
        manager = self.make_manager(guild_id="guild-a", now=kst(2026, 8, 18))
        backup = daily_export([], guild_id="guild-b")

        with self.assertRaisesRegex(ValueError, "다른 서버"):
            manager.replace_stats(backup)

        self.assertEqual(manager.store.get_daily_records("guild-a"), [])

    def test_import_rejects_more_than_twenty_four_hours_for_one_day(self):
        manager = self.make_manager(now=kst(2026, 8, 18))
        backup = daily_export(
            [
                {
                    "date": "2026-08-18",
                    "user_id": "user",
                    "nickname": "사용자",
                    "duration_microseconds": 86_400 * SECOND + 1,
                }
            ]
        )

        with self.assertRaisesRegex(ValueError, "24시간"):
            manager.replace_stats(backup)

        self.assertEqual(manager.store.get_daily_records("guild"), [])

    def test_legacy_upload_is_archived_without_erasing_daily_rows(self):
        manager = self.make_manager(now=kst(2026, 8, 18))
        manager.replace_stats(
            daily_export(
                [
                    {
                        "date": "2026-08-18",
                        "user_id": "user",
                        "nickname": "현재 사용자",
                        "duration_microseconds": 60 * SECOND,
                    }
                ]
            )
        )
        daily_before = manager.store.get_daily_records("guild")
        legacy = {
            "2025년": {
                "12월": {
                    "total": {
                        "old-user": {"time": 12, "nickname": "과거 사용자"},
                    }
                }
            }
        }

        import_type = manager.replace_stats(legacy)

        self.assertEqual(import_type, "legacy")
        self.assertEqual(manager.store.get_daily_records("guild"), daily_before)
        self.assertEqual(manager.store.get_legacy_stats("guild"), legacy)

    def test_sql_error_rolls_back_partial_replacement(self):
        self.store.replace_daily_records(
            "guild",
            [
                {
                    "activity_date": "2026-08-18",
                    "user_id": "existing",
                    "nickname": "기존 사용자",
                    "duration_microseconds": 60 * SECOND,
                }
            ],
        )
        before = self.store.get_daily_records("guild")
        duplicate_records = [
            {
                "activity_date": "2026-08-19",
                "user_id": "duplicate",
                "nickname": "중복 사용자",
                "duration_microseconds": SECOND,
            },
            {
                "activity_date": "2026-08-19",
                "user_id": "duplicate",
                "nickname": "중복 사용자",
                "duration_microseconds": SECOND,
            },
        ]

        with self.assertRaises(sqlite3.IntegrityError):
            self.store.replace_daily_records("guild", duplicate_records)

        self.assertEqual(self.store.get_daily_records("guild"), before)


class LegacyArchiveTests(SQLiteStoreTestCase):
    def test_legacy_json_is_archived_once_without_fabricating_daily_rows(self):
        legacy_file = os.path.join(self.temporary_directory.name, "stats.json")
        first_payload = {
            "2025년": {
                "12월": {
                    "total": {
                        "user": {"time": 3600, "nickname": "기존 이름"},
                    },
                    "4주차": {
                        "user": {"time": 900, "nickname": "기존 이름"},
                    },
                }
            }
        }
        with open(legacy_file, "w", encoding="utf-8") as stats_file:
            json.dump(first_payload, stats_file, ensure_ascii=False)

        first_manager = MemberManager(
            file_name=legacy_file,
            guild_id="guild",
            database_file=self.database_file,
            now_provider=lambda: kst(2026, 1, 1),
        )
        self.assertTrue(first_manager.legacy_archived)
        self.assertEqual(first_manager.store.get_legacy_stats("guild"), first_payload)
        self.assertEqual(first_manager.store.get_daily_records("guild"), [])

        second_payload = {
            "2026년": {
                "1월": {
                    "total": {
                        "user": {"time": 9999, "nickname": "바뀐 이름"},
                    }
                }
            }
        }
        with open(legacy_file, "w", encoding="utf-8") as stats_file:
            json.dump(second_payload, stats_file, ensure_ascii=False)

        second_manager = MemberManager(
            file_name=legacy_file,
            guild_id="guild",
            database_file=self.database_file,
            now_provider=lambda: kst(2026, 1, 2),
        )

        self.assertFalse(second_manager.legacy_archived)
        self.assertEqual(second_manager.store.get_legacy_stats("guild"), first_payload)
        self.assertEqual(second_manager.store.get_daily_records("guild"), [])
        with closing(sqlite3.connect(self.database_file)) as connection:
            archive_count = connection.execute(
                "SELECT COUNT(*) FROM legacy_json_archives WHERE guild_id = ?",
                ("guild",),
            ).fetchone()[0]
        self.assertEqual(archive_count, 1)


class StartupReconciliationTests(SQLiteStoreTestCase):
    def test_startup_reconcile_discards_stale_sessions_without_guessing_downtime(self):
        # Five minutes were safely checkpointed before the old process disappeared.
        self.store.start_session("guild", "returning", "복귀 사용자", kst(2026, 8, 18, 9))
        self.store.checkpoint_sessions(
            "guild",
            [("returning", "복귀 사용자")],
            kst(2026, 8, 18, 9, 5),
        )
        self.store.start_session("guild", "stale", "퇴장한 사용자", kst(2026, 8, 18, 9))

        restarted = self.make_manager(now=kst(2026, 8, 18, 10))
        restarted.reconcile_active_members(
            [("복귀 사용자", "returning"), ("신규 사용자", "new")],
            now=kst(2026, 8, 18, 10),
        )

        self.assertEqual(
            self.store.get_active_user_ids("guild"),
            ["new", "returning"],
        )
        before_new_checkpoint = self.store.aggregate(
            "guild",
            date(2026, 8, 18),
            date(2026, 8, 19),
        )
        self.assertEqual(
            before_new_checkpoint["returning"]["duration_microseconds"],
            5 * 60 * SECOND,
        )
        self.assertNotIn("stale", before_new_checkpoint)

        restarted.update(now=kst(2026, 8, 18, 10, 10))
        totals = self.store.aggregate(
            "guild",
            date(2026, 8, 18),
            date(2026, 8, 19),
        )

        self.assertEqual(
            totals["returning"]["duration_microseconds"],
            15 * 60 * SECOND,
        )
        self.assertEqual(totals["new"]["duration_microseconds"], 10 * 60 * SECOND)
        self.assertNotIn("stale", totals)

    def test_disconnect_suspends_accounting_until_current_members_are_known(self):
        manager = self.make_manager(now=kst(2026, 8, 18, 10))
        manager.reconcile_active_members(
            [("사용자", "user")],
            now=kst(2026, 8, 18, 10),
        )
        manager.suspend(now=kst(2026, 8, 18, 10, 5))

        # Autosave and settlement tasks can still run while the gateway is down.
        manager.update(now=kst(2026, 8, 18, 10, 30))
        # Replayed individual events are ignored until the full current state is known.
        manager.enter_exit(
            "사용자",
            "user",
            "out",
            now=kst(2026, 8, 18, 10, 20),
        )
        manager.reconcile_active_members(
            [("사용자", "user")],
            now=kst(2026, 8, 18, 10, 30),
        )
        manager.update(now=kst(2026, 8, 18, 10, 40))

        totals = self.store.aggregate(
            "guild",
            date(2026, 8, 18),
            date(2026, 8, 19),
        )
        self.assertEqual(
            totals["user"]["duration_microseconds"],
            15 * 60 * SECOND,
        )


if __name__ == "__main__":
    unittest.main()
