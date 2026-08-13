from copy import deepcopy
from datetime import datetime, timedelta
import json
import os


FILE_NAME = "stats.json"


def get_iso_week_of_month(date):
    # 해당 날짜가 속한 주의 목요일을 기준으로 ISO식 월/주차를 계산한다.
    target_thursday = date - timedelta(days=(date.weekday() - 3))
    base_year = target_thursday.year
    base_month = target_thursday.month

    first_day_of_month = target_thursday.replace(day=1)
    first_thursday = first_day_of_month + timedelta(
        days=(3 - first_day_of_month.weekday() + 7) % 7
    )
    week_number = (target_thursday - first_thursday).days // 7 + 1

    return [base_year, base_month, week_number]


class MemberRecord:
    def __init__(self, name, user_id):
        self.name = name
        self.userid = user_id
        self.time = timedelta(0)
        self._ing = False
        self.timestamp = datetime.now()
        self.time_week = timedelta(0)
        self.time_month = timedelta(0)

    def update_in(self):
        if not self._ing:
            self._ing = True
            self.timestamp = datetime.now()

    def update_out(self):
        self._ing = False
        time_adder = datetime.now() - self.timestamp
        self.time += time_adder
        self.time_week += time_adder
        self.time_month += time_adder

    def update(self):
        if self._ing:
            self.update_out()
            self.update_in()

    def print_current(self):
        elapsed = str(self.time).split(".")[0]
        return f"{elapsed} : {self.name}({self.userid})\n"

    def reset(self):
        self.time = timedelta(0)

    # 기존 코드와의 호환용 별칭
    printing = print_current


class MemberManager:
    def __init__(self, file_name=FILE_NAME):
        self.file_name = file_name
        self.members = {}
        self.timestamp = datetime.now()
        self.timestamp_recently = datetime.now()
        self.stats = self.load_stats()

    def load_stats(self):
        if os.path.exists(self.file_name):
            with open(self.file_name, "r", encoding="utf-8") as file:
                return json.load(file)
        return {}

    def save_stats(self):
        self._write_stats(self.stats)

    def replace_stats(self, stats):
        validate_stats(stats)

        replacement = deepcopy(stats)
        self._write_stats(replacement)

        self.stats = replacement
        for member in self.members.values():
            member.time_week = timedelta(0)
            member.time_month = timedelta(0)

        self.load_in_progress_data()

    def _write_stats(self, stats):
        temporary_file_name = f"{self.file_name}.tmp"
        try:
            with open(temporary_file_name, "w", encoding="utf-8") as file:
                json.dump(stats, file, indent=4, ensure_ascii=False)
            os.replace(temporary_file_name, self.file_name)
        finally:
            if os.path.exists(temporary_file_name):
                os.remove(temporary_file_name)

    def update(self):
        self.timestamp_recently = datetime.now()
        for member in self.members.values():
            member.update()
        self.save_in_progress_data()
        self.save_stats()

    def add(self, member):
        self.members[member.userid] = member

    def enter_exit(self, name, user_id, inout):
        if user_id in self.members:
            member = self.members[user_id]
            if member.name != name:
                member.name = name

            if inout == "in":
                member.update_in()
            if inout == "out":
                member.update_out()

        elif inout == "in":
            self.add(MemberRecord(name, user_id))
            self.members[user_id].update_in()

    def print_current(self):
        self.members = dict(
            sorted(
                self.members.items(),
                key=lambda item: item[1].time,
                reverse=True,
            )
        )

        message = str(self.timestamp).split(".")[0]
        message += "부터 시작된 기록입니다.\n"
        message += "".join(member.print_current() for member in self.members.values())
        return message

    def print_week(self):
        base = datetime.now() - timedelta(days=1)
        start_str = (base - timedelta(days=6)).strftime("%Y-%m-%d")
        end_str = base.strftime("%Y-%m-%d")
        year, month, week = get_iso_week_of_month(base)

        year_key = f"{year}년"
        month_key = f"{month}월"
        week_key = f"{week}주차"

        year_data = self.stats.setdefault(year_key, {})
        month_data = year_data.setdefault(month_key, {"total": {}})
        week_data = month_data.setdefault(week_key, {})

        for user_id, member in self.members.items():
            if member.time_week > timedelta(0):
                if user_id not in week_data:
                    week_data[user_id] = {"time": 0, "nickname": member.name}

                week_data[user_id]["time"] += int(member.time_week.total_seconds())
                member.time_week = timedelta(0)

        month_data[week_key] = sort_stats_by_time(week_data)

        message = f"{year_key} {month_key} {week_key} 결산 ({start_str} ~ {end_str})\n"
        message += "------------------------------------------\n"
        message += format_stats_body(
            month_data[week_key],
            empty_message="이번 주 기록된 활동이 없습니다.",
        )
        message += "\n------------------------------------------"

        self.clear_in_progress_time("time_week")
        self.save_stats()
        return message

    def print_month(self):
        base = datetime.now() - timedelta(days=1)
        year_key = f"{base.year}년"
        month_key = f"{base.month}월"

        start_str = base.replace(day=1).strftime("%Y-%m-%d")
        end_str = base.strftime("%Y-%m-%d")

        year_data = self.stats.setdefault(year_key, {})
        month_data = year_data.setdefault(month_key, {"total": {}})
        total_data = month_data["total"]

        for user_id, member in self.members.items():
            if member.time_month > timedelta(0):
                if user_id not in total_data:
                    total_data[user_id] = {"time": 0, "nickname": member.name}

                total_data[user_id]["time"] += int(member.time_month.total_seconds())
                member.time_month = timedelta(0)

        month_data["total"] = sort_stats_by_time(total_data)

        message = f" {year_key} {month_key} 월간 결산 ({start_str} ~ {end_str})\n"
        message += "==========================================\n"
        message += format_stats_body(
            month_data["total"],
            empty_message="이번 달 기록된 활동이 없습니다.",
        )
        message += "\n=========================================="

        self.clear_in_progress_time("time_month")
        self.save_stats()
        return message

    def reset(self):
        self.timestamp = datetime.now()
        self.timestamp_recently = datetime.now()

        for user_id in list(self.members):
            if self.members[user_id]._ing:
                self.members[user_id].reset()
            else:
                del self.members[user_id]

    def save_in_progress_data(self):
        """정산 전 진행 중인 time_week, time_month를 stats에 추가"""
        self.stats["_in_progress"] = {}
        for user_id, member in self.members.items():
            if member.time_week > timedelta(0) or member.time_month > timedelta(0):
                self.stats["_in_progress"][user_id] = {
                    "name": member.name,
                    "time_week": int(member.time_week.total_seconds()),
                    "time_month": int(member.time_month.total_seconds()),
                }

    def load_in_progress_data(self):
        if "_in_progress" not in self.stats:
            return

        for user_id, data in self.stats["_in_progress"].items():
            if user_id not in self.members:
                self.members[user_id] = MemberRecord(data["name"], user_id)

            self.members[user_id].time_week = timedelta(seconds=data["time_week"])
            self.members[user_id].time_month = timedelta(seconds=data["time_month"])

        del self.stats["_in_progress"]

    def clear_in_progress_time(self, key):
        if "_in_progress" in self.stats:
            for user_data in self.stats["_in_progress"].values():
                user_data[key] = 0

    # 기존 코드와의 호환용 별칭
    enterexit = enter_exit
    printing = print_current
    printing_week = print_week
    printing_month = print_month


def sort_stats_by_time(stats):
    return dict(
        sorted(
            stats.items(),
            key=lambda item: item[1]["time"],
            reverse=True,
        )
    )


def validate_stats(stats):
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
        time_str = str(timedelta(seconds=int(data["time"])))
        body.append(f"{time_str} : {data['nickname']}({user_id})")
    return "\n".join(body)


# 기존 코드와의 호환용 별칭
memb = MemberRecord
membermanager = MemberManager
