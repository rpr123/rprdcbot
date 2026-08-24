"""Import legacy ``stats_<guild_id>.json`` files into the SQLite store once.

The source JSON files are opened read-only and are never modified or deleted.
Run with ``--dry-run`` first to validate every input without creating the database.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import glob
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

from voice_stats_store import KST, VoiceStatsStore, normalize_legacy_period_rows
from voice_tracker import validate_stats


DEFAULT_DATABASE = os.environ.get("VOICE_STATS_DB", "voice_time.db")
STATS_FILE_PATTERN = re.compile(r"^stats_(?P<guild_id>[0-9]+)\.json$")


class MigrationError(Exception):
    """An expected CLI input or migration failure."""


@dataclass(frozen=True)
class MigrationSource:
    path: Path
    guild_id: str
    payload: dict


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "기존 stats_<guild_id>.json 통계를 검증해 SQLite로 한 번 가져옵니다. "
            "원본 JSON은 수정하거나 삭제하지 않습니다."
        )
    )
    parser.add_argument(
        "sources",
        nargs="+",
        metavar="JSON_OR_GLOB",
        help=(
            "stats_<guild_id>.json 파일 또는 glob 패턴. "
            "PowerShell에서는 glob을 따옴표로 감싸세요."
        ),
    )
    parser.add_argument(
        "--database",
        default=DEFAULT_DATABASE,
        metavar="PATH",
        help=(
            "대상 SQLite 파일 경로 "
            "(기본값: VOICE_STATS_DB 환경 변수 또는 voice_time.db)"
        ),
    )
    parser.add_argument(
        "--guild-id",
        metavar="ID",
        help=(
            "파일명 대신 사용할 Discord 서버 ID. "
            "확장 결과가 파일 하나일 때만 사용할 수 있습니다."
        ),
    )
    parser.add_argument(
        "--alias",
        action="append",
        default=[],
        metavar="OLD_USERNAME=DISCORD_USER_ID",
        help=(
            "이름이 바뀐 사용자의 과거 username을 현재 Discord ID와 연결합니다. "
            "파일 하나를 가져올 때 여러 번 지정할 수 있습니다."
        ),
    )
    parser.add_argument(
        "--cutover-at",
        required=True,
        metavar="ISO_DATETIME",
        help=(
            "JSON에서 SQLite로 전환한 시각(ISO 8601). "
            "시간대가 없으면 KST로 해석합니다. 진행 중 주/월의 귀속 기준이므로 "
            "기존 봇을 멈춘 실제 시각을 지정해야 합니다."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="JSON과 인자만 검증하고 SQLite를 생성하거나 변경하지 않습니다.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "같은 서버에 이미 가져온 데이터가 있어도 교체합니다. "
            "기본 동작은 동일한 가져오기를 안전하게 건너뜁니다."
        ),
    )
    return parser


def _validate_guild_id(value: str, location: str) -> str:
    if not re.fullmatch(r"[0-9]+", value) or int(value) <= 0:
        raise MigrationError(f"{location}의 서버 ID는 0보다 큰 숫자여야 합니다: {value!r}")
    return value


def _expand_sources(arguments: list[str]) -> list[Path]:
    paths: list[Path] = []
    seen: set[str] = set()

    for argument in arguments:
        literal = Path(argument).expanduser()
        if literal.is_file():
            matches = [literal]
        elif literal.exists():
            raise MigrationError(f"입력 경로가 파일이 아닙니다: {literal}")
        elif glob.has_magic(argument):
            matches = [
                Path(match)
                for match in sorted(glob.glob(argument))
                if Path(match).is_file()
            ]
            if not matches:
                raise MigrationError(f"glob과 일치하는 파일이 없습니다: {argument}")
        else:
            raise MigrationError(f"JSON 파일을 찾을 수 없습니다: {literal}")

        for match in matches:
            resolved = match.resolve(strict=True)
            key = os.path.normcase(os.fspath(resolved))
            if key not in seen:
                seen.add(key)
                paths.append(resolved)

    if not paths:
        raise MigrationError("가져올 JSON 파일이 없습니다.")
    return paths


def _guild_id_for_path(path: Path, override: str | None) -> str:
    if override is not None:
        return _validate_guild_id(override, "--guild-id")

    match = STATS_FILE_PATTERN.fullmatch(path.name)
    if match is None:
        raise MigrationError(
            f"파일명에서 서버 ID를 찾을 수 없습니다: {path.name}. "
            "stats_<guild_id>.json 형식을 사용하거나 --guild-id를 지정하세요."
        )
    return _validate_guild_id(match.group("guild_id"), path.name)


def _read_payload(path: Path) -> dict:
    def reject_duplicate_keys(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise MigrationError(f"JSON 객체에 중복 키가 있습니다: {key!r}")
            value[key] = item
        return value

    try:
        with path.open("r", encoding="utf-8-sig") as source_file:
            payload = json.load(source_file, object_pairs_hook=reject_duplicate_keys)
    except UnicodeDecodeError as error:
        raise MigrationError(f"UTF-8 JSON 파일이 아닙니다: {path}") from error
    except json.JSONDecodeError as error:
        raise MigrationError(
            f"JSON 문법 오류: {path} ({error.lineno}행 {error.colno}열: {error.msg})"
        ) from error
    except OSError as error:
        raise MigrationError(f"JSON 파일을 읽을 수 없습니다: {path} ({error})") from error

    try:
        validate_stats(payload)
    except ValueError as error:
        raise MigrationError(f"기존 통계 형식이 올바르지 않습니다: {path} ({error})") from error
    return payload


def _parse_cutover_at(value: str | None) -> datetime:
    if value is None:
        return datetime.now(KST)

    normalized = value.strip()
    if normalized.endswith(("Z", "z")):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise MigrationError(
            f"--cutover-at은 ISO 8601 날짜/시각이어야 합니다: {value!r}"
        ) from error

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed.astimezone(KST)


def _load_sources(paths: list[Path], guild_id_override: str | None) -> list[MigrationSource]:
    if guild_id_override is not None and len(paths) != 1:
        raise MigrationError("--guild-id는 입력 파일이 하나일 때만 사용할 수 있습니다.")

    sources = [
        MigrationSource(
            path=path,
            guild_id=_guild_id_for_path(path, guild_id_override),
            payload=_read_payload(path),
        )
        for path in paths
    ]

    path_by_guild: dict[str, Path] = {}
    for source in sources:
        previous = path_by_guild.get(source.guild_id)
        if previous is not None:
            raise MigrationError(
                f"한 번의 실행에 서버 ID {source.guild_id} 파일이 둘 이상 포함되었습니다: "
                f"{previous}, {source.path}"
            )
        path_by_guild[source.guild_id] = source.path
    return sources


def _parse_aliases(
    values: list[str],
    sources: list[MigrationSource],
    cutover_at: datetime,
):
    if not values:
        return []
    if len(sources) != 1:
        raise MigrationError("--alias는 입력 JSON 파일이 하나일 때만 사용할 수 있습니다.")

    period_rows = normalize_legacy_period_rows(sources[0].payload, cutover_at)
    nickname_by_user = {}
    for row in period_rows:
        nickname_by_user[row[3]] = row[4]

    aliases = {}
    for value in values:
        if "=" not in value:
            raise MigrationError(
                f"--alias는 OLD_USERNAME=DISCORD_USER_ID 형식이어야 합니다: {value!r}"
            )
        legacy_user_id, user_id = value.split("=", 1)
        user_id = user_id.strip()
        if not legacy_user_id or legacy_user_id not in nickname_by_user:
            raise MigrationError(
                f"--alias의 과거 username이 JSON에 없습니다: {legacy_user_id!r}"
            )
        if not re.fullmatch(r"[0-9]+", user_id) or int(user_id) <= 0:
            raise MigrationError(
                f"--alias의 Discord 사용자 ID는 0보다 큰 숫자여야 합니다: {user_id!r}"
            )
        previous = aliases.get(legacy_user_id)
        if previous is not None and previous[0] != user_id:
            raise MigrationError(
                f"과거 username {legacy_user_id!r}에 서로 다른 ID가 지정됐습니다."
            )
        aliases[legacy_user_id] = (
            user_id,
            nickname_by_user[legacy_user_id],
        )
    return [
        (legacy_user_id, user_id, nickname)
        for legacy_user_id, (user_id, nickname) in aliases.items()
    ]


def _print_plan(
    sources: list[MigrationSource],
    database: str,
    cutover_at: datetime,
    *,
    dry_run: bool,
    force: bool,
) -> None:
    mode = "검증만" if dry_run else ("강제 교체" if force else "멱등 가져오기")
    print(f"모드: {mode}")
    print(f"대상 DB: {os.path.abspath(database)}")
    print(f"전환 시각: {cutover_at.isoformat(timespec='seconds')}")
    for source in sources:
        print(f"- 서버 {source.guild_id}: {source.path}")
        in_progress = source.payload.get("_in_progress", {})
        active_carry = sum(
            1
            for user_data in in_progress.values()
            if user_data.get("time_week", 0) or user_data.get("time_month", 0)
        )
        if active_carry:
            print(
                "  주의: _in_progress "
                f"{active_carry}명은 전환 시각이 속한 주/월에 귀속됩니다. "
                "기존 봇에서 정산을 놓친 적이 있다면 정확한 기간 복원이 "
                "불가능하므로 적용 전에 원본을 확인하세요."
            )


def run(args: argparse.Namespace) -> int:
    paths = _expand_sources(args.sources)
    sources = _load_sources(paths, args.guild_id)
    cutover_at = _parse_cutover_at(args.cutover_at)
    for source in sources:
        try:
            normalize_legacy_period_rows(source.payload, cutover_at)
        except ValueError as error:
            raise MigrationError(
                f"기존 통계 기간을 변환할 수 없습니다: {source.path} ({error})"
            ) from error
    aliases = _parse_aliases(args.alias, sources, cutover_at)
    _print_plan(
        sources,
        args.database,
        cutover_at,
        dry_run=args.dry_run,
        force=args.force,
    )
    if aliases:
        print(f"명시적 사용자 연결: {len(aliases)}명")

    if args.dry_run:
        print(f"검증 완료: {len(sources)}개 파일. SQLite는 생성하거나 변경하지 않았습니다.")
        return 0

    import_method = getattr(VoiceStatsStore, "import_legacy_stats", None)
    if not callable(import_method):
        raise MigrationError(
            "현재 VoiceStatsStore에 import_legacy_stats API가 없습니다. "
            "저장소 migration 구현을 함께 배포한 뒤 다시 실행하세요."
        )

    try:
        store = VoiceStatsStore(args.database)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        raise MigrationError(f"SQLite를 열거나 초기화할 수 없습니다: {error}") from error

    # A glob may contain several guilds.  Check every known target conflict before
    # applying the first one so a missing --force cannot leave a partial batch.
    for source in sources:
        try:
            store.check_legacy_import(
                source.guild_id,
                source.payload,
                cutover_at,
                force=args.force,
            )
        except (RuntimeError, ValueError, sqlite3.Error) as error:
            raise MigrationError(
                f"서버 {source.guild_id} 사전 확인 실패 ({source.path}): {error}"
            ) from error

    imported = 0
    skipped = 0
    for source in sources:
        try:
            changed = store.import_legacy_stats(
                source.guild_id,
                os.fspath(source.path),
                source.payload,
                cutover_at,
                force=args.force,
            )
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
            raise MigrationError(
                f"서버 {source.guild_id} 가져오기 실패 ({source.path}): {error}"
            ) from error

        if changed is False:
            skipped += 1
            print(f"건너뜀: 서버 {source.guild_id} (이미 같은 데이터가 반영됨)")
        else:
            imported += 1
            print(f"완료: 서버 {source.guild_id}")

    if aliases:
        mapped = store.map_legacy_users(
            sources[0].guild_id,
            aliases,
            overwrite=True,
        )
        print(f"사용자 연결 반영: {mapped}명")

    print(f"결과: 반영 {imported}개, 건너뜀 {skipped}개")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except MigrationError as error:
        print(f"오류: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
