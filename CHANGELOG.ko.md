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

- REST API 레퍼런스 [`docs/api.ko.md`](docs/api.ko.md) — 전체 엔드포인트, 잡 수명주기,
  커서 페이지네이션, 오류 코드 표, 서명 웹훅 계약.
- 바깥용 문서의 한국어 번역 — `README.ko.md`, `docs/api.ko.md`, `CHANGELOG.ko.md`.
  영어 파일이 정본이다.
- 이 변경 기록과 [`docs/releasing.md`](docs/releasing.md).

### Changed

- 패키지 버전을 한 곳에서만 정의한다. `pyproject.toml`이 `dynamic`으로 선언하고
  `src/tubedepth/__init__.py`를 읽으므로, 릴리스는 절반만 성공할 수 없는 편집 한 번이 된다.
- `README.md`가 영어가 되었고, 한국어 본문은 `README.ko.md`로 옮겼다.
- `tests/test_documentation_is_true.py`의 문서 검사가 번역본에도 적용된다. 수집 목록 표를
  제목이 아니라 HTML 마커로 찾고, 서빙되는 모든 라우트와 모든 오류 코드가 API 레퍼런스에
  있는지 확인하며, 패키지 버전과 두 CHANGELOG가 일치하는지 확인한다.

### Fixed

- 라우트 검사가 영숫자 8자 이상을 전부 경로 파라미터로 치환하던 것을 고쳤다. 실제로 서빙되는
  `/v1/artifacts`를 존재하지 않는 라우트로 읽고 있었다. 이제 진짜 라우트 템플릿과 대조한다.

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
