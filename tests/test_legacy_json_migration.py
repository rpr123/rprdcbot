import json
import os
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import date, datetime
from io import StringIO

from migrate_json_to_sqlite import main as migrate_main
from voice_stats_store import KST, VoiceStatsStore, legacy_week_range
from voice_tracker import MemberManager


SECOND = 1_000_000


def kst(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=KST)


def legacy_payload():
    return {
        "2026년": {
            "1월": {
                "1주차": {
                    "old_name": {"time": 100, "nickname": "과거 별명"},
                },
            },
            "7월": {
                "total": {
                    "old_name": {"time": 300, "nickname": "과거 별명"},
                },
                "5주차": {
                    "old_name": {"time": 100, "nickname": "과거 별명"},
                },
            },
        },
        "_in_progress": {
            "old_name": {
                "name": "진행 중 별명",
                "time_week": 30,
                "time_month": 70,
            },
        },
    }


class LegacyJsonMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_file = os.path.join(
            self.temporary_directory.name,
            "voice-time.sqlite3",
        )
        self.store = VoiceStatsStore(self.database_file)
        self.cutover = kst(2026, 8, 18, 12, 0)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_legacy_week_range_handles_month_and_year_boundaries(self):
        self.assertEqual(
            legacy_week_range(2026, 1, 1),
            (date(2025, 12, 29), date(2026, 1, 5)),
        )
        self.assertEqual(
            legacy_week_range(2026, 7, 5),
            (date(2026, 7, 27), date(2026, 8, 3)),
        )
        self.assertEqual(
            legacy_week_range(2024, 2, 5),
            (date(2024, 2, 26), date(2024, 3, 4)),
        )
        with self.assertRaisesRegex(ValueError, "5주차"):
            legacy_week_range(2026, 2, 5)

    def test_week_month_and_year_use_only_the_matching_legacy_basis(self):
        self.assertTrue(
            self.store.import_legacy_stats(
                "123",
                "stats_123.json",
                legacy_payload(),
                self.cutover,
            )
        )

        # A row before the declared cutover is not mixed into imported aggregates.
        self.store.start_session("123", "42", "현재 별명", kst(2026, 8, 17, 10))
        self.store.end_session("123", "42", "현재 별명", kst(2026, 8, 17, 10, 5))
        self.store.start_session("123", "42", "현재 별명", kst(2026, 8, 18, 13))
        self.store.end_session(
            "123",
            "42",
            "현재 별명",
            kst(2026, 8, 18, 13, 0).replace(second=10),
        )
        self.store.map_legacy_users(
            "123",
            [("old_name", "42", "현재 별명")],
        )

        week = self.store.aggregate(
            "123",
            date(2026, 8, 17),
            date(2026, 8, 24),
            legacy_period_kind="week",
        )
        month = self.store.aggregate(
            "123",
            date(2026, 8, 1),
            date(2026, 9, 1),
            legacy_period_kind="month",
        )
        year = self.store.aggregate(
            "123",
            date(2026, 1, 1),
            date(2027, 1, 1),
            legacy_period_kind="month",
        )

        self.assertEqual(week["42"]["duration_microseconds"], 40 * SECOND)
        self.assertEqual(month["42"]["duration_microseconds"], 80 * SECOND)
        self.assertEqual(year["42"]["duration_microseconds"], 380 * SECOND)
        self.assertEqual(week["42"]["nickname"], "현재 별명")

    def test_unmapped_names_are_namespaced_and_unique_mapping_is_automatic(self):
        self.store.import_legacy_stats(
            "123",
            "stats_123.json",
            legacy_payload(),
            self.cutover,
        )
        before = self.store.aggregate(
            "123",
            date(2026, 7, 1),
            date(2026, 8, 1),
            legacy_period_kind="month",
        )
        self.assertIn("legacy:old_name", before)

        manager = MemberManager(
            file_name=None,
            guild_id="123",
            database_file=self.database_file,
            now_provider=lambda: self.cutover,
        )
        self.assertEqual(
            manager.map_legacy_members(
                [
                    ("old_name", "42", "현재 별명"),
                    ("duplicate", "51", "A"),
                    ("duplicate", "52", "B"),
                ]
            ),
            1,
        )
        after = self.store.aggregate(
            "123",
            date(2026, 7, 1),
            date(2026, 8, 1),
            legacy_period_kind="month",
        )
        self.assertEqual(list(after), ["42"])
        self.assertEqual(
            manager.map_legacy_members(
                [("old_name", "99", "username 재사용 사용자")]
            ),
            0,
        )
        preserved = self.store.aggregate(
            "123",
            date(2026, 7, 1),
            date(2026, 8, 1),
            legacy_period_kind="month",
        )
        self.assertEqual(list(preserved), ["42"])
        self.assertEqual(
            self.store.map_legacy_users(
                "123",
                [("old_name", "99", "명시적 교정 사용자")],
                overwrite=True,
            ),
            1,
        )
        corrected = self.store.aggregate(
            "123",
            date(2026, 7, 1),
            date(2026, 8, 1),
            legacy_period_kind="month",
        )
        self.assertEqual(list(corrected), ["99"])

    def test_repeat_is_noop_and_changed_materialization_requires_force(self):
        payload = legacy_payload()
        self.assertTrue(
            self.store.import_legacy_stats(
                "123", "stats_123.json", payload, self.cutover
            )
        )
        self.assertFalse(
            self.store.import_legacy_stats(
                "123", "stats_123.json", payload, self.cutover
            )
        )
        with self.assertRaisesRegex(ValueError, "--force"):
            self.store.import_legacy_stats(
                "123", "stats_123.json", payload, kst(2026, 8, 19)
            )

        changed = legacy_payload()
        changed["2026년"]["7월"]["total"]["old_name"]["time"] = 301
        with self.assertRaisesRegex(ValueError, "--force"):
            self.store.import_legacy_stats(
                "123", "stats_123.json", changed, self.cutover
            )
        total_before_force = self.store.aggregate(
            "123",
            date(2026, 7, 1),
            date(2026, 8, 1),
            legacy_period_kind="month",
        )
        self.assertEqual(
            total_before_force["legacy:old_name"]["duration_microseconds"],
            300 * SECOND,
        )
        self.assertTrue(
            self.store.import_legacy_stats(
                "123",
                "stats_123.json",
                changed,
                self.cutover,
                force=True,
            )
        )

    def test_version_three_backup_restores_materialization_and_aliases(self):
        self.store.import_legacy_stats(
            "123",
            "stats_123.json",
            legacy_payload(),
            self.cutover,
        )
        self.store.map_legacy_users(
            "123",
            [("old_name", "42", "현재 별명")],
        )
        source = MemberManager(
            file_name=None,
            guild_id="123",
            database_file=self.database_file,
            now_provider=lambda: self.cutover,
        )
        backup = source.export_stats()

        destination_file = os.path.join(
            self.temporary_directory.name,
            "restored.sqlite3",
        )
        destination = MemberManager(
            file_name=None,
            guild_id="123",
            database_file=destination_file,
            now_provider=lambda: self.cutover,
        )
        self.assertEqual(destination.replace_stats(backup), "daily")
        restored = destination.store.aggregate(
            "123",
            date(2026, 7, 1),
            date(2026, 8, 1),
            legacy_period_kind="month",
        )

        self.assertEqual(restored["42"]["duration_microseconds"], 300 * SECOND)
        self.assertEqual(destination.export_stats(), backup)

    def test_invalid_period_rolls_back_without_an_import_marker(self):
        invalid = {
            "2026년": {
                "2월": {
                    "5주차": {
                        "old_name": {"time": 1, "nickname": "별명"},
                    }
                }
            }
        }
        with self.assertRaisesRegex(ValueError, "5주차"):
            self.store.import_legacy_stats(
                "123", "stats_123.json", invalid, self.cutover
            )
        with closing(self.store._connect()) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM legacy_imports"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_existing_daily_row_on_midday_cutover_date_is_rejected(self):
        self.store.start_session("123", "42", "현재 사용자", kst(2026, 8, 18, 9))
        self.store.end_session("123", "42", "현재 사용자", kst(2026, 8, 18, 9, 1))

        with self.assertRaisesRegex(ValueError, "겹칠 수 있습니다"):
            self.store.import_legacy_stats(
                "123",
                "stats_123.json",
                legacy_payload(),
                self.cutover,
            )

        self.assertEqual(len(self.store.get_daily_records("123")), 1)
        with closing(self.store._connect()) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM legacy_imports WHERE guild_id = '123'"
            ).fetchone()[0]
        self.assertEqual(count, 0)


class LegacyMigrationCliTests(unittest.TestCase):
    def test_all_conflicts_are_checked_before_a_multi_file_batch_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            first = os.path.join(directory, "stats_123.json")
            second = os.path.join(directory, "stats_456.json")
            database = os.path.join(directory, "voice-time.sqlite3")
            with open(first, "w", encoding="utf-8") as output:
                json.dump(legacy_payload(), output, ensure_ascii=False)
            changed = legacy_payload()
            changed["2026년"]["7월"]["total"]["old_name"]["time"] = 999
            with open(second, "w", encoding="utf-8") as output:
                json.dump(changed, output, ensure_ascii=False)

            store = VoiceStatsStore(database)
            store.import_legacy_stats(
                "456",
                "old-stats-456.json",
                legacy_payload(),
                kst(2026, 8, 18, 12),
            )
            stderr = StringIO()
            with redirect_stdout(StringIO()), redirect_stderr(stderr):
                result = migrate_main(
                    [
                        first,
                        second,
                        "--database",
                        database,
                        "--cutover-at",
                        "2026-08-18T12:00:00+09:00",
                    ]
                )

            self.assertEqual(result, 1)
            self.assertIn("사전 확인 실패", stderr.getvalue())
            with closing(store._connect()) as connection:
                first_count = connection.execute(
                    "SELECT COUNT(*) FROM legacy_imports WHERE guild_id = '123'"
                ).fetchone()[0]
            self.assertEqual(first_count, 0)

    def test_dry_run_does_not_create_database_and_duplicate_keys_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "stats_123.json")
            database = os.path.join(directory, "voice-time.sqlite3")
            with open(source, "w", encoding="utf-8") as output:
                json.dump(legacy_payload(), output, ensure_ascii=False)

            stdout = StringIO()
            with redirect_stdout(stdout):
                result = migrate_main(
                    [
                        source,
                        "--database",
                        database,
                        "--cutover-at",
                        "2026-08-18T12:00:00+09:00",
                        "--dry-run",
                    ]
                )
            self.assertEqual(result, 0)
            self.assertFalse(os.path.exists(database))

            duplicate = os.path.join(directory, "stats_456.json")
            with open(duplicate, "w", encoding="utf-8") as output:
                output.write('{"2026년": {}, "2026년": {}}')
            stderr = StringIO()
            with redirect_stderr(stderr):
                result = migrate_main(
                    [
                        duplicate,
                        "--database",
                        database,
                        "--cutover-at",
                        "2026-08-18T12:00:00+09:00",
                        "--dry-run",
                    ]
                )
            self.assertEqual(result, 1)
            self.assertIn("중복 키", stderr.getvalue())
            self.assertFalse(os.path.exists(database))

            stdout = StringIO()
            arguments = [
                source,
                "--database",
                database,
                "--cutover-at",
                "2026-08-18T12:00:00+09:00",
                "--alias",
                "old_name=42",
            ]
            with redirect_stdout(stdout):
                self.assertEqual(migrate_main(arguments), 0)
            self.assertTrue(os.path.exists(database))
            migrated = VoiceStatsStore(database).aggregate(
                "123",
                date(2026, 8, 17),
                date(2026, 8, 24),
                legacy_period_kind="week",
            )
            self.assertEqual(
                migrated["42"]["duration_microseconds"],
                30 * SECOND,
            )

            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(migrate_main(arguments), 0)
            self.assertIn("건너뜀", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
