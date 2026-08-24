# Discord Voice Time Bot

Discord 음성 채널 체류 시간을 KST 날짜별로 저장하고 주간·월간·연간
정산을 게시하는 봇입니다.

## 저장 방식

통계는 기본적으로 `voice_time.db` SQLite 데이터베이스에 저장됩니다.
`VOICE_STATS_DB` 환경 변수로 경로를 바꿀 수 있습니다.

- `daily_voice_times`: `서버 ID + KST 날짜 + Discord 사용자 ID`별 체류 시간
- `active_voice_sessions`: 현재 접속 중인 세션과 마지막 반영 시각
- `member_profiles`: 표시 이름
- `legacy_json_archives`: 기존 `stats_<guild_id>.json` 원문 백업
- `legacy_period_totals`: 기존 JSON의 주간·월간 합계
- `legacy_imports`, `legacy_user_aliases`: 전환 시각과 과거 username/현재 ID 연결

음성 세션은 5분 체크포인트와 퇴장 이벤트에서 저장됩니다. 세션이 KST
자정을 넘으면 각 날짜의 행으로 나뉩니다. 주간(월~일), 월간, 연간
통계 테이블을 별도로 만들지 않으며 새 기록의 모든 정산은
`daily_voice_times`의 날짜 범위를 합산해 계산합니다. 기존 JSON을 한 번
가져온 경우에만 과거 주간 기준값 또는 월간 기준값을 같은 기간의 새 일일
기록과 합칩니다. 체크포인트 갱신과 일일 시간 증가는 하나의 SQL
트랜잭션으로 처리됩니다.

SQLite는 단일 봇 프로세스와 영속 로컬 디스크를 사용하는 현재 구조에
적합합니다. 여러 봇 인스턴스가 같은 DB를 공유하거나 실행 환경의 로컬
디스크가 휘발성이라면 저장소 계층을 PostgreSQL로 교체하는 것이 좋습니다.

## 기존 JSON에서 전환

처음 길드 관리자를 만들 때 같은 폴더의 `stats_<guild_id>.json`을 검증해
SQLite의 `legacy_json_archives`에 원문만 한 번 보관합니다. 실제 합계 전환은
봇을 멈춘 상태에서 `migrate_json_to_sqlite.py`를 한 번 실행합니다. 원본
파일은 어느 과정에서도 삭제하거나 수정하지 않습니다.

기존 JSON에는 주간·월간 중복 합계만 있고 날짜별 기록이 없으므로 정확한
일일 행으로 역변환할 수 없습니다. 이 도구는 임의 날짜를 만들지 않고 주간
합계와 월간 합계를 서로 다른 SQLite 기준값으로 보존합니다. 주간 정산은
과거 주간 값만, 월간·연간 정산은 과거 월간 값만 사용하므로 같은 시간이
두 번 더해지지 않습니다. `_in_progress.time_week`와 `time_month`는 지정한
전환 시각이 속한 주와 월의 이월값으로 각각 보존됩니다.

### 일회용 전환 순서

1. 기존 봇을 멈추고 `stats_<서버 ID>.json`과 현재 DB를 백업합니다.
2. `--cutover-at`에 기존 JSON 기록을 끝내고 SQLite 기록을 시작한 실제
   시각을 KST ISO 8601 형식으로 정합니다.
3. 먼저 검증만 실행한 뒤, 같은 인자로 실제 전환을 실행합니다.
4. 봇을 다시 시작하고 다음 정산 또는 `/get_json` 백업을 확인합니다.

```powershell
python migrate_json_to_sqlite.py "stats_*.json" `
  --database voice_time.db `
  --cutover-at "2026-08-25T12:00:00+09:00" `
  --dry-run

python migrate_json_to_sqlite.py "stats_*.json" `
  --database voice_time.db `
  --cutover-at "2026-08-25T12:00:00+09:00"
```

예시 시각은 그대로 쓰지 말고 실제 전환 시각으로 바꿔야 합니다. 파일명이
`stats_<서버 ID>.json` 형식이 아니면 파일 하나에 한해 `--guild-id`를
지정할 수 있습니다. 같은 파일과 전환 시각으로 다시 실행하면 안전하게
건너뜁니다. 다른 내용이나 시각으로 교체할 때만 `--force`가 필요합니다.
전환 시각이 날짜 중간이면 그 날짜에 이미 SQL 일일 기록이 있는 경우
이월값과 분리할 수 없으므로 도구가 적용을 거부합니다.

기존 봇이 주간 또는 월간 정산을 놓친 적이 있다면 `_in_progress`가 여러
기간의 누적값일 수 있습니다. 원본 JSON만으로 어느 날짜의 시간인지 복원할
수 없으므로 도구는 이를 경고하고 전환 시각이 속한 주/월에 보존합니다.
실제 적용 전에 `--dry-run` 경고와 원본을 확인하세요.

과거 JSON 키는 Discord 사용자 ID가 아니라 당시 username입니다. 봇을 다시
시작하면 현재 서버에서 username이 정확히 하나만 일치하는 회원을 현재
Discord ID와 자동 연결합니다. 이름이 바뀌었거나 중복되어 연결할 수 없는
기록은 삭제하지 않고 `legacy:<과거 username>`으로 표시합니다. 이름이 바뀐
사용자는 파일 하나를 전환할 때 `--alias "과거_username=현재_Discord_ID"`를
여러 번 지정해 명시적으로 연결할 수 있습니다. 자동 연결은 이미 확정된
연결을 덮지 않으며, `--alias`만 기존 연결을 명시적으로 교정합니다.

## 설정

`.env.example`을 `.env`로 복사한 뒤 값을 채웁니다.

```env
DISCORD_TOKEN=your_discord_bot_token
DEV_GUILD_ID=optional_test_guild_id
VOICE_STATS_DB=voice_time.db
```

Discord에서 서버 관리자가 채널을 설정합니다.

- `/set_log_channel`: 입퇴장 로그 채널
- `/set_settlement_channel`: 주간·월간·연간 정산 채널
- `/settings`: 현재 채널 설정 확인

## 백업과 복원

- `/get_json`: 현재 길드의 일일 행, legacy 원문, 전환 시각과 사용자 연결을
  버전 3 JSON으로 내보냅니다. 여러 길드가 같은 DB를 사용해도 다른 길드
  데이터는 포함하지 않습니다. JSON이 5MB를 넘으면 자동으로 `.json.gz`로
  압축합니다.
- `/upload_json`: 버전 2 또는 3 일일 `.json`/`.json.gz` 백업을
  트랜잭션으로 복원합니다. 버전 3은 변환된 과거 합계까지 재현합니다. 다른
  서버 ID의 백업은 거부합니다. 기존 형식
  `stats.json`을 올리면 일일 행으로 변환하지 않고 legacy 원문으로만
  보관하며, 이미 쌓인 일일 행은 지우지 않습니다.

버전 2 백업에는 전환 시각과 사용자 연결이 없으므로 일일 행과 legacy
원문만 복원됩니다. 새 DB에서 과거 합계까지 다시 쓰려면 원래 전환 시각으로
일회용 변환 도구를 다시 실행해야 합니다.

실행 중인 SQLite 파일을 직접 복사하는 대신 `/get_json`을 사용하는 것이
안전합니다. DB 파일을 운영 백업 시스템으로 복사해야 한다면 WAL을 포함한
SQLite 온라인 백업 절차를 사용하세요.

## 실행 및 테스트

```powershell
python bot.py
python -m unittest discover -s tests -v
```

주간 정산은 월요일 00:00 KST, 월간 정산은 매월 1일 00:00 KST에
실행됩니다. 연간 정산은 1월 1일 월간 정산 뒤에 실행됩니다. 정산 채널이
설정되지 않았더라도 일일 시간 저장은 계속됩니다.
