# flatten ETL — payload를 조회 가능한 테이블로 펼치기

날짜: 2026-08-21 · 상태: 승인됨 (대화에서 설계 합의) · 범위: A단계 (이 저장소)

## 배경과 목적

`artifacts`는 의도적으로 append-only 시계열이고, payload 본문은 디스크의
content-addressed `.json.gz` blob이다. 그래서 PostgREST(→ data-portal)는 인덱스
행만 볼 수 있고 조회수·제목·댓글 같은 실제 내용에는 닿지 못한다.

이 작업은 blob 내용을 **PostgreSQL 테이블로 증분 펼치는(flatten) ETL**을
추가한다. 스키마에 새 테이블이 생기면 기존 grant 장치(`postgres-bootstrap.sql`의
`ALTER DEFAULT PRIVILEGES`, stack의 `40-postgrest-tubedepth-grants.sh`)가
runtime 쓰기권한과 postgrest_anon 읽기권한을 자동으로 잇는다 — 이 저장소 밖을
고칠 필요 없이 data-portal에서 새 테이블이 보인다.

부수 효과: blob 30일 prune과 무관하게 정제된 시계열이 장기 보존된다.

## 범위

**포함 (A단계):**
- Alembic migration 1개: 아래 테이블 6개 생성
- `tubedepth flatten` CLI subcommand: 증분·멱등 ETL
- `deploy/`에 systemd 타이머 참조본 (`tubedepth-flatten.service/.timer`) 및
  compose 미러 패턴 — stack 반영은 사람이 한다
- 문서: `docs/status.md` 결정 기록, `CHANGELOG.md` Unreleased,
  `service-db.json` connection budget 갱신

**제외:**
- data-portal 전용 페이지·nginx 프록시 (B단계, data-portal 저장소의 별도 계획)
- retention/prune 정책 변경 (flatten은 prune과 독립)
- API route 추가 (없음 — `docs/api.md` 무변경)
- `video.related`, `video.sponsor_segments`, `channel.community` 펼치기
  (skip으로 집계만 하고, 필요해지면 후속)

## 테이블 설계

전부 `tubedepth` schema. migration이 `SET ROLE tubedepth_owner`로 만들므로
권한은 default privileges가 처리한다. 각 행은 출처 `artifact_id`
(= `artifacts.identifier`)를 남기되 **FK는 걸지 않는다** — retention이 artifact
행을 지워도 펼친 행은 남는 것이 목적이다.

### `video_snapshots` — 시계열 (행 추가만)

`video.metadata` payload 1건 → 1행.

| column | type | 비고 |
| --- | --- | --- |
| `artifact_id` | text PK | 멱등 키. `ON CONFLICT DO NOTHING` |
| `video_id` | text NOT NULL | |
| `fetched_at` | timestamptz NOT NULL | artifact의 관측 시각 |
| `title` | text NOT NULL | |
| `channel` | text | |
| `channel_id` | text | |
| `duration_seconds` | integer | |
| `view_count` | bigint | |
| `like_count` | bigint | |
| `comment_count` | bigint | |
| `published_at` | timestamptz | |
| `published_date` | date | payload가 정확한 시각을 못 준 경우의 대체 |

index: `(video_id, fetched_at)`.

### `listing_entries` — 순위 시계열 (행 추가만)

`search.videos` / `channel.videos` / `playlist.items` / `trending.videos`
payload 1건 → 목록 항목 수만큼 행.

| column | type | 비고 |
| --- | --- | --- |
| `artifact_id` | text | PK 앞부분 |
| `position` | integer | 목록 안 0-기반 순서 = 순위. PK 뒷부분 |
| `kind` | text NOT NULL | artifact kind 그대로 |
| `target` | text NOT NULL | 검색어·채널·playlist·region |
| `fetched_at` | timestamptz NOT NULL | |
| `video_id` | text NOT NULL | |
| `title` | text | |
| `view_count` | bigint | |
| `duration_seconds` | integer | |
| `channel` | text | |
| `channel_id` | text | |
| `published_at` | timestamptz | |

PK `(artifact_id, position)`, index `(target, fetched_at)`, index `(video_id)`.

### `channel_snapshots` — 시계열 (행 추가만)

`channel.about` payload 1건 → 1행.

| column | type | 비고 |
| --- | --- | --- |
| `artifact_id` | text PK | |
| `channel_id` | text NOT NULL | |
| `fetched_at` | timestamptz NOT NULL | |
| `name` | text | |
| `handle` | text | |
| `subscriber_count_approximate` | bigint | 반올림 값임 — 이름이 그 사실을 말한다 |
| `view_count` | bigint | 채널 누적 조회수 (정확값) |
| `video_count` | integer | |
| `country` | text | |

index: `(channel_id, fetched_at)`.

### `comments` — 중복 제거 코퍼스 (upsert)

`video.comments` payload 1건 → 댓글 수만큼 upsert. video_id는 payload에 없고
artifact `target`이 그것이다.

| column | type | 비고 |
| --- | --- | --- |
| `video_id` | text | PK 앞부분 (artifact target) |
| `comment_id` | text | PK 뒷부분 |
| `parent_id` | text | NULL = 최상위 |
| `text` | text NOT NULL | |
| `author` | text | |
| `author_id` | text | |
| `like_count` | bigint | 재관측 시 갱신 |
| `is_hearted_by_uploader` | boolean NOT NULL | 재관측 시 갱신 |
| `is_pinned` | boolean NOT NULL | 재관측 시 갱신 |
| `published_at` | timestamptz | |
| `first_seen_at` | timestamptz NOT NULL | 최초 관측 artifact의 fetched_at |
| `last_seen_at` | timestamptz NOT NULL | 최근 관측 artifact의 fetched_at |

upsert 규칙: `ON CONFLICT (video_id, comment_id) DO UPDATE` — 단
**더 새로운 관측일 때만** (`EXCLUDED.last_seen_at > comments.last_seen_at`)
`text/like_count/하트/고정/last_seen_at`을 덮는다. 과거 blob을 재처리해도
최신 관측이 과거 값으로 되돌아가지 않는다. `first_seen_at`은
`LEAST(comments.first_seen_at, EXCLUDED.first_seen_at)`.

index: `(video_id, published_at)`.

### `transcripts` — 최신본 유지 (upsert)

`video.transcript` payload 1건 → 1행. video_id는 artifact `target`.

| column | type | 비고 |
| --- | --- | --- |
| `video_id` | text | PK 앞부분 |
| `language` | text | PK 뒷부분 |
| `is_automatic` | boolean NOT NULL | |
| `full_text` | text NOT NULL | |
| `segment_count` | integer NOT NULL | |
| `fetched_at` | timestamptz NOT NULL | |

upsert: 더 새로운 `fetched_at`일 때만 덮는다.

### `flatten_progress` — 커서 1행

| column | type | 비고 |
| --- | --- | --- |
| `identifier` | text PK | 고정값 `"flatten"` (worker_control 패턴) |
| `cursor_fetched_at` | timestamptz NOT NULL | |
| `cursor_identifier` | text NOT NULL | 같은 시각 안의 순서 결속 |
| `updated_at` | timestamptz NOT NULL | |

## `tubedepth flatten` 동작

1. 커서를 읽는다 (없으면 epoch부터 = 전체 backfill).
2. `artifacts`에서 `(fetched_at, identifier) > 커서` **이고**
   `fetched_at < now() - 5분` 인 행을 `(fetched_at, identifier)` 오름차순으로
   배치 단위로 읽는다. 5분 지연은 동시 커밋 순서 역전으로 커서가 행을
   건너뛰는 것을 막는 안전 여유다.
3. kind별 라우팅: 위 표의 kind → 해당 테이블. `video.bundle`은 `parts`를
   같은 핸들러로 라우팅한다 (bundle의 artifact_id·fetched_at을 물려준다).
   그 외 kind는 skip으로 집계.
4. payload 읽기 실패(`FileNotFoundError` — prune됐거나 store 불일치)는 그
   artifact를 skip으로 집계하고 계속한다. 부분 실패가 전체를 멈추지 않는다.
5. payload 파싱은 관대하게: 없는 field는 NULL, 모르는 field는 무시. 오래된
   schema_version의 blob도 읽을 수 있는 만큼 펼친다. **payload 하나의 파싱
   실패는 그 artifact만 error로 집계하고 계속한다.**
6. 배치마다 upsert + 커서 전진을 **한 transaction**으로 커밋한다. 배치 크기는
   `statement_timeout=15s`·`transaction_timeout=60s` 안에서 안전한 크기
   (기본 200 artifacts, `--batch` 옵션)로 한다. 중간에 죽어도 다음 실행이
   커서부터 재개하고, upsert 멱등성 덕에 재처리는 무해하다.
7. 종료 시 집계를 표준 출력으로 보고: kind별 처리 수, skip 수(사유별),
   error 수, 커서 위치.
8. `--limit N` (한 번에 최대 N artifacts, 기본 무제한)과 `--dry-run`
   (커밋 없이 집계만)을 둔다. 첫 backfill 검증용.

변환 로직(payload dict → 행 값들)은 DB·CLI와 분리된 순수 함수 모듈
(`src/tubedepth/flatten.py`)로 두어 fixture 기반 단위 테스트가 가능해야 한다.

## 배포

- `deploy/tubedepth-flatten.service` + `.timer` (systemd 참조본, watch와 같은
  패턴). 주기 15분.
- `deploy/docker-compose.yml`에 flatten 서비스 항목 추가 (watch 미러 패턴).
- stack 반영(타이머 기동)은 사람이 한다 — 이 저장소는 참조본까지만.

## 제약 (Global Constraints)

- 코드·주석·commit message는 영어. Conventional Commits. commit마다
  `just check` green.
- migration은 이 저장소 밖 접속과 경합하지 않게 한 세션에서만 실행.
- `service-db.json`의 `workers_and_schedulers`를 0 → 1로 올린다 (flatten
  타이머가 runtime role 접속 1개를 쓴다). budget 검사 테스트가 있으면 함께.
- 새 소스 추가가 아니므로 `docs/api.md`/`api.ko.md`는 건드리지 않는다.
  README에 CLI 목록이 있으면 flatten을 더한다 (한/영 짝 유지).
- `docs/status.md`에 결정 기록(왜 FK가 없는지, 왜 5분 지연인지, 왜 upsert
  규칙이 관측 시각으로 보호되는지)을 남긴다. `CHANGELOG.md` Unreleased 갱신.
- 테스트: 변환 순수 함수는 fixture로, upsert 멱등성·커서 재개는 기존 DB 테스트
  패턴으로. 같은 blob을 두 번 flatten해도 행 수가 늘지 않는 것을 테스트로 고정.
