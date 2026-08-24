from copy import deepcopy
from datetime import date, datetime, timedelta
import hashlib
import json
import os

from voice_stats_store import (
    KST,
    LEGACY_IMPORT_ALGORITHM_VERSION,
    MAX_DAILY_DURATION_MICROSECONDS,
    SQLITE_MAX_INTEGER,
    VoiceStatsStore,
    datetime_to_epoch_microseconds,
    normalize_kst,
    normalize_legacy_period_rows,
)


FILE_NAME = "stats.json"
DATABASE_FILE = "voice_time.db"
EXPORT_FORMAT_VERSION = 3
SUPPORTED_EXPORT_FORMAT_VERSIONS = {2, EXPORT_FORMAT_VERSION}


def get_iso_week_of_month(target_date):
    # 해당 날짜가 속한 주의 목요일을 기준으로 ISO식 월/주차를 계산한다.
    target_thursday = target_date - timedelta(days=(target_date.weekday() - 3))
    base_year = target_thursday.year
    base_month = target_thursday.month

    first_day_of_month = target_thursday.replace(day=1)
    first_thursday = first_day_of_month + timedelta(
        days=(3 - first_day_of_month.weekday() + 7) % 7
    )
    week_number = (target_thursday - first_thursday).days // 7 + 1

    return [base_year, base_month, week_number]


class MemberRecord:
    def __init__(self, name, user_id, now=None):
        self.name = name
        self.userid = str(user_id)
        self.time = timedelta(0)
        self._ing = False
        self.timestamp = normalize_kst(now or datetime.now(KST))

    def update_in(self, now=None):
        if not self._ing:
            self._ing = True
            self.timestamp = normalize_kst(now or datetime.now(KST))

    def update_out(self, now=None):
        if not self._ing:
            return None

        ended_at = normalize_kst(now or datetime.now(KST))
        if ended_at < self.timestamp:
            ended_at = self.timestamp
        started_at = self.timestamp
        self.time += ended_at - started_at
        self.timestamp = ended_at
        self._ing = False
        return started_at, ended_at

    def update(self, now=None):
        if not self._ing:
            return None

        checkpoint = normalize_kst(now or datetime.now(KST))
        if checkpoint < self.timestamp:
            checkpoint = self.timestamp
        started_at = self.timestamp
        self.time += checkpoint - started_at
        self.timestamp = checkpoint
        return started_at, checkpoint

    def apply_persisted_elapsed(self, elapsed_microseconds, checkpoint, active):
        self.time += timedelta(microseconds=elapsed_microseconds)
        self.timestamp = normalize_kst(checkpoint)
        self._ing = active

    def print_current(self):
        elapsed = str(self.time).split(".")[0]
        return f"{elapsed} : {self.name}({self.userid})\n"

    def reset(self):
        self.time = timedelta(0)

    # 기존 코드와의 호환용 별칭
    printing = print_current


class MemberManager:
    def __init__(
        self,
        file_name=FILE_NAME,
        *,
        guild_id="default",
        database_file=DATABASE_FILE,
        now_provider=None,
        store=None,
    ):
        self.file_name = file_name
        self.guild_id = str(guild_id)
        self.database_file = database_file
        self._now_provider = now_provider or (lambda: datetime.now(KST))
        self.store = store or VoiceStatsStore(database_file)
        self.members = {}
        self.timestamp = self._now()
        self.timestamp_recently = self.timestamp
        self._startup_reconciled = False
        self._suspended = False
        self.legacy_archived = self._archive_existing_legacy_file()

    def _now(self, value=None):
        return normalize_kst(value if value is not None else self._now_provider())

    def _archive_existing_legacy_file(self):
        if not self.file_name or not os.path.exists(self.file_name):
            return False

        with open(self.file_name, "r", encoding="utf-8") as stats_file:
            stats = json.load(stats_file)
        validate_stats(stats)
        return self.store.archive_legacy_stats(
            self.guild_id,
            self.file_name,
            stats,
        )

    def reconcile_active_members(self, active_members, now=None):
        """Align DB sessions with Discord's current voice-channel state."""

        checkpoint = self._now(now)
        current = {str(user_id): name for name, user_id in active_members}

        if not self._startup_reconciled or self._suspended:
            self.store.reset_active_sessions(
                self.guild_id,
                [(user_id, name) for user_id, name in current.items()],
                checkpoint,
            )
            if not self._startup_reconciled:
                self.members = {}
            else:
                for member in self.members.values():
                    if member._ing:
                        member.apply_persisted_elapsed(0, checkpoint, active=False)
            for user_id, name in current.items():
                if user_id not in self.members:
                    self.members[user_id] = MemberRecord(name, user_id, now=checkpoint)
                member = self.members[user_id]
                member.name = name
                member.update_in(checkpoint)
            self._startup_reconciled = True
            self._suspended = False
            return

        # If on_ready repeats without an on_disconnect callback, only members still
        # visible are safe to account through the new checkpoint. Missing users are
        # discarded at their last committed watermark instead of being overcounted.
        continuing = [
            (user_id, current[user_id])
            for user_id, member in self.members.items()
            if member._ing and user_id in current
        ]
        elapsed_by_user = self.store.checkpoint_sessions(
            self.guild_id,
            continuing,
            checkpoint,
        )
        for user_id, _ in continuing:
            member = self.members[user_id]
            member.name = current[user_id]
            member.apply_persisted_elapsed(
                elapsed_by_user.get(user_id, 0),
                checkpoint,
                active=True,
            )

        for user_id, member in self.members.items():
            if member._ing and user_id not in current:
                self.store.discard_session(self.guild_id, user_id)
                member.apply_persisted_elapsed(0, checkpoint, active=False)

        for user_id, name in current.items():
            if user_id not in self.members:
                self.members[user_id] = MemberRecord(name, user_id, now=checkpoint)
            member = self.members[user_id]
            member.name = name
            if not member._ing:
                self.store.start_session(
                    self.guild_id,
                    user_id,
                    name,
                    checkpoint,
                )
                member.update_in(checkpoint)

    def map_legacy_members(self, members):
        """Resolve legacy username keys only when the current match is unique."""

        candidates = list(members)
        counts = {}
        for legacy_user_id, _, _ in candidates:
            counts[legacy_user_id] = counts.get(legacy_user_id, 0) + 1
        unambiguous = [
            (legacy_user_id, user_id, nickname)
            for legacy_user_id, user_id, nickname in candidates
            if counts[legacy_user_id] == 1
        ]
        return self.store.map_legacy_users(self.guild_id, unambiguous)

    def suspend(self, now=None):
        """Checkpoint once, then stop guessing time while Discord is disconnected."""

        if self._suspended:
            return
        checkpoint = self._now(now)
        self.update(checkpoint)
        self._suspended = True

    def update(self, now=None):
        if self._suspended:
            return
        checkpoint = self._now(now)
        self.timestamp_recently = checkpoint
        active_members = [
            (user_id, member.name)
            for user_id, member in self.members.items()
            if member._ing
        ]
        if not active_members:
            return

        elapsed_by_user = self.store.checkpoint_sessions(
            self.guild_id,
            active_members,
            checkpoint,
        )
        for user_id, member in self.members.items():
            if not member._ing:
                continue
            if user_id not in elapsed_by_user:
                # Repair an unexpected missing active row without inventing time.
                self.store.start_session(
                    self.guild_id,
                    user_id,
                    member.name,
                    checkpoint,
                )
                elapsed_by_user[user_id] = 0
            member.apply_persisted_elapsed(
                elapsed_by_user[user_id],
                checkpoint,
                active=True,
            )

    def add(self, member):
        self.members[str(member.userid)] = member

    def enter_exit(self, name, user_id, inout, now=None):
        if self._suspended:
            # Gateway 재개 시 현재 음성 멤버 전체를 reconcile하므로, 연결 중
            # 재생되는 개별 이벤트만으로 단절 구간을 추측하지 않는다.
            return
        user_id = str(user_id)
        event_at = self._now(now)
        member = self.members.get(user_id)

        if member is not None:
            member.name = name
            if inout == "in" and not member._ing:
                self.store.start_session(
                    self.guild_id,
                    user_id,
                    name,
                    event_at,
                )
                member.update_in(event_at)
            elif inout == "out" and member._ing:
                elapsed = self.store.end_session(
                    self.guild_id,
                    user_id,
                    name,
                    event_at,
                )
                if elapsed is None:
                    return
                member.apply_persisted_elapsed(elapsed, event_at, active=False)
            return

        if inout == "in":
            member = MemberRecord(name, user_id, now=event_at)
            self.store.start_session(
                self.guild_id,
                user_id,
                name,
                event_at,
            )
            member.update_in(event_at)
            self.add(member)

    def print_current(self):
        sorted_members = sorted(
            self.members.values(),
            key=lambda member: member.time,
            reverse=True,
        )

        message = str(self.timestamp).split(".")[0]
        message += "부터 시작된 기록입니다.\n"
        message += "".join(member.print_current() for member in sorted_members)
        return message

    def get_totals(self, start_date, end_date, *, legacy_period_kind=None):
        return self.store.aggregate(
            self.guild_id,
            start_date,
            end_date,
            legacy_period_kind=legacy_period_kind,
        )

    def print_week(self, now=None):
        checkpoint = self._now(now)
        self.update(checkpoint)
        end_date = checkpoint.date() - timedelta(days=checkpoint.weekday())
        start_date = end_date - timedelta(days=7)
        base = end_date - timedelta(days=1)
        year, month, week = get_iso_week_of_month(base)

        message = (
            f"{year}년 {month}월 {week}주차 결산 "
            f"({start_date:%Y-%m-%d} ~ {base:%Y-%m-%d})\n"
        )
        message += "------------------------------------------\n"
        message += format_stats_body(
            self.get_totals(
                start_date,
                end_date,
                legacy_period_kind="week",
            ),
            empty_message="이번 주 기록된 활동이 없습니다.",
        )
        message += "\n------------------------------------------"
        return message

    def print_month(self, now=None):
        checkpoint = self._now(now)
        self.update(checkpoint)
        end_date = checkpoint.date().replace(day=1)
        base = end_date - timedelta(days=1)
        start_date = base.replace(day=1)

        message = (
            f" {base.year}년 {base.month}월 월간 결산 "
            f"({start_date:%Y-%m-%d} ~ {base:%Y-%m-%d})\n"
        )
        message += "==========================================\n"
        message += format_stats_body(
            self.get_totals(
                start_date,
                end_date,
                legacy_period_kind="month",
            ),
            empty_message="이번 달 기록된 활동이 없습니다.",
        )
        message += "\n=========================================="
        return message

    def print_year(self, now=None):
        checkpoint = self._now(now)
        self.update(checkpoint)
        end_date = date(checkpoint.year, 1, 1)
        start_date = date(checkpoint.year - 1, 1, 1)
        base = end_date - timedelta(days=1)

        message = (
            f"{base.year}년 연간 결산 "
            f"({start_date:%Y-%m-%d} ~ {base.year}-12-31)\n"
        )
        message += "##########################################\n"
        message += format_stats_body(
            self.get_totals(
                start_date,
                end_date,
                legacy_period_kind="month",
            ),
            empty_message="이번 해 기록된 활동이 없습니다.",
        )
        message += "\n##########################################"
        return message

    def reset(self):
        checkpoint = self._now()
        self.timestamp = checkpoint
        self.timestamp_recently = checkpoint

        for user_id in list(self.members):
            if self.members[user_id]._ing:
                self.members[user_id].reset()
            else:
                del self.members[user_id]

    def export_stats(self):
        records = [
            {
                "date": row["activity_date"],
                "user_id": row["user_id"],
                "nickname": row["nickname"],
                "duration_microseconds": row["duration_microseconds"],
            }
            for row in self.store.get_daily_records(self.guild_id)
        ]
        return {
            "format_version": EXPORT_FORMAT_VERSION,
            "timezone": "Asia/Seoul",
            "duration_unit": "microseconds",
            "guild_id": self.guild_id,
            "daily_voice_times": records,
            "legacy_stats": self.store.get_legacy_stats(self.guild_id),
            "legacy_import": self.store.get_legacy_import_metadata(self.guild_id),
        }

    def replace_stats(self, stats):
        checkpoint = self._now()
        self.update(checkpoint)
        if is_daily_export(stats):
            records, legacy_stats, legacy_import = validate_daily_export(
                stats,
                expected_guild_id=self.guild_id,
            )
            self.store.replace_daily_records(
                self.guild_id,
                records,
                legacy_payload=legacy_stats,
                legacy_import=legacy_import,
                checkpoint_at=checkpoint,
            )
            return "daily"

        validate_stats(stats)
        self.store.replace_legacy_stats(
            self.guild_id,
            "uploaded_legacy_stats.json",
            deepcopy(stats),
        )
        return "legacy"

    # 이전 API를 호출하는 배포 스크립트가 깨지지 않도록 남긴 무동작 별칭.
    # 일일 행은 각 checkpoint transaction에서 즉시 저장된다.
    def save_stats(self):
        return None

    def save_in_progress_data(self):
        return None

    def load_in_progress_data(self):
        return None

    enterexit = enter_exit
    printing = print_current
    printing_week = print_week
    printing_month = print_month
    printing_year = print_year


def is_daily_export(stats):
    return (
        isinstance(stats, dict)
        and stats.get("format_version") in SUPPORTED_EXPORT_FORMAT_VERSIONS
    )


def validate_daily_export(stats, expected_guild_id=None):
    if not isinstance(stats, dict):
        raise ValueError("최상위 JSON 값은 객체여야 합니다.")
    format_version = stats.get("format_version")
    if format_version not in SUPPORTED_EXPORT_FORMAT_VERSIONS:
        raise ValueError("지원하지 않는 일일 통계 형식입니다.")
    if stats.get("timezone") != "Asia/Seoul":
        raise ValueError("timezone은 Asia/Seoul이어야 합니다.")
    if stats.get("duration_unit") != "microseconds":
        raise ValueError("duration_unit은 microseconds여야 합니다.")
    guild_id = stats.get("guild_id")
    if not isinstance(guild_id, str) or not guild_id:
        raise ValueError("guild_id는 빈 문자열이 아니어야 합니다.")
    if expected_guild_id is not None and guild_id != str(expected_guild_id):
        raise ValueError("다른 서버에서 만든 통계 백업은 복원할 수 없습니다.")

    raw_records = stats.get("daily_voice_times")
    if not isinstance(raw_records, list):
        raise ValueError("daily_voice_times는 배열이어야 합니다.")

    records = []
    keys = set()
    for index, record in enumerate(raw_records):
        location = f"daily_voice_times[{index}]"
        if not isinstance(record, dict):
            raise ValueError(f"{location} 값은 객체여야 합니다.")

        raw_date = record.get("date")
        if not isinstance(raw_date, str):
            raise ValueError(f"{location}의 date는 문자열이어야 합니다.")
        try:
            activity_date = date.fromisoformat(raw_date)
        except ValueError as error:
            raise ValueError(f"{location}의 date가 올바르지 않습니다.") from error
        if activity_date.isoformat() != raw_date:
            raise ValueError(f"{location}의 date는 YYYY-MM-DD 형식이어야 합니다.")

        user_id = record.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            raise ValueError(f"{location}의 user_id는 빈 문자열이 아니어야 합니다.")
        nickname = record.get("nickname")
        if not isinstance(nickname, str):
            raise ValueError(f"{location}의 nickname은 문자열이어야 합니다.")
        duration = record.get("duration_microseconds")
        _validate_non_negative_integer(
            duration,
            f"{location}의 duration_microseconds",
        )
        if duration > SQLITE_MAX_INTEGER:
            raise ValueError(
                f"{location}의 duration_microseconds가 SQLite 범위를 초과합니다."
            )
        if duration > MAX_DAILY_DURATION_MICROSECONDS:
            raise ValueError(
                f"{location}의 하루 기록은 24시간을 초과할 수 없습니다."
            )

        key = (raw_date, user_id)
        if key in keys:
            raise ValueError(f"{location}에 날짜/사용자 중복 기록이 있습니다.")
        keys.add(key)
        records.append(
            {
                "activity_date": raw_date,
                "user_id": user_id,
                "nickname": nickname,
                "duration_microseconds": duration,
            }
        )

    legacy_stats = stats.get("legacy_stats")
    if legacy_stats is not None:
        validate_stats(legacy_stats)
        legacy_stats = deepcopy(legacy_stats)
    legacy_import = None
    if format_version >= 3:
        legacy_import = _validate_legacy_import_backup(
            stats.get("legacy_import"),
            legacy_stats,
        )
    return records, legacy_stats, legacy_import


def _validate_legacy_import_backup(raw_import, legacy_stats):
    if raw_import is None:
        return None
    if not isinstance(raw_import, dict):
        raise ValueError("legacy_import는 객체 또는 null이어야 합니다.")
    if legacy_stats is None:
        raise ValueError("legacy_import를 복원하려면 legacy_stats가 필요합니다.")

    source_name = raw_import.get("source_name")
    source_sha256 = raw_import.get("source_sha256")
    algorithm_version = raw_import.get("algorithm_version")
    raw_cutover = raw_import.get("cutover_at")
    if not isinstance(source_name, str) or not source_name:
        raise ValueError("legacy_import.source_name이 올바르지 않습니다.")
    if algorithm_version != LEGACY_IMPORT_ALGORITHM_VERSION:
        raise ValueError("지원하지 않는 legacy 변환 알고리즘입니다.")
    payload_json = json.dumps(legacy_stats, ensure_ascii=False, sort_keys=True)
    expected_digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    if source_sha256 != expected_digest:
        raise ValueError("legacy_import 해시가 legacy_stats와 일치하지 않습니다.")
    if not isinstance(raw_cutover, str):
        raise ValueError("legacy_import.cutover_at은 ISO 8601 문자열이어야 합니다.")
    try:
        cutover = datetime.fromisoformat(raw_cutover)
    except ValueError as error:
        raise ValueError("legacy_import.cutover_at이 올바르지 않습니다.") from error
    if cutover.tzinfo is None:
        raise ValueError("legacy_import.cutover_at에는 시간대가 필요합니다.")
    cutover = normalize_kst(cutover)
    period_rows = normalize_legacy_period_rows(legacy_stats, cutover)
    legacy_user_ids = {row[3] for row in period_rows}

    raw_aliases = raw_import.get("aliases")
    if not isinstance(raw_aliases, list):
        raise ValueError("legacy_import.aliases는 배열이어야 합니다.")
    aliases = []
    seen = set()
    for index, raw_alias in enumerate(raw_aliases):
        location = f"legacy_import.aliases[{index}]"
        if not isinstance(raw_alias, dict):
            raise ValueError(f"{location} 값은 객체여야 합니다.")
        legacy_user_id = raw_alias.get("legacy_user_id")
        user_id = raw_alias.get("user_id")
        nickname = raw_alias.get("nickname")
        if legacy_user_id not in legacy_user_ids:
            raise ValueError(f"{location}의 legacy_user_id가 원본에 없습니다.")
        if not isinstance(user_id, str) or not user_id:
            raise ValueError(f"{location}의 user_id가 올바르지 않습니다.")
        if not isinstance(nickname, str):
            raise ValueError(f"{location}의 nickname이 올바르지 않습니다.")
        if legacy_user_id in seen:
            raise ValueError(f"{location}에 중복 legacy_user_id가 있습니다.")
        seen.add(legacy_user_id)
        aliases.append((legacy_user_id, user_id, nickname))

    return {
        "source_name": source_name,
        "source_sha256": source_sha256,
        "algorithm_version": algorithm_version,
        "cutover_at_us": datetime_to_epoch_microseconds(cutover),
        "period_rows": period_rows,
        "aliases": aliases,
    }


def sort_stats_by_time(stats):
    return dict(
        sorted(
            stats.items(),
            key=lambda item: item[1]["time"],
            reverse=True,
        )
    )


def sum_month_totals(year_data):
    """Legacy JSON helper retained for validating old backups."""

    year_total = {}
    for month in range(1, 13):
        month_data = year_data.get(f"{month}월", {})
        for user_id, user_data in month_data.get("total", {}).items():
            if user_id not in year_total:
                year_total[user_id] = {
                    "time": 0,
                    "nickname": user_data["nickname"],
                }
            year_total[user_id]["time"] += int(user_data["time"])
            year_total[user_id]["nickname"] = user_data["nickname"]
    return sort_stats_by_time(year_total)


def validate_stats(stats):
    """Validate the legacy aggregate JSON format without converting it to days."""

    if not isinstance(stats, dict):
        raise ValueError("최상위 JSON 값은 객체여야 합니다.")

    for year_key, year_data in stats.items():
        if not isinstance(year_key, str):
            raise ValueError("통계 키는 문자열이어야 합니다.")

        if year_key == "_in_progress":
            _validate_in_progress_stats(year_data)
            continue

        if not isinstance(year_data, dict):
            raise ValueError(f"{year_key} 값은 객체여야 합니다.")

        for month_key, month_data in year_data.items():
            if not isinstance(month_data, dict):
                raise ValueError(f"{year_key} {month_key} 값은 객체여야 합니다.")

            for period_key, period_data in month_data.items():
                _validate_period_stats(
                    period_data,
                    f"{year_key} {month_key} {period_key}",
                )


def _validate_period_stats(period_data, location):
    if not isinstance(period_data, dict):
        raise ValueError(f"{location} 값은 객체여야 합니다.")

    for user_id, user_data in period_data.items():
        if not isinstance(user_data, dict):
            raise ValueError(f"{location}의 {user_id} 기록은 객체여야 합니다.")
        _validate_non_negative_integer(user_data.get("time"), f"{location}의 time")
        if not isinstance(user_data.get("nickname"), str):
            raise ValueError(f"{location}의 nickname은 문자열이어야 합니다.")


def _validate_in_progress_stats(in_progress):
    if not isinstance(in_progress, dict):
        raise ValueError("_in_progress 값은 객체여야 합니다.")

    for user_id, user_data in in_progress.items():
        if not isinstance(user_data, dict):
            raise ValueError(f"_in_progress의 {user_id} 기록은 객체여야 합니다.")
        if not isinstance(user_data.get("name"), str):
            raise ValueError("_in_progress의 name은 문자열이어야 합니다.")
        _validate_non_negative_integer(
            user_data.get("time_week"),
            "_in_progress의 time_week",
        )
        _validate_non_negative_integer(
            user_data.get("time_month"),
            "_in_progress의 time_month",
        )


def _validate_non_negative_integer(value, location):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{location}은 0 이상의 정수여야 합니다.")


def format_stats_body(stats, empty_message):
    if not stats:
        return empty_message

    body = []
    for user_id, data in stats.items():
        if "duration_microseconds" in data:
            seconds = data["duration_microseconds"] // 1_000_000
        else:
            seconds = int(data["time"])
        time_str = str(timedelta(seconds=seconds))
        body.append(f"{time_str} : {data['nickname']}({user_id})")
    return "\n".join(body)


# 기존 코드와의 호환용 별칭
memb = MemberRecord
membermanager = MemberManager
