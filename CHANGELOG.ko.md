# 변경 기록

[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) 형식이고, 버전은
[semver](https://semver.org)를 따른다.

*[English](CHANGELOG.md) (정본)*

여기서 관리되는 버전은 둘이고 서로 독립적으로 움직인다. **패키지 버전**은 아래 목록의
번호로, `src/tubedepth/__init__.py`에 쓰여 있고 `tubedepth version`과 `GET /healthz`가
보고한다. **`/v1` HTTP 계약**은 [`docs/api.ko.md`](docs/api.ko.md)에 대고 작성한
클라이언트가 깨질 때만 움직인다.

1.0 이전에는 minor 상향에서 수집 payload 모양이 바뀔 수 있다. 저장된 artifact는 각 소스의
`schema_version`으로 키가 잡히므로, 예전 payload는 예전 모양 그대로 읽힌다.

릴리스 절차: [`docs/releasing.md`](docs/releasing.md).

## [Unreleased]

### Added

- **watch list가 댓글을 수확할 수 있고, 플레이리스트를 감시할 수 있다.** listing
  directive에 `+comments` 변형 — `channel+comments`, `search+comments`,
  `playlist+comments` — 이 생겼다. 모든 listing directive가 이미 큐잉하는
  `video.metadata`에 더해, 찾아낸 영상마다 `video.comments` 잡으로도 퍼진다.
  기본값이나 플래그가 아니라 줄 단위 opt-in인 이유: 댓글은 이 시스템에서 가장
  비싼 kind라서, 그것을 수확하는 스케줄은 목록 크기를 정하는 바로 그 자리에
  적혀 있어야 한다. `playlist` directive도 새로 생겼다. 채널의 `/videos` 탭에는
  Shorts도 지난 라이브도 없어서, 업로드 전체 이력은 원래 `UU…` 플레이리스트에
  대한 `playlist.items`였다 — 이제 watch list가 그것을 말할 수 있다.

  기계적으로는, 후속이 둘인 줄은 같은 listing을 후속마다 하나씩 두 번 큐잉한다.
  잡은 `follow_up_kind`를 정확히 하나 갖기 때문이다. 첫 잡만 신선도 기간을
  강제로 넘기고, 둘째는 첫째가 방금 쓴 캐시를 탄다 — listing의 artifact 이력은
  회차당 두 건이 아니라 한 건씩 쌓인다.

## [1.1.0] - 2026-08-21

### Changed

- **`/v1`이 더 이상 기본적으로 `X-API-Key`를 요구하지 않는다.** 이 서비스는 사설망에
  배포되어 플릿의 다른 서비스가 호출한다. 거기서 호출자마다 키를 발급하는 것은 감사
  컬럼과 레이트 리밋을 얻는 대신 배포하고 교체해야 할 비밀을 하나 늘리는 일이었다.
  인증은 이제 `TUBEDEPTH_REQUIRE_API_KEY`(`1`/`true`/`yes`/`on`) 뒤에 있고, 설정하지
  않으면 꺼져 있으며 시작할 때 한 번 읽는다. 예도 아니오도 아닌 값은 아니오로 읽지 않고
  시작 시점에 거부한다.

  삭제가 아니라 스위치인 이유: "사설망에 있다"와 "닿는다"의 차이는 방화벽 규칙 하나이고,
  다시 켜면 모든 401과 키별 할당량이 이전 그대로 돌아온다. 키는 여전히
  `tubedepth key`로 발급·조회·폐기한다. **키를 요구하지 않는 인스턴스에 키를 보내도
  검증은 그대로 이루어지므로** 호출자는 자기 귀속 이력과 할당량을 유지하고, 폐기된 키는
  조용히 익명 접근으로 승격되는 대신 실패한다. 키 없이 등록된 잡은 지어낸 식별자 대신
  `api_key_id`를 남기지 않는다.

  **이 인스턴스가 플릿 밖에서도 닿는다면 업그레이드 전에
  `TUBEDEPTH_REQUIRE_API_KEY=1`을 설정할 것** — 그러지 않으면 업그레이드가 문을 연다.

## [1.0.3] - 2026-08-21

### Fixed

- **드레인 중에도 정지 요청이 존중된다 (#35).** 정지 이벤트를 드레인 사이에서만
  확인했기 때문에, 깊은 큐에서 `systemctl stop`은 타임아웃이 SIGKILL을 배달할
  때까지 새 job을 계속 집었고 버려진 lease는 만료를 다 기다렸다. 이제 pause
  플래그를 읽는 모든 곳에서 정지도 읽는다: 진행 중인 job은 끝내고, 새 것은
  집지 않는다.
- **claim 경합에서 진 것이 빈 큐로 읽히지 않는다 (#37).** 후보 SELECT가
  `FOR UPDATE SKIP LOCKED`를 쓰므로, 경합된 후보 대신 다음 집을 수 있는 job이
  돌아온다 — QUEUED가 남아 있는데 드레인이 끝나던 거짓 "할 일 없음"이 사라졌다.
- **`prune`의 불균형 스윕 가드가 삭제 전에, 스윕이 실제로 마주할 카운트로
  판정한다 (#31).** 이전에는 age 삭제 뒤에 삭제 전 카운트로 돌아서, 이 가드가
  막으려고 존재하는 바로 그 부분-전송 상태를 통과시켰다.
- **`prune`이 행 삭제가 커밋된 뒤에만 payload 파일을 지운다 (#32).** 커밋이
  실패하면 이전에는 행은 돌아오는데 파일은 이미 없었다; 이제 같은 실패에서
  행과 파일이 모두 남고, 둘 사이의 크래시는 다음 스윕이 수거할 고아 파일만
  남긴다.
- **트렌딩 페이지네이션이 아무것도 더하지 않는 페이지에서 끝난다 (#36).**
  `nextPageToken` 옆의 빈 `items` — 또는 반복되는 토큰 — 이 lease를 쥔 채
  랩마다 Data API 쿼터를 태우며 무한 루프를 돌았다.
- **`GET /v1/jobs/{job_id}/result`가 철회된 payload를 거부한다 (#34).** 철회된
  schema version으로 수집된 payload가 같은 바이트인데 artifact 경로로는 410,
  job 경로로는 200이었다. 이제 하나의 공유 게이트가 두 문을 모두 지킨다.
- **`tubedepth migrate`가 데이터베이스 비밀번호를 출력하지 않는다 (#30).**
  migrator 자격증명이 셸 스크롤백, journalctl, `docker compose logs`에
  남았다; URL을 담는 모든 메시지가 비밀번호를 `***`로 렌더링한다.
- **`tubedepth transfer`가 컷오버 이전 소스를 사전 점검한다 (#33).** 구버전
  SQLite 파일은 없는 컬럼에서 전송 도중에 죽었고 (`--dry-run`도 그럴듯한
  카운트를 찍은 뒤 같은 방식으로 죽었다); 이제 전송이 각 결손을 대며 앞에서
  거부하고, `tubedepth migrate`로 파일을 먼저 끌어올릴 수 있다 — 업그레이드
  직후의 카운트가 방금 마이그레이션하라던 SQLite 소스를 거부하지 않는다.

## [1.0.2] - 2026-08-21

### Fixed

- **외부 서버를 상대로 한 `docker compose up`이 local 프로파일의 비밀번호를
  요구하지 않는다.** compose는 프로파일을 적용하기 전에 파일 전체를
  보간하므로, `postgres` 서비스의 `TUBEDEPTH_LOCAL_*` 비밀번호 세 개에 붙은
  `:?` 필수 마커가 기본 배포 — 외부 데이터베이스 — 를 쓰지도 않을 값 때문에
  실패시켰다. 이제 `:-`이며, 값 없이 `--profile local`을 올리면 그 값을
  실제로 쓰는 곳 — postgres 이미지와 initdb 래퍼 — 이 변수 이름을 대며
  거부한다.

## [1.0.1] - 2026-08-21

### Fixed

- **`deploy/.env`는 더 이상 Docker build context에 실리지 않는다.**
  `.dockerignore`의 `.env` 제외 규칙이 context 루트에서만 매칭됐는데, compose
  파일은 저장소 루트에서 빌드하고(`context: ..`) 문서화된 절차는 진짜 secret을
  `deploy/.env`에 두므로, 그 파일이 빌드할 때마다 daemon으로 tar되어 갔다.
  이제 `**/.env`와 `**/.env.*`이고, 마지막 매치가 이기므로 `!**/.env.example`은
  그 뒤에 남는다.
- **로컬 postgres healthcheck는 TCP를 찌른다: `pg_isready -h 127.0.0.1`.**
  이미지의 entrypoint는 initdb 동안 socket만 여는 임시 서버를 돌리는데, socket
  probe는 bootstrap이 아직 role을 만드는 중에도 ready라고 답할 수 있었다 —
  `migrate`가 일찍 풀려나 실패하고, 첫 부팅에서 `api`·`worker`·`watch`가 영영
  뜨지 못했다. 임시 서버는 TCP를 아예 열지 않으므로, TCP 응답은 bootstrap이
  끝난 뒤의 진짜 서버를 뜻한다 — `tool/checks/test`가 기록해 둔 것과 같은 선택.
- **timezone offset 없는 `since`/`until`은 500이 아니라 422다.**
  `GET /v1/jobs?since=2026-08-21`은 naive datetime으로 파싱되어 컬럼 비교에서
  터졌고, 순수한 읽기에서 저장을 거부한다는 메시지와 함께 문서화된 에러 모양
  밖의 500 `internal_error`가 나갔다. 이제 두 목록 라우트 모두 무엇을 보내야
  하는지 짚어 주는 422 `invalid_request`로 답한다.

## [1.0.0] - 2026-08-21

### Added

- **Docker 이미지, 그리고 전체를 돌리는 compose 예제 (#22).** 이미지 하나,
  `ENTRYPOINT ["tubedepth"]`. 그래서 `deploy/docker-compose.yml`의 서비스 넷
  — `migrate`·`api`·`worker`·`watch` — 은 `command:`만 다르다. `migrate`는
  one-shot이고 나머지 셋은 그것이 *성공적으로 끝나는 것*을 기다린다. 부팅 경로가
  DDL을 내지 않기 때문이고(#14), 시작할 때 migrate하는 컨테이너는 그 변경을 도로
  무르는 것이기 때문이다. `api`와 `worker`는 리뷰가 아니라 YAML anchor로 env
  block 하나를 공유한다. listing·comment·trending cap이 cache key의 일부라서,
  둘이 다른 값을 읽으면 서로 다른 질문에 답한다. 이미지에는 `HEALTHCHECK`가
  없다 — 포트를 열지 않는 worker와, 끝나는 게 정상인 one-shot에게 틀린 검사다 —
  그래서 API의 healthcheck는 compose 파일에 있다. 기본 전제는 외부 fleet
  PostgreSQL이고, `--profile local`이 `deploy/postgres-bootstrap.sql` 그 자체로
  세팅되는 PostgreSQL을 띄운다. 명령은 `just compose-up` 하나. 레지스트리
  배포는 없다.

- **`tubedepth watch <목록>` — 채널·트렌드 키워드·지역을 한 스케줄에서 수집한다 (#20).** 목록에는
  타입이 붙는다: 한 줄에 `video`·`channel`·`search`·`trending` 중 하나와 타깃 하나. 한 줄에 id
  하나짜리 목록으로는 이것을 표현할 수 없었다 — `UCxxx`, `kpop debut`, `KR`은 문자열만 보고
  구분되지 않는 서로 다른 타깃 타입이다. 그래서 타입을 적게 했고, 넷 중 하나가 아닌 directive는
  조용히 아무것도 수집하지 않는 대신 줄 번호를 짚어 거부한다. 큐에 넣는 모든 잡은 줄별 플래그
  없이 신선도 기간을 강제로 넘긴다. listing 줄은 다시 열거되어 새 영상이 나타나고, 거기서 퍼져
  나가는 영상별 후속 잡은 캐시가 관장하는 상태로 남는다. **`channel`·`search`·`trending` 한 줄은
  수집 한 건이 아니라 `TUBEDEPTH_LISTING_LIMIT`건까지다** — 산수는
  `deploy/watchlist.example.txt`에 있다. `deploy/tubedepth-watch.timer`가 매시간 돌리고,
  타이머가 없는 환경에서는 `--every 초`가 상주하며 매 회차마다 목록을 다시 읽으므로 편집에
  재시작이 필요 없다. 첫 회차가 목록을 읽지 못하면 0이 아닌 코드로 끝나고, 이후 회차의 실패는
  로그만 남기고 건너뛴다 — 편집하다 만 파일이 수집을 멈추는 원인이 되어서는 안 되기 때문이다.

- **`tubedepth transfer --from <url> --to <url>`, 그리고 그 뒤의 `tubedepth.transfer.transfer()`.**
  #15와 #24는 PostgreSQL 컷오버의 데이터 이동을 "데이터를 옮긴다. 여섯 테이블." 한 줄로만
  명시하는데, 지금까지 이 저장소에는 한 데이터베이스에서 다른 데이터베이스로 행을 옮긴 코드가
  전혀 없었다 — 대안은 재수집이 불가능한 248개 타깃의 관측을 손으로 `pg_dump`-and-hope 하는
  것이었다. 이 transfer는 dialect 수준 dump가 아니라 model 기반이다 — 모든 행을 ORM으로 읽어
  타깃에 컬럼 단위로 재구성하며, 이것이 `identifier` primary key와 `fetched_at`을 microsecond
  까지 그대로 보존하는 방법이고 — `pg_dump`가 보지 못하는 실패인데 — 모든 instant를
  `UtcDateTime`을 통해 다시 흘려보내 naive datetime을 거부하게 만든다. 그렇지 않으면 SQLite의
  timezone 없는 저장이 PostgreSQL의 `timestamptz` 아래에서 조용히 잘못된 instant가 된다.
  `artifacts`는 `fingerprint`에 unique 제약이 의도적으로 없으므로, 이미 행을 가진 타깃은
  거부한다 — 부분적으로 두 번 실행되면 아무것도 걸러내지 못한 채 모든 관측이 중복된다.
  payload store는 절대 import하지 않는다 — `docs/shared-postgres.md` 규정 7은 index와
  `TUBEDEPTH_DATA_DIR/payloads`의 payload bytes를 하나의 복구 세트로 다루고, 이 transfer는
  그중 index 절반만 옮긴다. `--dry-run`은 소스의 모든 테이블 개수를 세고 아무것도 쓰지 않아서,
  운영자가 컷오버를 실행하기 전에 숫자 여섯 개를 먼저 보게 한다.
- **샘플러 — 이력이 쌓이기 시작한다.** `deploy/`의 `tubedepth-sample.timer`가 매시간 watch
  list를 강제로 다시 수집한다. 목록은 `~/.config/tubedepth/watchlist.txt`이고 한 줄에 영상 id
  하나이며, 형식은 `deploy/watchlist.example.txt`에 있다. 켜지 않으면 동작하지 않는다.
  velocity는 두 관측의 차이이고 관측은 실시간으로만 쌓이므로, 필요해지기 전에 시작할 값어치가 있다.
  **이 릴리스가 나가기 전에 위의 `tubedepth watch`와 `tubedepth-watch` 유닛 짝으로 대체됐다** —
  존재 이유는 그대로이고, 형식과 유닛이 바뀌었다.
- **`tubedepth enqueue --refresh`와 `--from-file`.** 앞은 `POST /v1/jobs`가 가진 강제 수집을
  커맨드라인에도 두는 것이고, 뒤는 한 줄에 하나씩 타깃을 읽어서 스케줄이 `ExecStart`에 id 서른
  개를 싣는 대신 목록 파일을 가리키게 한다. 읽을 수 없는 목록은 빈 목록으로 취급하지 않고 거부한다.


- **`tubedepth pause`와 `tubedepth resume`.** `PATCH /v1/control`이 쓰는 것과 같은 행을 API 없이
  건드린다 — API에 의존하는 것이 잘못이었다. API가 죽어 있거나 애초에 설치되지 않았다면, 워커는
  가장 멈추고 싶은 프로세스이면서 멈출 수 없는 프로세스였다.
- **`TUBEDEPTH_LISTING_LIMIT`과 `TUBEDEPTH_COMMENT_LIMIT`.** 100개 상한은 등록 시점에 고정된
  생성자 기본값이라, 그보다 영상이 많은 채널은 소스를 고치지 않고는 통째로 수집할 수 없었다.
  지금 올려도 안전한 이유는 상한이 캐시 키에 들어갔기 때문이다 — 그 전에 올렸다면 1,000개를
  요청한 쪽에 캐시된 100개짜리 리스팅이 나갔을 것이다. **API 유닛과 워커 유닛에 동일하게
  설정한다** — 각 프로세스가 한 번씩 읽고, `GET /v1/sources`가 실효값을 보고하므로 둘을 비교할 수 있다.
- **스키마가 이미 기록하고 있던 질문에 답한다.** 컬럼 넷이 매번 기록되면서 아무도 읽지 않고 있었다.
  `last_error_message`가 이제 `/healthz`와 대시보드까지 온다 — 코드는 `parse_mismatch`라고 하고
  메시지는 더 이상 맞지 않는 렌더러 이름을 말하는데, 무엇을 고칠지 알려주는 것은 후자뿐이다.
  `api_key_id`와 `claimed_by`는 `GET /v1/jobs`에 실려서, "어느 클라이언트가 폭주 중인가"와
  "어느 워커가 이 잡을 붙들고 있나"에 SQLite를 직접 열지 않고 답할 수 있다. `tubedepth key list`는
  각 키가 마지막으로 쓰인 때를 말한다 — 폐기하기 전에 누구나 묻는 그 질문이다.
- **`GET`·`PATCH /v1/control` — 운영자가 워커를 멈출 수 있다.** API와 워커는 의도적으로 별개
  프로세스라 한쪽이 다른 쪽에 손을 뻗을 수 없다. 제어는 워커가 매 drain 시작에 읽는 행이고,
  재시작 루프가 그것을 10초쯤 안에 효력이 생기는 일시정지로 만든다. 일시정지는 "새로 집지 않는다"다:
  큐에 있던 잡은 그대로 남고, 들어오는 길에 실패 처리되는 것도 없으며, 이미 실행 중인 잡은
  끝까지 간다 — 진행 중인 추출은 끝날 때까지 계속 요청을 쓴다. 버튼은 대시보드에 있다.
- **`/healthz`가 각 경로에 무엇이 허용되는지 보고한다.** rate controller의 상태가 워커 메모리에만
  있어서, 밖에서 보면 격리된 lane과 빈 큐가 똑같아 보였다. 이제 `window`, `in_flight`, 그리고
  벽시계로 변환된 격리 기한을 워커가 기록하고 대시보드가 보여준다.
- **`POST /v1/jobs/batch`.** kind 하나, 타깃 여럿, 요청 하나 — 그리고 분당 60 allowance에 대한
  차감도 하나다. 영상 100개짜리 스윕을 *표현할 수 있는* API와 *실행할 수 있는* API의 차이가 이것이다.
  전부 아니면 전무: 한 줄이라도 큐에 넣기 전에 모든 타깃을 정규화하므로, 잘못된 id 하나면 99개를
  넣고 202로 답하는 대신 배치를 거부한다. 이미 보유한 타깃은 payload가 아니라 digest로 알려준다.
  대시보드는 타깃이 둘 이상이면 이 라우트를 쓴다.
- **대시보드가 이제 큐를 보기만 하지 않고 움직인다.** 수집 요청, 신선도 무시 강제, 잡 취소,
  실패한 것 다시 요청, 결과 열람, 그리고 digest를 눌러 그 관측을 읽기 — 죽은 텍스트였던 그 칸이다.
  200으로 답한 제출은 "아무 일도 없었던 것처럼" 보이는 대신 그렇다고 말하고, 취소했지만 running인
  잡은 추출이 아직 요청을 쓰고 있다고 말한다. 서버 변경은 없다 — 저 라우트들은 전부 이미 있었다.
- **`trending.videos` — YouTube 자신이 인기라고 부르는 것.** 트렌딩 페이지는 사라졌고 긁어올
  순위도 남아 있지 않지만, Data API의 `chart=mostPopular`는 살아남았다: 2026-08-20 확인,
  지역당 200건. 관측이 아니라 순위를 보고하는 유일한 kind다 — 나머지는 전부 두 번 수집하면
  트렌드가 되지만, YouTube 자신의 순서는 몇 번을 샘플링해도 복원할 수 없다.

  자체 lane을 쓴다. 나머지가 경쟁하는 per-address 예산이 아니라 Google 쿼터를 쓰므로, 한쪽의
  격리가 다른 쪽을 조이면 안 되기 때문이다. `TUBEDEPTH_DATA_API_KEY`를 설정한다. 없으면 그
  kind만 설정 오류로 실패하고 나머지는 영향받지 않는다.

  payload가 `VideoListing`이라 `--then`이 그대로 동작한다:
  `tubedepth enqueue trending.videos KR --then video.metadata` 한 줄이 지역 하나를 트렌딩
  영상별 메타데이터 잡으로 바꾼다.
- **payload 모델을 바꾸고 `schema_version`을 안 올리면 CI가 거부한다.** DB 쪽 절반은
  `tests/test_migrations.py`가 늘 잡아왔지만 payload 쪽에는 대응물이 없었고, 실제로 이미 한 번
  놓쳤다. kind와 버전별로 다듬은 모양을 append-only lock에 기록하므로, 통과시키는 유일한 방법은
  요구받은 bump다 — 그냥 다시 기록하는 것은 거부된다. 복합 kind는 parts를 펼치므로
  `video.metadata` 변경이 `video.bundle`도 올바르게 움직인다. 초록은 기록되지 않은 모양 변경이
  없다는 뜻이지, bump가 필요 없었다는 뜻이 아니다.
- **`GET /v1/artifacts/{digest}`.** 목록 라우트는 늘 digest를 내줬는데 그걸 실제 데이터로 바꿀
  방법이 없었다. 옛 payload에 닿으려면 그것을 만든 잡 id를 계속 갖고 있어야 했고, retention은
  artifact를 지우면서 잡 행은 건드리지 않으므로 둘은 서로 다른 속도로 늙는다. payload는 **그대로**
  나온다 — 이 경로에 모델이 없어서 옛 normalizer가 쓴 관측도 읽힌다. `payload_fields`와
  `current_fields`의 차이가 "이 옛 관측에 무엇이 없는가"에 대한 정직한 답이다: 수집된 적 없는
  필드는 아예 없고, 그것이 null보다 강한 진술이다.
- **`410 retracted`.** 소스는 payload가 낡은 것이 아니라 틀린 버전을 선언할 수 있다 —
  `channel.about` v1이 홈 탭을 about 패널로 읽었다 — 그리고 그런 관측은 세탁 대신 거부된다.
  404가 아니라 410인 이유는 관측이 실제로 일어났기 때문이다.
- **`tubedepth backfill-schema-versions`.** 버전이 기록되기 전에 수집된 payload를, kind가 거쳐온
  버전들에 대해 fingerprint를 다시 계산해 귀속시킨다. 아무것도 맞지 않는 행은 추측하지 않고
  비워둔 채 kind별로 보고한다.
- **`tubedepth capture-fixture --innertube <surface>`.** InnerTube fixture는 기록 경로가 아예
  없어서, 저장소에 있는 넷은 손으로 만들어졌고 세션 신원과 서명된 `googlevideo` URL을 지우는
  redaction은 만든 사람이 기억했을 때만 돌았다. 기록은 소스가 쓰는 것과 같은 헬퍼를 거치므로,
  fixture가 프로덕션이 실제로 받는 것이 된다. `browse-channel-about`은 의도적으로 제외했다 —
  런타임 continuation 뒤에 있고, 이미 한 번 깨진 적 있는 표면에 대해 반쯤 맞는 fixture는 없느니만 못하다.
- **`TUBEDEPTH_COOKIES_FILE`이 이제 troubleshooting 문서가 말하던 일을 한다.** Netscape 형식
  쿠키 jar를 가리키면 워커가 모든 추출에 실어 보낸다. 경로가 잘못되면 조용히 버리지 않고 시작할 때
  거부한다 — 오타를 무시하는 것은 아무것도 안 읽던 예전 동작과 정확히 같기 때문이다.

### Changed

- **부팅 경로가 더 이상 DDL을 내지 않는다 (#14).** 모든 CLI 진입점이 거치는
  `_database()`는 `create_schema()`를 호출했는데, 이것은 이 프로젝트가 SQLite 파일을
  독점하던 시절의 편의 기능이었다. 다른 서비스와 공유하는 데이터베이스에서는
  `docs/shared-postgres.md`의 규정 6번이 이를 금지하고, 조용히 migration을 깨기도
  했다 — 컬럼을 추가하는 부팅이 `alembic_version`은 건드리지 않고 지나가서, 다음
  `alembic upgrade`가 이미 있는 컬럼을 다시 만들려 했다. `Database.create_schema()`는
  여전히 존재한다 — 테스트와 새 `--data-dir`가 데이터베이스를 얻는 방법이다 — 하지만
  이제는 없는 것만 만든다. 예전에 하던 컬럼·인덱스 보수는 사라졌는데, 같은 공백을
  `tubedepth migrate`가 이제 메우면서 `alembic_version`도 정확하게 유지하기 때문이다.
  유일한 스키마 경로는 `tubedepth migrate`다.

### Removed

- **`deploy/tubedepth-sample.{service,timer}` (#20).** `deploy/tubedepth-watch.{service,timer}`가
  대신한다. bare-id 목록에 대고 `tubedepth enqueue video.metadata --from-file … --refresh`를
  돌리던 것이, 타입 붙은 목록에 대고 `tubedepth watch`를 돌리는 것으로 바뀌었다. `watch`가 생긴
  뒤로 샘플러 짝에는 권장할 만한 호출자가 없고, `decisions/003`이 말하는 것이 정확히 그 상황이다.
  새 짝도 예전과 같은 이유로 여전히 타이머 + one-shot이다. `enqueue --from-file`과 그것이 읽는
  bare-id 형식은 그대로 남는다 — `watch`가 읽는 파일은 다른 파일이다.
- **SQLite 지원 (#15).** 컷오버가 완료됐다: `Database`는 PostgreSQL이 아닌 URL을 전부
  거부한다. 단 하나 의도된 예외는 `tubedepth transfer --from`인데, 실제 컷오버가 데이터를
  옮겨오는 곳이 SQLite이기 때문이다. `TUBEDEPTH_DATABASE_URL`에는 더 이상 대체 경로가
  없고 필수다 — 아무것도 설정하지 않은 체크아웃은 이제 묻지도 않은 `var/tubedepth.db`
  대신 이름이 붙은 거부를 받는다. `psycopg[binary]`는 선택 extra에서 `dependencies`로
  옮겼다. 배포 유닛들은 그 URL을 위한 필수 `EnvironmentFile`을 갖는다. `tool/doctor.sh`의
  SQLite 버전 확인은 PostgreSQL 접속 확인이 됐다. `docs/troubleshooting.md`의 SQLite
  항목들은 지우지 않고 역사로 표시해 남겼다.

### Fixed

- **`prune`은 행이 하나도 없는 index로는 payload store를 sweep하지 않고 거부한다.** orphan
  sweep은 부재로 판단한다 — artifact 행이 가리키지 않는 payload는 쓰레기다 — 그리고 그 추론은
  index가 비었을 때 조용히 뒤집힌다. 모든 파일이 orphan이 되고, 로그는 정상적인 sweep처럼
  읽히는 동안 store 전체가 지워진다. 이것은 데이터베이스 컷오버가 절반만 끝난 모습이기도 하다.
  `TUBEDEPTH_DATABASE_URL`은 새 인스턴스로 옮겼고 `TUBEDEPTH_DATA_DIR`에는 옛 index가 알던
  payload가 그대로 남아 있는 상태다. 거부는 운영자에게 명령 하나를 물리지만, 잘못 추측하면
  지금까지 수집한 모든 관측을 잃고 3주 전 조회수는 어떤 재수집으로도 돌아오지 않는다.
  index가 정말로 없는 store는 `--sweep-without-an-index`를 쓴다.
- **`GET /v1/artifacts/{digest}`가 찾지 못한 바이트를 retention 탓으로 단정하지 않는다.**
  30일 정책 아래 이틀 된 관측에도 "retention을 지나 사라졌다"고 단언하고 있었다. 이제 두 가지
  설명을 모두 제시하고 — retention이거나, index가 payload store와 분리됐거나 — 관측 시각을
  함께 말한다.
- **API가 `docs/api.ko.md`가 이미 말하고 있던 대로 답한다 (#21).** 라우트가 실행되기 전에
  FastAPI가 거부하는 요청도 `detail`이 아니라 문서화된 `error` 모양으로 나오고,
  `UnavailableError`는 404 `unavailable`, `ConfigurationError`는 503 `not_configured`가 됐으며,
  `limit`은 OpenAPI 스키마에 1–500으로 선언되어 범위 밖이면 잘리지 않고 거부된다.


- **캐시 키가 더 이상 자기 입력의 절반을 무시하지 않는다.** 소스의 파라미터 — 리스팅의 `limit`,
  댓글 수집의 `sort`, 자막의 언어 우선순위, 번들의 parts — 가 생성 시점에 고정된 채 fingerprint에
  빠져 있었다. 그래서 그 상한이 설정 가능해지는 순간, 100개로 자른 리스팅이 1,000개 요청에
  답했을 것이다. 그 결과 6개 kind의 지문이 한 번 움직여 캐시가 식고, 나머지 5개는 바이트 단위로
  동일해 영향이 없다. `collect`와 `cached`는 이제 한 곳에서 키를 만든다 — 한쪽만 고치는 것은
  둘 다 안 고치는 것보다 나쁘기 때문이다.
- **비싼 kind는 싼 것보다 적은 횟수로 큐에 들어간다.** `Job.max_attempts`는 스스로를 "잡을 큐에
  넣을 때 설정된다"고 적어놓고 아무도 설정하지 않아서, 모든 kind가 컬럼 기본값 3을 받았다 —
  한 타깃에 대해 실패한 댓글 수집 세 번은 모두가 경쟁하는 per-address 예산에서 약 100건의
  요청을 쓴다. 이제 expensive는 2회, cheap과 standard는 쓰던 3회 그대로다.
- **`tubedepth work --once`가 콜백을 보내고 만료된 lease를 회수한다.** 원시 연산인
  `run_once`를 부르고 있었는데 그쪽은 둘 다 하지 않는다. 그래서 따라잡아 줄 다음 실행이 없는
  유일한 호출이 하필 그 정리를 건너뛰었고, 거기서 끝난 잡은 영영 announce되지 않았다. 이제
  `--once`는 `drain(limit=1)`이다 — 어긋날 수 있는 두 경로 대신 경계가 있는 한 경로.
- **retention이 더 이상 현재 관측이 쓰는 payload를 지우지 않는다.** 저장소는 내용 주소
  방식이라, 동일한 바이트를 수집한 두 관측은 하나의 파일이다 — `GET /v1/artifacts`가
  "digest가 같다는 것은 아무것도 안 변했다는 뜻"이라고 읽도록 가르치는 바로 그 상황이다.
  prune이 만료된 행마다 확인 없이 unlink해서, 동일한 두 관측 중 오래된 쪽이 새 쪽의 payload를
  같이 가져갔다. 살아남은 행은 조용한 캐시 미스가 되고 그 잡은 500으로 답했다. 아직 만료된
  저장소가 없어서 발화한 적은 없다.
- **결과가 만료된 잡은 500이 아니라 404로 답한다.** retention은 artifact를 지우고 잡 행은
  건드리지 않으므로, 이것은 오래된 잡의 정상적인 최종 상태다. 처리되지 않은
  `FileNotFoundError`가 FastAPI 기본 핸들러까지 갔다.
- **`refresh`가 이제 워커까지 닿는다.** `POST /v1/jobs`의 `"refresh": true`는 API 자신의
  캐시 확인만 건너뛰고 버려져서, 그렇게 만들어진 잡은 결국 캐시로 처리됐다 — 성공으로 끝나고,
  몇 시간 전에 수집된 payload를 가리키고, 새 관측은 기록하지 않았다. kind의 신선도 기간보다
  빠르게 폴링하는 쪽은 성공을 보고받으면서 아무것도 수집하지 못하고 있었다. 이제 플래그가 잡의
  컬럼이라 큐와 재시도를 넘어 살아남는다. 이미 있는 데이터베이스는 시작 시 복구 경로가 컬럼을
  붙여주므로 돌고 있는 배포가 깨지지는 않는다. 다만 Alembic 버전 테이블은 뒤처지고, 다음
  `tubedepth migrate`가 `duplicate column name`으로 실패한다 — `--stamp`가 답인지 upgrade가
  답인지 구분하는 방법은 `docs/troubleshooting.md`에 있다.

## [0.1.0] - 2026-08-19

계획서가 M0–M9로 부른 것 전부와, 계획에 없던 운영 대시보드. 지속 부하에서는 아직
검증되지 않았다 — [README](README.ko.md)의 honest limits 참조.

### Added

- **수집 11종.** `video.metadata`, `video.transcript`, `video.comments`,
  `video.sponsor_segments`, `video.related`, `video.bundle`, `channel.about`,
  `channel.community`, `channel.videos`, `playlist.items`, `search.videos`.
  목록형은 `--then`으로 항목별 수집까지 팬아웃한다.
- **작업 큐.** 지속되는 claim, 백오프 재시도, 만료 리스 회수, 실행 중 리스 갱신, 취소,
  그리고 댓글 수집이 1초 미만 작업을 굶기지 못하게 하는 cost lane.
- **Egress 제어.** (egress, lane)별 AIMD 레이트 컨트롤러, 격리, 그리고 기본값이
  `NEUTRAL`인 판정 분류 — 모르는 실패가 멀쩡한 주소를 태우지 않고, 파서 불일치는 egress
  건강을 아예 건드리지 않는다.
- **저장.** 내용 주소 방식의 gzip payload, artifact 인덱스, 나이 기준 보존, 고아 payload 청소.
- **HTTP API** (`X-API-Key` 뒤): jobs, artifacts, results, sources, health.
  필터·기간·커서를 갖춘 목록 조회.
- **운영 대시보드** `/` — 자기완결적이고 같은 `/v1` 라우트를 읽는다. 인터넷이 닿지 않는
  네트워크에서도 뜬다.
- **소스별 건강 상태.** 워커가 쓰고 API가 읽는다. `broken`(우리 파서), `blocked`(주소),
  `stale`(아무도 돌리지 않음)을 구분한다.
- **서명 웹훅.** 잡이 끝나면 호출한다. 타임스탬프와 본문에 대한 HMAC-SHA256이라
  기록해 둔 전송을 나중에 재생할 수 없다.
- **배포**: root가 필요 없는 API·워커 systemd 유저 유닛, 그리고 Alembic 마이그레이션.
- 영상·채널·재생목록·검색어가 들어올 수 있는 모든 URL 형태의 식별자 정규화.

- **문서.** [`docs/api.ko.md`](docs/api.ko.md)의 REST 레퍼런스 — 전체 엔드포인트,
  잡 수명주기, 커서 페이지네이션, 오류 코드, 서명 웹훅 계약. `README.md`·`docs/api.md`·
  이 변경 기록은 영어가 정본이고 옆에 한국어 번역이 있으며, 기여자용 문서는 한국어로만 둔다.
  기계가 확인할 수 있는 주장(라우트·kind·오류 코드·버전)은 모든 사본에 대해 검사된다.
- **버전은 한 곳에.** `pyproject.toml`이 `src/tubedepth/__init__.py`에서 읽고, 패키지와
  이 변경 기록이 어긋난 상태를 테스트가 거부한다. 절차는
  [`docs/releasing.md`](docs/releasing.md).

### Removed

- **`video.dislikes`** — 의도적 제거. YouTube가 더 이상 공개하지 않는 원본에 대고 아무도
  판정할 수 없는 재구성 추정치를 주는 소스였다. 이 제거로 프록시 풀의 유일한 정량적 근거도
  함께 사라졌다 — [`docs/status.md`](docs/status.md)에 기록.
- **`channel.profile`** — 만들기 전에 취소. 그 내용이 `channel.about`이 이미 하는 호출의
  응답에 들어 있었다.

### Fixed

- `channel.about`이 about 패널이 아니라 홈 탭을 읽던 것.
- API 읽기가 더 이상 쓰기 잠금을 잡지 않는다. 행을 세는 라우트가 p99 1,434ms였고, DB를
  건드리지 않는 라우트는 같은 부하에서 335ms였다.
- 워커가 실행 중 리스를 갱신한다. 긴 댓글 수집이 죽은 것으로 회수되어 재시도되던 문제.
- 계획서에 있었으나 아무도 넣지 않았던 조회 인덱스.

[Unreleased]: https://github.com/slopindustries/yt-scrapper/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/slopindustries/yt-scrapper/compare/v1.0.3...v1.1.0
[1.0.3]: https://github.com/slopindustries/yt-scrapper/compare/v1.0.2...v1.0.3
[1.0.2]: https://github.com/slopindustries/yt-scrapper/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/slopindustries/yt-scrapper/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/slopindustries/yt-scrapper/compare/v0.1.0...v1.0.0
[0.1.0]: https://github.com/slopindustries/yt-scrapper/releases/tag/v0.1.0
