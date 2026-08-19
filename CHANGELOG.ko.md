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

- **샘플러 — 이력이 쌓이기 시작한다.** `deploy/`의 `tubedepth-sample.timer`가 매시간 watch
  list를 강제로 다시 수집한다. 목록은 `~/.config/tubedepth/watchlist.txt`이고 한 줄에 영상 id
  하나이며, 형식은 `deploy/watchlist.example.txt`에 있다. 켜지 않으면 동작하지 않는다.
  velocity는 두 관측의 차이이고 관측은 실시간으로만 쌓이므로, 필요해지기 전에 시작할 값어치가 있다.
- **`tubedepth enqueue --refresh`와 `--from-file`.** 앞은 `POST /v1/jobs`가 가진 강제 수집을
  커맨드라인에도 두는 것이고, 뒤는 한 줄에 하나씩 타깃을 읽어서 스케줄이 `ExecStart`에 id 서른
  개를 싣는 대신 목록 파일을 가리키게 한다. 읽을 수 없는 목록은 빈 목록으로 취급하지 않고 거부한다.

### Added

- **`TUBEDEPTH_COOKIES_FILE`이 이제 troubleshooting 문서가 말하던 일을 한다.** Netscape 형식
  쿠키 jar를 가리키면 워커가 모든 추출에 실어 보낸다. 경로가 잘못되면 조용히 버리지 않고 시작할 때
  거부한다 — 오타를 무시하는 것은 아무것도 안 읽던 예전 동작과 정확히 같기 때문이다.

### Fixed

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
  붙여주므로 돌고 있는 배포가 깨지지는 않는다. 그래도 `tubedepth migrate`는 돌려야 한다 —
  안 그러면 Alembic 버전 테이블이 뒤처져서, 다음 마이그레이션이 이미 있는 컬럼을 추가하려 든다.

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

[Unreleased]: https://github.com/slopindustries/yt-scrapper/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/slopindustries/yt-scrapper/releases/tag/v0.1.0
