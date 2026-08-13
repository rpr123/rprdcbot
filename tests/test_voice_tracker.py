import json
import os
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch

from voice_tracker import MemberManager, MemberRecord, validate_stats


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


if __name__ == "__main__":
    unittest.main()
