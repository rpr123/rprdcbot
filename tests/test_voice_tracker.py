import json
import os
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import patch

from voice_tracker import MemberManager, MemberRecord, sum_month_totals, validate_stats


class ValidateStatsTests(unittest.TestCase):
    def test_accepts_saved_stats_shape(self):
        validate_stats(
            {
                "2026년": {
                    "8월": {
                        "total": {
                            "user": {"time": 120, "nickname": "테스터"},
                        },
                    },
                },
                "_in_progress": {
                    "user": {
                        "name": "테스터",
                        "time_week": 30,
                        "time_month": 60,
                    },
                },
            }
        )

    def test_rejects_non_object_root(self):
        with self.assertRaises(ValueError):
            validate_stats([])

    def test_rejects_invalid_time(self):
        with self.assertRaises(ValueError):
            validate_stats(
                {
                    "2026년": {
                        "8월": {
                            "total": {
                                "user": {"time": -1, "nickname": "테스터"},
                            },
                        },
                    },
                }
            )


class ReplaceStatsTests(unittest.TestCase):
    def test_replaces_file_and_restores_in_progress_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            file_name = os.path.join(temp_dir, "stats.json")
            manager = MemberManager(file_name=file_name)
            manager.members["user"] = MemberRecord("현재 이름", "user")
            manager.members["user"].time_week = timedelta(seconds=999)
            manager.members["user"].time_month = timedelta(seconds=999)

            manager.replace_stats(
                {
                    "2026년": {"8월": {"total": {}}},
                    "_in_progress": {
                        "user": {
                            "name": "백업 이름",
                            "time_week": 30,
                            "time_month": 60,
                        },
                    },
                }
            )

            self.assertEqual(manager.members["user"].time_week, timedelta(seconds=30))
            self.assertEqual(manager.members["user"].time_month, timedelta(seconds=60))

            with open(file_name, "r", encoding="utf-8") as stats_file:
                saved_stats = json.load(stats_file)

            self.assertEqual(saved_stats["2026년"]["8월"]["total"], {})
            self.assertEqual(saved_stats["_in_progress"]["user"]["time_week"], 30)
            self.assertEqual(saved_stats["_in_progress"]["user"]["time_month"], 60)

    def test_keeps_memory_unchanged_when_file_write_fails(self):
        manager = MemberManager(file_name="unused.json")
        manager.stats = {"existing": {}}

        with patch.object(manager, "_write_stats", side_effect=OSError):
            with self.assertRaises(OSError):
                manager.replace_stats({"replacement": {}})

        self.assertEqual(manager.stats, {"existing": {}})


class YearSettlementTests(unittest.TestCase):
    def test_sums_only_month_totals(self):
        year_total = sum_month_totals(
            {
                "1월": {
                    "total": {
                        "user-a": {"time": 10, "nickname": "이전 이름"},
                    },
                    "1주차": {
                        "user-a": {"time": 999, "nickname": "이전 이름"},
                    },
                },
                "2월": {
                    "total": {
                        "user-a": {"time": 20, "nickname": "현재 이름"},
                        "user-b": {"time": 5, "nickname": "다른 사용자"},
                    },
                },
            }
        )

        self.assertEqual(year_total["user-a"]["time"], 30)
        self.assertEqual(year_total["user-a"]["nickname"], "현재 이름")
        self.assertEqual(year_total["user-b"]["time"], 5)
        self.assertNotEqual(year_total["user-a"]["time"], 1029)

    def test_print_year_uses_previous_year_without_mutating_stats(self):
        manager = MemberManager(file_name="unused.json")
        manager.stats = {
            "2025년": {
                "12월": {
                    "total": {
                        "user": {"time": 65, "nickname": "테스터"},
                    },
                },
            }
        }
        original_stats = deepcopy(manager.stats)

        with patch("voice_tracker.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = datetime(2026, 1, 1)
            message = manager.print_year()

        self.assertIn("2025년 연간 결산 (2025-01-01 ~ 2025-12-31)", message)
        self.assertIn("0:01:05 : 테스터(user)", message)
        self.assertEqual(manager.stats, original_stats)


if __name__ == "__main__":
    unittest.main()
