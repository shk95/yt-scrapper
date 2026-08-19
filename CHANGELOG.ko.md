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

### Fixed

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
