# The plan this was built from

Written 2026-08-18, before any code existed, and approved as written. Kept here
because it is the only record of two things the repository cannot otherwise
show: the measurements taken *before* deciding anything, and what was expected
to be hard.

**It is history, not instruction.** Where it disagrees with
[`status.md`](status.md), status.md is right and the disagreement is usually
the interesting part — several of its confident predictions turned out wrong in
ways that are recorded there:

- It planned for the channel About tab's `params` going stale. There is no
  About tab any more; the surface moved into an engagement panel.
- It made Return YouTube Dislike the quantified case for a proxy pool. That
  source was removed, and with it the case.
- It recorded a channel's total view count as unavailable. It is exact, and
  sitting in the about panel.
- It expected the queue, not YouTube, to be the interesting engineering. The
  binding constraint turned out to be a verdict mapping in our own rate
  controller, which cost 8× throughput before it was measured.

What it got right is worth as much: the lane-versus-backend split, the refusal
to let an empty result look like a working parser, and the insistence that
sources be one module plus two lines — which the removal of dislikes tested
backwards and passed.

---

# yt-scrapper — YouTube 심층 데이터 스크래핑 API

## Context

YouTube Data API v3는 공개 영상에 대해서도 많은 것을 감춘다. `snippet.tags`는 영상 소유자에게만 반환되고,
자막 본문은 소유자 OAuth 없이 내려받을 수 없으며, 챕터·"가장 많이 본 구간" 히트맵·싫어요·관련 영상·커뮤니티
게시물은 아예 필드가 없다. 댓글은 있지만 쿼터 소모가 커서 대량 수집에 못 쓴다.

이 공백을 메우는 **자체 호스팅 스크래핑 API**를 만든다. 여러 클라이언트가 API 키로 접근해 "이 영상/채널의
상세 데이터를 수집해달라"는 **작업(job)을 등록**하고, 정규화된 JSON을 받아간다.

디렉터리는 비어 있다. 그린필드이며 `project-scaffold` 규약 위에서 처음부터 세운다.

**패키지 이름은 `tubedepth`** (저장소 디렉터리 `yt-scrapper`는 유지). `note-store` → `knowstore` 선례를 따라
저장소명은 서술적, 패키지명은 온전한 두 단어의 합성. `yt-`는 축약 금지 규칙에 걸린다.

---

## 확정된 결정

| | 결정 |
| --- | --- |
| 핵심 데이터 | 영상 상세 메타 / 자막·트랜스크립트 / 댓글 전량 |
| 확장 데이터 | 싫어요 + SponsorBlock / 채널 상세 / 탐색(검색·관련영상·재생목록) |
| 스택 | Python + FastAPI |
| 응답 모델 | **비동기 작업 큐** (POST로 잡 생성 → 폴링/롱폴/웹훅) |
| InnerTube 의존 기능 | **v1 포함**, fixture 회귀 테스트로 방어, 실패 시 degraded 격리 |
| 트렌딩 | **제외** (YouTube가 피드 자체를 폐지) |
| 인증 | **`X-API-Key` 헤더** + 키별 rate limit, 사설 서버 배포 |
| 확장성 | 신규 소스 추가가 "모듈 1개 + enum 1줄 + import 1줄"이어야 함 |
| **분산 범위** | **단일 머신 + 다중 egress.** 워커는 이 호스트에서 N개, 나가는 IP만 여러 개. SQLite 큐 유지 |
| **egress 정책** | **백엔드별 라우팅** — YouTube는 직결 우선, 서드파티는 풀 전체로 팬아웃. 승격은 측정된 성공률로만 |
| **프록시 런타임** | `EgressProvider` 추상화 아래 **wireproxy(1순위) · Gluetun · 직결 · 외부 프록시** |
| **목표 처리량** | 시간당 수천 건 — 종류별 달성 가능성은 아래 "처리량 현실"에 정직하게 적는다 |

v1 제외(확장 지점만 유지): 스트림 포맷/코덱, 라이브 채팅 리플레이, 트렌딩.

> 큐 선택에 대한 정정: "시간당 수천 건"은 SQLite에 전혀 부담이 아니다. 5,000건/시간 ≈ 1.4 job/s ≈ 초당
> 10~15 write이고, WAL 모드 SQLite는 그보다 두 자릿수 위다. 병목은 큐가 아니라 YouTube의 IP당 관용이다.

---

## 이 계획의 근거가 된 실측 (전부 이 머신에서 직접 실행)

환경: Python 3.14.4 · **uv 0.12.1** · **yt-dlp 2026.07.04 CLI (단 `import yt_dlp`는 실패 → 프로젝트
의존성으로 다시 넣어야 함)** · **SQLite 3.46.1** (`UPDATE … RETURNING`은 3.35+ 필요 → 사용 가능) ·
Docker 없음 · 아직 git 저장소 아님. yt-dlp `--help`에 `--impersonate`, `--cookies`, `--proxy`,
`--sleep-requests`, `--retries` 모두 존재.

**되는 것**

- `yt-dlp --dump-json` → 상위 키 **78개**. `heatmap`(100버킷 = 가장 많이 본 구간), `chapters`, `tags`,
  `timestamp`(정확한 업로드 유닉스 시각), `categories`, `like_count`, `channel_follower_count`,
  `availability`, `live_status`, `subtitles`, `automatic_captions` 포함.
- 자막: 수동 5개 언어 + **자동 생성 160개 언어**, 각 `vtt/json3/srv1..3/ttml/srt`. `json3` URL 직접 GET → 200,
  `{"tStartMs":320,"dDurationMs":14260,"segs":[{"utf8":"[Music]"}]}` 형태.
- 댓글: `--write-comments --extractor-args "youtube:comment_sort=top;max_comments=20,all,all,20"` → **20건 6.7초**.
  `parent`("root" 또는 부모 id = 대댓글 트리), `like_count`, `is_pinned`, `is_favorited`(채널 하트),
  `author_is_uploader`, `author_is_verified`, `timestamp` 포함.
- Return YouTube Dislike: **브라우저 User-Agent 필수** — 기본 urllib UA는 403, 브라우저 UA는 200.
- SponsorBlock: `?videoID=..&categories=[...]` → 200 `[{"category":"sponsor","segment":[87.973,100.734],
  "votes":16,"locked":1,…}]`. **구간이 없으면 404** — 정상 응답이지 오류가 아니다.
- 검색: `ytsearch5:query` + `--flat-playlist` 정상.
- InnerTube `POST /youtubei/v1/next` → 관련 영상 **`lockupViewModel` 20건**. API 키 없이도 200.
- InnerTube `POST /youtubei/v1/browse` (community params) → 커뮤니티 글 **`backstagePostRenderer` 4건**.

**egress·병렬 관련 실측 (새 범위의 근거)**

- 현재 직결 egress = **`119.194.145.146` · AS4766 Korea Telecom · 성남 · KR** — 즉 **가정용 ISP 회선**이다.
  YouTube 상대로 가장 좋은 종류의 egress를 이미 갖고 있고, 위의 yt-dlp·InnerTube·RYD·SponsorBlock 성공은
  전부 이 IP에서 나온 결과다.
- `video.metadata` 추출 시간: **직렬 2건 4.52초**(건당 ~2.26초) → **병렬 4건 3.11초**(건당 실효 0.78초),
  4건 모두 성공. CPU 사용률 ~50%로 IO 바운드이므로 병렬화가 그대로 이득이 된다.
  환산하면 **≈1.29건/초 ≈ 시간당 4,600건**. **단 표본이 4건이고, 지속 부하에서 봇 검사가 언제 뜨는지는
  아무것도 말해주지 않는다** — 그래서 아래 설계는 이 한계를 추측하지 않고 런타임에 측정한다.
- **`wireproxy 1.1.3`이 nixpkgs에 있다** → `nix profile install nixpkgs#wireproxy`, sudo 불필요.
  유저스페이스 WireGuard라 root·netns·TUN·Docker가 전부 필요 없고, config 1개 = 로컬 프록시 1개다.
- yt-dlp `--proxy`는 `socks5://`와 `http://`를 모두 받는다(도움말에 명시). httpx도 `proxy=`로 동일.
- Go 1.26.5 + 쓰기 가능한 GOPATH도 있어 `go install`이 대안 경로로 남아 있다.

**egress 관련 함정**

7. **Gluetun은 이 머신에서 그대로 못 돈다.** Docker·podman·nerdctl 전부 없음. Windows쪽 Docker Desktop
   디렉터리는 있으나 WSL 통합이 꺼져 있고 interop 바이너리도 PATH에 없다. `wg`·`openvpn`·`iptables`·`nft`도
   없고 **passwordless sudo도 없다**. 커널 WireGuard 모듈(`wireguard.ko`, `CONFIG_WIREGUARD=m`,
   `CONFIG_NET_NS=y`, `/dev/net/tun`)은 존재하지만 쓰려면 설치와 sudo가 필요하다.
8. **VPN IP는 YouTube에 대해 역효과일 가능성이 높다.** yt-dlp 커뮤니티의 표준 권고가 문자 그대로
   *"avoid VPNs and datacenter proxies — use a residential proxy or disable your VPN"*이고, 데이터센터
   IP(ProtonVPN exit이 이에 해당)가 봇 검사의 1순위다. 즉 **프록시를 붙이면 YouTube 처리량이 오히려 떨어질
   수 있다.** 반면 RYD·SponsorBlock은 자체 IP rate limit이 병목이라 팬아웃이 실제로 이득이다.
   → 이것이 "백엔드별 라우팅"과 "측정 기반 승격"이 정책이 된 이유다.

**안 되는 것 / 함정 — 아래 설계는 전부 이것들의 결과다**

1. **관련 영상은 yt-dlp 덤프에 없다.** 78키 중 related/recommended 필드 0개 → InnerTube 필수.
2. **커뮤니티 탭은 yt-dlp가 조용히 빈 목록을 준다.** 에러가 아니라 `entries: []` + 전 필드 null.
   → *빈 결과와 "파서가 더 이상 안 맞음"을 반드시 구분해야 한다*는 요구로 이어진다.
3. **채널 About(가입일·국가·외부 링크·총 조회수)은 yt-dlp에 없다.** `view_count`는 None. InnerTube about 탭
   `params`는 낡으면 **조용히 홈 탭으로 폴백**하므로 런타임에 `tabRenderer`에서 해석해야 한다.
4. **정확한 구독자 수는 원천적으로 불가능.** InnerTube 응답 자체가 `"4.53M subscribers"` 문자열이고,
   yt-dlp의 `channel_follower_count: 4530000`은 그것을 파싱한 값일 뿐이다.
5. **트렌딩 폐지.** `/feed/trending` → *"the URL redirected to youtube.com home page"*.
6. **렌더러 이름은 바뀐다.** 예전 `compactVideoRenderer`는 이제 0건, `lockupViewModel`이 그 자리다.
   InnerTube 파싱이 이 시스템에서 가장 잘 깨지는 부분이고 fixture 회귀가 유일한 방어선이다.

---

## 데이터 종류별 백엔드 매핑

| kind | 백엔드 | 비고 |
| --- | --- | --- |
| `video.metadata` | yt-dlp | 챕터·히트맵·태그·정확한 업로드 시각·라이선스 |
| `video.transcript` | yt-dlp(트랙 목록) + httpx(json3 본문) | 자동 생성 포함 |
| `video.comments` | yt-dlp (subprocess) | `parent`로 대댓글 복원, 취소 가능해야 함 |
| `video.dislikes` | 서드파티 HTTP (RYD) | **브라우저 UA 필수** |
| `video.sponsor_segments` | 서드파티 HTTP (SponsorBlock) | **404 = 구간 없음, 성공 처리** |
| `video.related` | **InnerTube** `/next` | `lockupViewModel` |
| `channel.profile` | yt-dlp (`youtube:tab`) | 설명·태그·팔로워(반올림)·인증 |
| `channel.videos` | yt-dlp `--flat-playlist` | |
| `channel.about` | **InnerTube** `/browse` | 가입일·국가·링크. params 런타임 해석 |
| `channel.community` | **InnerTube** `/browse` | `backstagePostRenderer` |
| `search.videos` | yt-dlp `ytsearch:` | |
| `playlist.items` | yt-dlp `--flat-playlist` | |
| `video.bundle` | 위 여러 개 조합 | 부분 실패는 `degradations`로 표시 |

---

## 아키텍처

`note-store`와 같은 계층 규칙: **models → repositories → services → (CLI | API)**. 쓰기는 전부 service를
통과하고 **HTTP API는 CLI와 같은 service 위에 얇게 얹는다.**

**동기/비동기 정책 (한 번만 정한다)**: repositories와 services는 **동기** — CLI가 부르는 것과 같은 코드다
(`knowstore`와 동일). sources만 **비동기**(httpx + `anyio.to_thread`로 yt-dlp 감싸기). DB만 만지는 라우트
핸들러는 `def`로 선언해 Starlette 스레드풀에 맡긴다. `aiosqlite`도, 비동기 리포지토리 중복 구현도 없다.

### 디렉터리

```
src/tubedepth/
  errors.py           예외 taxonomy — 각 클래스가 code·status_code·retryable을 들고 다님
  models.py           SQLAlchemy: Job, Artifact, ApiKey, WebhookDelivery + JobKind/JobState/SourceCost enum
  schemas.py          공개 계약 pydantic 모델 (yt-dlp 78키를 그대로 흘리지 않음)
  database.py         엔진 팩토리 + SQLite PRAGMA + Database.session() 컨텍스트매니저
  repositories.py     JobRepository / ArtifactRepository / ApiKeyRepository
  identifiers.py      URL·ID 파싱, target_key() 정규화
  payload_store.py    콘텐츠 주소 지정 gzip-JSON 블롭 (디스크)
  configuration.py    frozen dataclass, Configuration.from_environment() — pydantic-settings 안 씀
  http_client.py      단 하나의 httpx.AsyncClient 팩토리: 브라우저 UA·타임아웃·호스트별 토큰버킷·프록시
  observability.py    stdlib logging + → ✓ ✗ · 콘솔 어휘, uvicorn 로거 재부모화
  worker.py           claim/execute/persist/retry 루프, lease 리퍼, 보존 정리 틱
  cli.py              typer: serve / worker / fetch / job / key / migrate / capture-fixture / purge
  sources/
    base.py           DataSource 프로토콜, SourceRequest/Result/Context, Degradation, CancellationToken
    registry.py       SourceRegistry, register() 중복 가드, describe()
    ytdlp_runtime.py  YoutubeDL 옵션 dict를 만드는 유일한 곳 (library / subprocess 두 구현)
    innertube.py      InnerTube httpx 클라이언트 + 관용적 렌더러 탐색기
    video_metadata.py  transcripts.py  comments.py
    returnyoutubedislike.py  sponsorblock.py  related.py
    channel_profile.py  channel_about.py  channel_community.py  playlist.py  search.py
    bundle.py         합성 kind — 다른 소스로 팬아웃, degradations 취합
    __init__.py       각 모듈 import (= 등록 지점) + default_registry()
  egress/
    base.py           EgressKind·EgressEndpoint·EgressProvider 프로토콜·EgressProbe
    pool.py           pool.toml 로딩, 기동/종료 수명주기, 포트 할당
    selection.py      백엔드별 적격 집합 + 가중 최소사용 선택 (순수 로직)
    control.py        AIMD permit_limit·min_interval·격리 (순수 로직)
    wireproxy.py      유저스페이스 WireGuard 프로세스 감독 + 파생 config 생성
    gluetun.py        컨테이너 provider — 설정만, Docker가 생기면 동작
    external.py       기존 HTTP/SOCKS 프록시(레지덴셜 업체 포함)
  services/
    jobs.py           submit(캐시 단축·진행중 중복 제거·idempotency) / get / list / cancel / complete / fail
    extraction.py     레지스트리로 한 잡 실행 → 정규화 → artifact 저장
    cache.py          fingerprint 신선도 조회, kind별 TTL 표
    retention.py      종료 잡·만료 artifact·고아 블롭 정리
    authentication.py API 키 검증, 키별 rate limit
    webhooks.py       HMAC 서명 완료 콜백 + 백오프
  api/
    application.py  dependencies.py  exception_handlers.py
    jobs.py  videos.py  channels.py  discovery.py  system.py
tests/
  conftest.py  fixtures/ytdlp/*.json  fixtures/innertube/*.json
```

**정규화는 각 소스 모듈 안에 산다** — 별도 `normalizers/` 패키지를 두지 않는다. 그래야 "소스 추가"가 파일 하나로 끝난다.

### 확장 지점 — DataSource 프로토콜

```python
# src/tubedepth/sources/base.py
#
# Every kind of data this project can produce is one implementation of the
# protocol below. The point of the shape is what it costs to add the next one:
# a new module here, one JobKind member, one import line in __init__.py, and
# nothing else changes — not the router, not the worker, not the job table.
# Live chat replay and stream formats are deliberately absent, and M8 exists to
# hold that claim to a three-line diff.

@dataclass(frozen=True, slots=True)
class Degradation:
    """A partial failure that did not fail the job.

    SponsorBlock answering 404 for a video with no segments is why this exists:
    it is a normal answer, and a bundle that lost one of six sources is still
    a useful result.
    """
    source: str
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class SourceContext:
    """Everything a source may touch. Nothing here knows about jobs or SQL.

    Outbound concurrency, cookies, proxy and cancellation are governed here
    rather than per source, so a new source cannot bypass the politeness limits
    by accident.
    """
    http: httpx.AsyncClient
    ytdlp: YtdlpRuntime
    innertube: InnerTubeClient
    configuration: Configuration
    cancellation: CancellationToken
    registry: SourceRegistry        # composite sources fan out through this, never by import


@runtime_checkable
class DataSource(Protocol):
    kind: ClassVar[JobKind]
    backend: ClassVar[Backend]              # YT_DLP | INNERTUBE | THIRD_PARTY
    parameter_model: ClassVar[type[BaseModel]]
    payload_model: ClassVar[type[BaseModel]]
    schema_version: ClassVar[str]           # bumped when normalization changes; invalidates cache
    default_freshness: ClassVar[timedelta]
    cost: ClassVar[SourceCost]              # CHEAP | MODERATE | EXPENSIVE — picks the concurrency lane

    async def fetch(self, request: SourceRequest) -> SourceResult: ...
```

`SourceResult`: `payload`, `fetched_at`, `provider`("yt-dlp/2026.07.04" · "innertube" · "sponsorblock"),
`raw`, `degradations`, `item_count`.

`registry.register()`는 같은 `kind` 중복 등록을 `ConfigurationError`로 거부한다. `sources/__init__.py`가
모든 모듈을 import해 등록을 강제하고, `GET /v1/sources`가 레지스트리를 그대로 노출한다
(kind·백엔드·파라미터 JSON Schema·기본 신선도·비용). **새 소스는 문서화가 공짜다.**

### yt-dlp 런타임 — 두 구현을 두는 이유는 취소다

```python
# src/tubedepth/sources/ytdlp_runtime.py
#
# yt-dlp is the single most likely thing here to break when YouTube changes, and
# the fix is almost always "add this one extractor argument". Sources are
# forbidden from constructing their own YoutubeDL so that when that day comes
# there is exactly one file to edit.
#
# Two implementations, and the difference is cancellation. The library runtime is
# fast and in-process, but a blocking call inside a thread cannot be interrupted:
# a cancelled comment harvest would keep burning quota for minutes after the
# client gave up. The subprocess runtime pays ~0.5s of interpreter startup and
# gets SIGTERM in exchange — free next to a five-minute harvest — and keeps a
# 50 MB comment payload out of the API process's heap.
# SourceCost.EXPENSIVE selects the subprocess one.
#
# The subprocess is `sys.executable -m yt_dlp`, never the bare `yt-dlp` on PATH.
# The PATH one on this machine is an isolated install at a version uv.lock does
# not pin, and fixtures recorded against one version replayed against another is
# exactly the drift that makes a green test suite lie.
```

라이브러리 경로에서는 `sanitize_info()`를 반드시 거친다 — `extract_info`가 남기는 비직렬화 객체와 private
키를 걸러내지 않으면 `json.dumps` 직전까지 멀쩡해 보인다.

### InnerTube 파서 방어 — v1의 최대 리스크

1. **관용적 탐색**: 고정 경로(`contents.twoColumn…[3].items[0]`) 금지. 이름으로 찾는 재귀 탐색기
   `find_renderers(payload, "lockupViewModel")`를 쓴다. 껍데기 구조 변경에 견딘다.
2. **빈 결과 ≠ 성공, 단 "진짜 비어있음"은 구분한다**: `collect(payload, accepted_names=(...),
   empty_markers=("messageRenderer",))`. 기대 렌더러가 0건이면서 *응답이 스스로 비었다고 말하는 표지*
   (커뮤니티 글이 없는 채널의 `messageRenderer` 등)도 없으면 `ExtractionError`를 던지고, 그 메시지에
   **YouTube가 실제로 보낸 렌더러 이름 목록**을 담는다 — 그게 진단의 전부다. 표지가 있으면 정상적인 빈
   결과다. yt-dlp가 커뮤니티 탭에서 조용히 `[]`를 준 것이 정확히 이 함정이었고, "글이 없는 채널"과
   "파서가 깨짐"이 구분 불가능한 상태가 바로 망가진 스크래퍼가 배포되는 경로다.
   `accepted_names`에는 옛 이름도 남긴다(`("lockupViewModel", "compactVideoRenderer")`) — YouTube가
   롤백해도 파서가 살아남는다.
   파싱에 실제로 매칭된 키를 `renderer_shape`로 정규화 페이로드에 기록해, 저장된 결과가 어떤 파스로
   생성됐는지 증거를 들고 다니게 한다.
3. **params 런타임 해석**: about 탭 params를 하드코딩하지 않고 `tabRenderer` 목록에서 찾는다. 못 찾으면
   폴백이 아니라 `ExtractionError`.
4. **fixture 회귀**: `tubedepth capture-fixture` 로 실제 응답을 `tests/fixtures/innertube/`에 저장.
   파서 테스트는 네트워크 없이 fixture만 읽으므로 CI에서 항상 돈다. 갱신은 사람이 의도적으로 한다.
5. **degraded 격리**: InnerTube 소스 하나가 죽어도 `video.bundle`은 나머지를 반환한다.

### 잡 큐 — SQLite

Celery/arq/dramatiq은 브로커(Redis·RabbitMQ)를 요구하는데 이 호스트엔 Docker가 없고, 손으로 설치한 브로커는
모든 클론의 미문서화 전제조건이 된다. APScheduler는 스케줄러지 내구성 큐가 아니다(claim 의미론·시도 원장·결과
저장소가 없다). **구조적 근거**: 큐 테이블이 곧 API의 read model이라 브로커를 쓸 때 생기는 이중 쓰기 불일치가
아예 없다. `GET /v1/jobs/{id}`는 워커가 쓴 바로 그 행을 읽는다. 처리량은 무관하다 — 댓글 수집은 분 단위다.

상태는 `queued → running → succeeded | failed | cancelled`. **`retrying` 상태는 두지 않는다** — 재시도는
`running → queued` 이면서 `scheduled_at`을 미래로 미는 것이고, claim 쿼리가 이미 `scheduled_at <= now`를 건다.

```python
class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        # The claim query's index. Column order matches WHERE/ORDER BY exactly;
        # without it every claim is a table scan and two workers contend on the
        # same pages.
        Index("ix_job_claimable", "state", "scheduled_at", "priority"),
        Index("ix_job_fingerprint", "parameter_fingerprint", "state"),
        Index("ix_job_lease", "state", "lease_expires_at"),
        UniqueConstraint("idempotency_key", name="uq_job_idempotency_key"),
    )
    id / kind / state / target_key
    parameters_json:        Mapped[str]
    # sha256 over kind + target_key + canonical parameters + the source's
    # schema_version. Including schema_version means changing a normalizer
    # invalidates its cached artifacts without anyone remembering to.
    parameter_fingerprint:  Mapped[str] = mapped_column(String(64))
    priority / scheduled_at / created_at / updated_at / started_at / finished_at
    attempt_count / max_attempts / worker_id / lease_expires_at / cancel_requested_at
    error_code / error_message / degradations_json
    artifact_id / webhook_url / api_key_id / idempotency_key
```

**중복 실행 방지** — `BEGIN IMMEDIATE` 안에서 SELECT + 조건부 UPDATE. 애플리케이션 락은 필요 없다:

```python
def claim(self, *, worker_id, lease) -> Job | None:
    """Take exactly one queued job, or return None.

    BEGIN IMMEDIATE takes the write lock on the *first* statement rather than
    on the first write. With a deferred transaction two workers can both run
    the SELECT, both see the same row, and the second UPDATE either overwrites
    the first claim or fails on lock upgrade — and a failed *upgrade* raises
    SQLITE_BUSY immediately, ignoring busy_timeout entirely. IMMEDIATE
    serializes the pair.

    The `AND state = 'queued'` guard on the UPDATE plus the rowcount check is
    belt and braces: it also covers a job cancelled between the two statements.
    """
```

`database.py`는 엔진을 **`isolation_level=None`으로 만든다** — pysqlite의 암묵적 BEGIN을 끈다. 그것을 켜두면
SQLAlchemy가 모든 트랜잭션을 deferred read로 열고 첫 UPDATE에서 쓰기 락으로 *승격*하는데, 그 승격 실패는
`busy_timeout`을 무시하고 즉시 `database is locked`를 던진다. claim은 자기 `BEGIN IMMEDIATE`를 직접 낸다.

`connect` 이벤트(=`knowstore`가 이미 `foreign_keys`에 쓰는 훅)에서:
`journal_mode=WAL`(워커가 쓰는 동안 API가 읽는다) · `synchronous=NORMAL` · `busy_timeout=5000` · `foreign_keys=ON`.

> `UPDATE … RETURNING`(SQLite 3.35+, 이 호스트 3.46.1)은 단일 문장 원자성이 필요한 곳에서 계속 쓸 수 있지만,
> claim 경로의 안전성은 `BEGIN IMMEDIATE`가 보장한다. `tool/doctor.sh`가 SQLite 버전을 확인한다 —
> 확인하지 않으면 claim 시점의 `OperationalError`로 알게 되는데 그게 가장 나쁜 발견 장소다.

> **WSL 함정** — `docs/troubleshooting.md`에 `database is locked`라는 문자 그대로의 제목으로 기록할 것:
> WAL은 실제 POSIX 잠금을 요구하는데 `/mnt/c`(drvfs)는 이를 신뢰성 있게 제공하지 않는다. DB는 리눅스 파일시스템에 둔다.

- **재시도**: 재시도 여부는 예외 클래스의 속성(`retryable`)이며 한 곳에서 결정된다.
  `delay = min(15s * 2**(attempt-1), 30min) * uniform(0.5, 1.5)`. `max_attempts` 기본 3,
  **`EXPENSIVE` 소스는 1** — 봇 탐지로 실패한 5분짜리 수집을 재시도하면 상황을 악화시킬 뿐이다.
- **취소**: `DELETE`가 `cancel_requested_at`을 쓴다. `queued`면 조건부 UPDATE로 곧장 `cancelled`;
  0행이면 그 사이 워커가 집은 것이므로 running 경로로 흘러간다(경합 없음). running이면
  `CancellationToken`을 소스가 단계 사이에 확인하고, `EXPENSIVE`는 subprocess에 SIGTERM.
  **v1 한계로 명시**: yt-dlp는 추출 끝에 댓글을 쓰므로 취소된 수집은 아무것도 남기지 않는다. 실질적 제어는
  `max_comments`로 미리 상한을 두는 것이다.
- **lease 만료 회수**: 워커가 죽으면 `running` 행이 영원히 남는다. 리퍼가 매 틱마다
  `state='running' AND lease_expires_at < now`를 `queued`로 되돌리고, `max_attempts` 초과분은
  `error_code='lease_expired'`로 실패시킨다. 긴 수집 중에는 워커가 주기적으로 lease를 갱신한다.
- **프로세스 배치**: 기본은 **별도 프로세스** (`uv run tubedepth serve` / `uv run tubedepth worker`).
  yt-dlp 추출은 블로킹·메모리 집약적이라 API 옆에서 돌리면 댓글 수집이 `GET /v1/jobs/{id}`의 p99를 결정하고,
  yt-dlp 크래시가 API를 같이 죽인다. 단 **단일 명령 개발 경로는 반드시 있어야 한다** — 두 터미널짜리 셋업은
  사람들이 안 쓰게 된다: `serve --with-worker`가 같은 `worker.run()` 코루틴을 lifespan에서 띄운다.
- **확장 경로**: claim이 상태 기반 조건부 UPDATE이므로 한 호스트에서 워커 N개는 **오늘 이미 올바르다**.
  나중에 Postgres로 가면 `claim()`이 `FOR UPDATE SKIP LOCKED`로 바뀌고 그게 전부다. 상태 전이에 관한 SQL이
  오직 그 메서드에만 있도록 유지하는 것이 조건이다.

### API 표면

베이스 `/v1`. 오류는 RFC 9457 problem details. **모든 요청에 `X-API-Key` 필요** (`/healthz` 제외).

| Method | Path | 동작 |
| --- | --- | --- |
| `POST` | `/v1/jobs` | `{kind, target, parameters, max_age_seconds, priority, webhook_url, idempotency_key, retain_raw}` → **202** + `JobEnvelope` + `Location` + `Retry-After`, 또는 신선한 캐시가 있으면 **200** + `ResultEnvelope` |
| `GET` | `/v1/jobs/{id}` | `JobEnvelope`. `?wait=30`이면 상태 변화까지 롱폴 |
| `GET` | `/v1/jobs/{id}/result` | `ResultEnvelope` (디스크에서 스트리밍). 미완료면 **409** + `Retry-After` |
| `GET` | `/v1/jobs/{id}/raw` | 원본 provider 페이로드. `retain_raw` 안 걸었으면 **404** |
| `DELETE` | `/v1/jobs/{id}` | 취소 요청 |
| `GET` | `/v1/jobs?state=&kind=&target=&cursor=` | 커서 페이지네이션 |
| `GET` | `/v1/sources` | 레지스트리 introspection — 새 소스가 코드 변경 없이 여기 나타난다 |
| `GET` | `/healthz` `/readyz` `/version` | `/version`은 앱 버전 **+ 실행 중인 yt-dlp 버전** (고장 났을 때 첫 번째로 필요한 숫자) |

편의 별칭(같은 잡을 만드는 얇은 래퍼): `/v1/videos/{id}/{metadata,transcript,comments,dislikes,
sponsor-segments,related,bundle}` · `/v1/channels/{id}/{profile,videos,about,community}` ·
`/v1/playlists/{id}/items` · `/v1/search`. 각 별칭은 두 메서드를 갖는다:

- **`GET` = 캐시 전용.** 저장된 결과가 있으면 200, 없으면 404. **절대 네트워크를 만지지 않는다.**
- **`POST` = 보장(ensure).** 신선한 캐시가 있으면 200, 없으면 잡 생성 후 202.

이 분리가 있으면 "있으면 주고 없으면 말고"를 원하는 클라이언트가 실수로 수집을 유발할 수 없다.

**"신선하면 즉시 반환"을 만드는 단 하나의 seam** — 모든 라우트와 CLI가 `JobService.submit()`을 통과하고,
그것이 `Submission{job, result, reused_in_flight, poll_after_seconds}`를 돌려준다. 검사 순서:

1. `idempotency_key`가 이미 있으면 그 잡(또는 결과)을 반환
2. 같은 `parameter_fingerprint`의 artifact가 `max_age_seconds` 안이면 **결과 반환, 잡 생성 안 함**
3. 같은 fingerprint의 `queued`/`running` 잡이 있으면 그것을 반환 (`reused_in_flight=True`)
   — 열 개 클라이언트가 똑같은 4분짜리 수집을 열 번 시작하는 것을 막는다
4. 아니면 잡 삽입

`max_age_seconds`: `null` = 소스의 `default_freshness`, `0` = 무조건 재수집, 정수 = 그 창.

### 공개 응답 계약 — yt-dlp 78키는 흘리지 않는다

```python
class ResultEnvelope(BaseModel):
    kind, target, schema_version
    fetched_at: datetime      # always UTC, offset-aware
    fresh_until: datetime
    provider: str             # "yt-dlp/2026.07.04"
    degradations: list[Degradation]
    raw_available: bool
    data: VideoMetadata | TranscriptSet | CommentHarvest | ...   # discriminated on kind
```

의미 있는 정규화 결정:

| 원본 | 정규화 | 이유 |
| --- | --- | --- |
| `chapters: null` | `chapters: []` | 없음은 영상에 대한 사실이지 계약의 null이 아니다. nullable list 금지 |
| `heatmap` 100버킷 | `heatmap[]` **+** `most_replayed[{rank,…}]` | 순위가 실제로 원하는 것. 여기서 한 번 유도하면 모든 클라이언트가 다시 안 해도 된다 |
| `timestamp` + `upload_date` | `published_at: datetime\|None` + `published_date: date\|None` | `timestamp`가 공식 API가 못 주는 정확한 순간. 없을 때가 있어 둘 다 보존 |
| `is_favorited` | `is_hearted` | YouTube UI가 실제로 보여주는 것의 이름 |
| `parent: "root"` | `parent_id: None` | 센티넬 문자열은 공개 계약에 들어가면 안 된다 |
| `formats` 37개 서명 URL | **전부 제거** | 범위 밖이고, 몇 시간 만에 만료되며, `gitleaks`에 자격증명처럼 보인다 |
| `automatic_captions` 160×7 | 메타데이터엔 `subtitle_tracks[{language,name,source,formats}]`만; URL은 transcript 잡 안에서만 쓰고 **절대 저장 안 함** | 자막 URL은 시한부다. 저장하면 나중의 403을 보장하는 셈 |

`CommentHarvest.comments`는 **`parent_id`로 엮인 평평한 리스트**이지 중첩 트리가 아니다. 5만 건짜리 중첩
문서는 파싱도 diff도 병리적이고, `parent_id`는 yt-dlp가 그대로 주는 것이며, 클라이언트는 다섯 줄이면 트리를 만든다.

### 캐시와 저장 — 2계층

**SQLite 행**은 잡 원장과 artifact *색인*만 든다. 50MB 댓글 수집을 `TEXT` 컬럼에 넣으면 그 테이블의 모든
`SELECT *`가 재앙이 되고 페이지 캐시가 망가지며 `VACUUM`이 장애가 된다.

**임계값 하나로 행과 파일을 가른다** — `TUBEDEPTH_INLINE_LIMIT_BYTES`, 기본 64 KiB:

- 미만 → `payload_inline` 컬럼에 JSON 문자열. 영상 메타·싫어요·스폰서 구간·채널 프로필·관련영상·검색 등
  **클라이언트가 반복해서 폴링하는 것 전부**가 여기 들어간다. SELECT 한 번, 파일 IO 없음.
- 초과 → `<payload_root>/<kind>/<sha256[:2]>/<sha256>.json.gz`, 콘텐츠 주소 지정. 댓글 수집과 긴 자막은 항상 여기.
  공짜로 따라오는 것: 동일 재수집은 새 바이트를 안 쓰고, `GET /result`는 `FileResponse` +
  `Content-Encoding: gzip`이라 API 프로세스가 JSON을 메모리에 펼치지 않으며, 무결성 검증이 가능하다.

`CheckConstraint("(payload_inline IS NULL) != (payload_path IS NULL)")`로 정확히 한 곳에만 있음을 DB가 강제한다.
2단계 디렉터리 팬아웃은 WSL2 파일시스템에서 평평한 십만 파일 디렉터리가 느리기 때문이다.

**쓰기 순서는 항상 파일 먼저, 행 나중이다.** 그래야 크래시가 남기는 유일한 불일치가 "행 없는 고아 파일"이고,
그건 정리 스윕이 치우면 그만이다. 반대 순서면 "파일 없는 행"이 남고 그건 500이다.

**artifact에 `(kind, target_key, parameter_fingerprint)` 유니크 제약을 걸지 않는다 — 이력으로 쌓는다.**
"현재"는 `max(fetched_at)`. 대가는 디스크와 정리 부담이고, 얻는 것은 조회수·좋아요·댓글수의 시계열이
캐싱의 부산물로 공짜라는 것이다. 원치 않으면 제약 추가는 마이그레이션 하나다.

TTL(기본값, 요청·환경변수로 덮어쓰기 가능): `transcript` 30일 · `channel.about` 7일 ·
`channel.profile`/`comments` 12–24시간 · `video.metadata`/`dislikes`/`related`/`channel.videos`/
`playlist.items` 6시간 · `sponsor_segments` 1시간 · `search` 15분.
신선도는 **보증이 아니라 조언**이다. 계약은 "이것은 `fetched_at`에 관측되었다"이며, `fetched_at`과
`fresh_until`이 모든 응답에 실려 클라이언트가 스스로 판단한다.

`RetentionService`가 15분마다: 종료 잡 7일 초과 삭제 · artifact 30일 초과 삭제(단
`(kind,target_key,fingerprint)`별 최신 1개는 항상 보존) · artifact 행 없는 고아 블롭 정리(mtime 기준
스윕이라 방금 쓴 파일은 안 지운다) · 느린 주기로 `PRAGMA incremental_vacuum`.

### 인증과 정중함

- `api_keys` 테이블(`id`, `label`, `key_hash`(sha256), `created_at`, `revoked_at`,
  `requests_per_minute`, `daily_job_quota`). 평문 키는 저장하지 않고 `tubedepth key create` 출력 시 한 번만 보인다.
  파일이 아니라 DB에 두는 이유: 폐기·쿼터 변경이 재시작 없이 되고, 잡에 `api_key_id`를 남겨 사용량을 귀속할 수 있다.
- `require_api_key` 의존성. 없음/불일치 401, 폐기됨 403, 초과 429(+`Retry-After`).
  **키 검사는 잡 생성 전에 끝난다** — 인증 실패가 큐에 행을 남기면 안 된다.
- **브라우저 UA는 필수지 장식이 아니다.** RYD가 기본 urllib UA에 403, 브라우저 UA에 200을 준 것이 실측이다.
  `http_client.py`가 yt-dlp 이외의 *모든* 요청에 기본 UA를 붙여서 어떤 소스도 잊을 수 없게 한다.
  `docs/troubleshooting.md`에 `403 Client Error: Forbidden for url: https://returnyoutubedislikeapi.com/votes`를
  문자 그대로의 제목으로 넣는다.
- **SponsorBlock 404 = 구간 없음.** `segments: []`로 매핑하고 5xx·타임아웃만 실패로 본다.
- **동시성과 속도 제어는 정적 세마포어가 아니라 egress별 적응 제어다** — 아래 "다중 egress와 병렬 처리" 참조.
  호스트별 최소 간격과 yt-dlp `sleep_requests`는 그 아래 최종 게이트로 남는다.
- **차단 대응, 단계 순서**:
  1. **yt-dlp를 최신으로 유지** — 이게 실제 해결책이고, 집안 규칙에서 의도적으로 벗어나는 지점이다:
     **`yt-dlp>=2026.7`, 상한 없음** (다른 의존성은 `pydantic>=2.7,<3`처럼 상한을 건다). yt-dlp에 상한을 걸면
     "`uv lock --upgrade-package yt-dlp` 실행"이 "먼저 pyproject.toml을 고쳐라"로 바뀌므로 적극적으로 해롭다.
     이건 `AGENTS.md`의 "깨면 비싼 규칙"에 들어간다.
  2. `TUBEDEPTH_COOKIES_FILE` (Netscape 형식). 서버에서 `--cookies-from-browser`는 쓰지 않는다.
     자동 추출에 쓰는 로그인 계정은 잃을 수 있는 계정이라는 경고를 함께 적는다.
  3. `TUBEDEPTH_IMPERSONATE` — 이 빌드에 `--impersonate`가 있음을 확인했다.
  4. PO token — 구현하지 않고 훅만 문서화. `TUBEDEPTH_YTDLP_EXTRACTOR_ARGS`가 임의 extractor arg를
     통과시키므로 코드 변경 없이 provider를 붙일 수 있다.
- **서킷 브레이커** — `source_health` 테이블이 백엔드별 연속 거부 횟수를 센다. yt-dlp가
  *"Sign in to confirm you're not a bot"* / 429 / 동의 월을 내면 `UpstreamRejectedError`로 분류하고,
  3회 연속이면 워커가 **그 백엔드의 잡을 아예 집지 않는다**(쿨다운 5분 → 배로 늘려 최대 1시간).
  잡은 `failed`로 타버리지 않고 `queued`에 남는다. `GET /healthz`가 열린 브레이커를 보고하므로 운영자가
  *왜* 아무것도 진행되지 않는지 본다 — 그 대안은 조용히 멈춘 큐다. 백오프만으로는 이걸 못 준다:
  백오프는 시도를 소진시키지만 브레이커는 시도를 보존한다.
- **프록시**: `TUBEDEPTH_PROXY_URL` 하나가 httpx와 yt-dlp 양쪽을 먹인다. 두 전송 계층이 프록시 사용 여부를
  두고 어긋나면 그 불일치는 눈에 안 보이면서 출발지 IP를 흘린다.
- **타임아웃**: 소스별 벽시계 상한(메타 60s, 자막 120s, 댓글 1800s)을 `asyncio.timeout`으로 건다.
  library 런타임은 초과해도 스레드를 못 끊는다 — 정확히 그래서 `EXPENSIVE`는 subprocess를 쓴다.

---

## 다중 egress와 병렬 처리

목표는 두 가지다: **나가는 IP를 여러 개로 늘려 IP당 rate limit을 완화**하고, **병렬도를 올려 처리량을 낸다.**
단 위의 실측 8번 때문에 "전부 VPN으로 보낸다"는 답은 틀렸다. 대신 **경로를 백엔드별로 나누고, 어느 경로가
실제로 통하는지는 시스템이 측정해서 스스로 정한다.**

### EgressProvider 추상화

```python
# src/tubedepth/egress/base.py
#
# One egress is one exit address the project can send a request from. The whole
# point of the type is that a source never learns which one it got: it receives
# a proxy URL (or None) and uses it. That is what lets the same source run over
# the direct residential line today and over a residential proxy pool later
# without a line changing inside it.

class EgressKind(StrEnum):
    DIRECT = "direct"          # this host's own line — currently the best route for YouTube
    WIREPROXY = "wireproxy"    # userspace WireGuard, no root, no container
    GLUETUN = "gluetun"        # container-based; config-only until Docker exists here
    EXTERNAL = "external"      # any pre-existing HTTP/SOCKS proxy, incl. residential providers


@dataclass(frozen=True, slots=True)
class EgressEndpoint:
    """A usable exit. `proxy_url` is None only for the direct route.

    One string feeds BOTH transports — httpx's `proxy=` and yt-dlp's `proxy`
    option — because a mismatch there is invisible and leaks the origin address
    on whichever transport was forgotten. There is exactly one place that reads
    this field for each transport, and a test asserts both read the same value.
    """
    name: str                  # "proton-jp1"
    kind: EgressKind
    proxy_url: str | None      # "http://127.0.0.1:24101"
    label: str                 # "protonvpn/jp-free-1" — for logs and /healthz


class EgressProvider(Protocol):
    name: ClassVar[str]
    kind: ClassVar[EgressKind]

    async def start(self) -> EgressEndpoint: ...
    async def stop(self) -> None: ...
    async def probe(self) -> EgressProbe: ...   # public IP, latency, reachable?
```

**wireproxy는 SOCKS5가 아니라 HTTP CONNECT로 노출한다.** 우리 트래픽은 전부 HTTPS라 CONNECT 터널로 충분하고,
그러면 httpx가 `socksio` 추가 의존성 없이 그대로 쓰며 yt-dlp도 문제없다. 스킴이 하나로 통일된다.

### wireproxy 감독 — Docker도 root도 없이

- 설치: `nix profile install nixpkgs#wireproxy` (1.1.3 확인됨). `tool/doctor.sh`가 존재를 확인한다.
- ProtonVPN에서 받은 WireGuard `.conf` N개를 `$TUBEDEPTH_EGRESS_DIR`(기본 `var/egress/`, 모드 **0700**)에 둔다.
  기동 시 각 config에 `[http] BindAddress = 127.0.0.1:<port>` 섹션을 덧붙인 파생 config를 만들어 쓴다.
- `asyncio.create_subprocess_exec`로 프로세스를 띄우고, 자체 프로세스 그룹에 넣어 종료 시 그룹째 SIGTERM.
  포트는 고정 범위(`24100+`)에서 할당하되 바인드 실패 시 다음 포트로 물러난다.
- **로컬 무료 헬스체크**: wireproxy `-i 127.0.0.1:<info_port>`가 **`/readyz`**(마지막 CheckAlive ping)와
  **`/metrics`**(`wg show` 상당, 마지막 핸드셰이크 시각)를 준다. 둘 다 로컬이라 15초마다 돌려도 공짜다.
  스폰 전에 `wireproxy -n -c <rendered>`로 configtest를 돌려, 잘못된 렌더링이 정체불명의 자식 종료가 아니라
  이름 붙은 기동 오류가 되게 한다.
- **trace probe가 이 설계에서 가장 중요한 한 가지다**: 해당 프록시로
  `https://www.cloudflare.com/cdn-cgi/trace`를 GET한다. `key=value` 본문이라 JSON 의존성 없이
  **`ip=`와 `loc=`(출구 국가)를 한 번에** 준다(실측: `ip=119.194.145.146 loc=KR colo=ICN`).
  그 IP가 **직결 IP와 같으면 즉시 실패로 처리한다.** 터널이 안 붙었는데 프록시만 살아 있는 상태는 조용히
  원래 IP로 나가는 것이고, 그게 이 서브시스템에서 유일하게 위험한 무증상 실패다.
- **probe는 절대 YouTube를 건드리지 않는다.** YouTube 카나리아는 자기가 지키려는 바로 그 쿼터를 태우고,
  게다가 *통과하면서도* 실제 추출은 실패할 수 있다(`--dump-json`이 봇 검사를 맞기 시작한 뒤에도 맨 `watch`
  페이지는 한참 더 200을 준다). **YouTube 적합성은 오직 실제 트래픽의 판정에서만 유도한다.** 예외는
  서드파티 lane뿐 — 거기선 카나리아가 공짜고 모호하지 않다(`/votes?videoId=dQw4w9WgXcQ` → 200 + `dislikes` 키).
- **비밀 취급 — `.gitignore`는 방어가 아니라 backstop이다.** ProtonVPN config에는 `PrivateKey`가 들어 있고,
  `.gitignore`는 *트리 안에 있는* 파일만 도와준다. 그래서:
  - 원본 config는 **저장소 바깥** `~/.config/tubedepth/wireguard/`에, 디렉터리 0700 / 파일 0600.
  - 렌더링된 런타임 config(원본 + `[http]` 섹션)는 **`$XDG_RUNTIME_DIR/tubedepth/egress/`**에 0600으로 쓰고
    `stop()`의 `finally`에서 unlink한다. 이 머신에서 `/run/user/1000`은 **tmpfs, mode 0700**임을 확인했으므로
    개인키가 디스크에 닿지 않는다.
  - `.gitleaks.toml`에 `^\s*(Private|Preshared)Key\s*=\s*[A-Za-z0-9+/]{42,43}=` 규칙을 추가한다.
    테스트 fixture는 명백히 가짜인 키(`AAAA…AAA=`)를 써서 allowlist가 형식적인 것이 되게 한다.
  - 로그 필터가 포맷된 레코드에서 키 정규식을 스캔한다 — 테스트에선 raise, 운영에선 warn.

### 라우팅 정책 — Lane과 Backend는 다른 축이다

**이 구분이 이 서브시스템에서 가장 중요한 설계 결정이다.** 우리를 rate limit 하는 주체는 우리 내부 분류가
아니라 *서비스*다:

- **Backend**(`YT_DLP`/`INNERTUBE`/`THIRD_PARTY`)는 **어떤 egress를 쓸 자격이 있는가**(라우팅)를 정한다.
- **Lane**(`YOUTUBE`/`RYD`/`SPONSORBLOCK`)은 **어떤 예산을 갉아먹는가**(속도 제어)를 정한다.

둘은 직교한다. 자막 `json3` GET은 라우팅상 `THIRD_PARTY`(풀로 팬아웃 가능)지만 rate limit상
**`Lane.YOUTUBE`**다 — 같은 구글 관용치를 소비하기 때문이다. 이걸 하나로 합치면 RYD의 100/분 제한이
SponsorBlock을 옥죄거나, 자막 GET이 YouTube 예산에서 빠져나가 관측이 틀어진다.

| Backend | 적격 egress | 근거 |
| --- | --- | --- |
| `YT_DLP` · `INNERTUBE` | **`residential` 라벨만** (기본적으로 VPN 제외) | 데이터센터 IP가 봇 검사 1순위 |
| `THIRD_PARTY` | **풀 전체, `vpn` 선호** | 직결 IP는 오직 그것만 할 수 있는 일(YouTube)에 아껴둔다 |

`TUBEDEPTH_EGRESS_ALLOW_VPN_FOR_YOUTUBE` 기본 `0`. 이건 소심함이 아니라 관측 결과다 — 켜는 것은 고침이
아니라 *측정*이고, 풀이 매 시도의 판정을 기록하므로 한 시간이면 답이 나온다. 안 통하면 컨트롤러가
물어보기 전에 이미 강등해 놨을 것이다.

**선택은 감쇠 Beta 사후분포에 대한 Thompson 샘플링이다.** `score = betavariate(alpha+1, beta+1)`.
가중 최소사용보다 나은 이유: 튜닝 상수가 없고, argmax가 아니라 *샘플링*이라 부하가 자연히 분산되며,
**데이터가 없는 경로도 시도한다**(ε-greedy는 고정 예산을 낭비하고 순수 가중 랜덤은 아예 시도조차 안 한다).
근소한 차이는 가장 오래 안 쓴 쪽으로 기울여(`staleness bonus 5%`) 한 경로가 트래픽을 독식하는 동안 쌍둥이의
사후분포가 낡아버리는 것을 막는다.

**AIMD 적응 제어 — `(egress, lane)`마다:**

- 성공 → `window += 1/window`(TCP 혼잡 회피: 한 윈도우 전부 성공에 +1), `interval *= 0.95`, `alpha += 1`
- 429 → `window = max(1, window×0.5)`, `interval = min(ceil, max(floor, interval×2))`, `beta += 1`
- 봇 검사(`BLOCKED`) → `window = 1.0`, `beta += 3`, **격리**
- `alpha`/`beta`는 **지연 지수 감쇠**(반감기 30분)라 한 시간 전에 나빴던 경로가 스위퍼 잡 없이도 다시 시도된다

격리는 `base 5분 × 2^(streak-1)`, 상한 1시간, decorrelated jitter. 해제 후에는 옛 윈도우가 아니라
**slow start(`window=1`)로 복귀**한다.

> **풀 전체 격리 가드 — 없으면 안 된다.** 한 lane의 egress 중 **60% 이상이 이미 격리 상태면 추가 격리를
> 거부하고 `PoolWideFailure` 알람만 올린다.** 모든 주소가 같은 5분 안에 동시에 나빠지는 일은 거의 없다 —
> 그건 우리 문제이거나 저쪽 문제다. 그 상황에서 풀 전체를 격리하면 두 시간짜리 장애가 두 시간 장애 +
> 전 경로 콜드 스타트가 된다.

```python
class EgressHealth(Base):
    __tablename__ = "egress_health"
    egress_name / lane          # PRIMARY KEY — backend가 아니라 lane이다
    alpha / beta                # decayed Beta posterior
    window: float               # AIMD 동시 허용치 (분수라 additive increase가 매끄럽다)
    interval_seconds: float     # AIMD 최소 시작 간격
    quarantined_until / quarantine_streak / consecutive_failures
    last_verdict / last_used_at
    day_started_at / day_used   # RYD의 일일 예산용
    total_attempts / total_successes / total_throttles / total_blocks


class EgressAttempt(Base):
    """Append-only. 이 테이블이 '한 IP가 실제로 얼마나 견디는가'라는,
    아무도 미리 알려줄 수 없는 숫자에 답하는 계측기다. 7일 보존 후 리퍼가 정리한다."""
    attempt_id / egress_name / lane / job_kind / work_class
    verdict / http_status / started_at / duration_ms / public_ip
```

**영속화는 write-behind다.** 컨트롤러가 메모리에서 권위를 갖고, `egress_health`는 5초마다 + 종료 시 flush,
`egress_attempt`는 bounded 큐를 통해 200건 배치로 삽입한다. 요청마다 같은 SQLite 파일에 쓰면 그게
`BEGIN IMMEDIATE` 클레임 락과 경합해 **클레임 지연으로 나타나고 "큐 문제"로 오진된다.** flush 큐가 차면
attempt 행은 카운터만 올리고 버린다 — 계측이 작업에 역압을 걸어선 안 된다. 기동 시 `egress_health`를 한 번
읽어 워밍업하므로 재시작이 격리된 주소를 다시 태우지 않는다.

> **WSL2 함정**: Windows 절전/복귀 후 벽시계가 점프한다. 모든 간격·윈도우·격리 기한은 `time.monotonic()`을
> 쓰고, 벽시계 타임스탬프는 영속 행과 `/healthz` 표시에만 쓴다.

### 자막 fetch는 추출한 egress를 따라가야 한다

분할 egress 설계가 만들어내는 실제 위험이다. transcript 잡은 직결 IP에서 yt-dlp로 자막 트랙을 찾은 뒤
`timedtext`/`json3` URL을 GET하는데, **그 URL을 획득한 것과 다른 주소에서 가져오면 403이 나는 패턴이 흔하다.**
그래서 transcript 잡은 `prefer=<추출을 수행한 egress>`를 넘기고, 선택기는 그 egress가 수용 가능하면 우선한다
(어피니티 보너스 15%). VPN 팬아웃은 폴백이며, 건강 테이블이 판정하는 *실험*이지 확정 정책이 아니다.

### 병렬 처리 모델

전역 세마포어 3개를 버리고, **작업 클래스별로 워커 집합과 클레임 쿼리를 분리한다.** 굶주림은 *목적지*가
아니라 *지속시간* 속성이므로 lane이 아니라 클래스로 갈라야 한다:

| 클래스 | kind | 워커 | 실행 방식 |
| --- | --- | --- | --- |
| `auxiliary` | dislikes · sponsor_segments · 자막 json3 | 32 태스크 | httpx만 |
| `extraction` | metadata · channel about · related · 자막 트랙 탐색 | 8 태스크 | yt-dlp **전용 스레드풀**, InnerTube는 httpx |
| `harvest` | comments · channel videos · search · playlists | 3 태스크 | yt-dlp **subprocess** |

각 클래스는 **자기 클레임 쿼리**(`WHERE kind IN (...)`)와 자기 워커 집합을 갖고 서로에게서 빌리지 못한다.
이게 굶주림에 대한 구조적 답이다 — 댓글 백로그가 auxiliary 워커를 물리적으로 점유할 수 없다.
더해서 각 클래스는 **egress window의 지분**을 갖는다(`extraction 0.6 / harvest 0.34 / auxiliary 1.0`):
`(direct, youtube)`의 window가 6이면 harvest는 최대 2슬롯만 쥐고 extraction에는 항상 3이 남는다.
**이것이 댓글 스크레이프가 가정용 IP를 독점하는 것을 막는 조각이다.**

수용 체인은 곱이 아니라 항상 같은 순서의 **순차 통과**다(그래서 데드락이 불가능하다):
`워커 슬롯(= 워커 태스크 자신) → API 키 공정성(클레임 쿼리의 제외 집합) → egress 선택 → lane interval`.
슬롯과 interval은 **요청 시작 전에 `select()` 안에서 확보**한다 — 안 그러면 동시 워커들이 같은 틱에 전부
interval 게이트를 통과한다. 수용 가능한 게 없으면 워커는 **폴링하지 않고** `asyncio.Event`를 기다린다
(타임아웃은 적격 집합 중 가장 이른 `next_earliest_start`).

yt-dlp 블로킹 처리:
- `extraction`(짧음) → **전용** `ThreadPoolExecutor(max_workers=min(8, cpu_count))`. 기본 executor를 쓰면
  안 되는 이유는 `asyncio.to_thread`가 artifact 파일 IO도 서빙하기 때문이다 — 공유하면 4초짜리 추출이
  gzip 쓰기를 막는다.
- `harvest`(김) → **subprocess**. 6분짜리 댓글 스크레이프를 스레드 안에서 협조적으로 중단하는 건 신뢰성 있게
  안 되지만 pid에 `terminate()` → `kill()`은 된다. 앞서 약속한 협조적 취소를 harvest에서 지키는 방법이 이것이다.
- 양쪽 모두 프록시를 같은 `lease.proxy_url`에서 받고, **yt-dlp의 `--sleep-requests`는 컨트롤러가 그
  `(egress, youtube)`에 대해 현재 갖고 있는 `interval`로 채운다** — yt-dlp 내부 페이싱이 별도 상수가 아니라
  같은 적응 측정을 따라간다.

### 처리량 현실 — 목표를 미화하지 않는다

건당 실제 요청 수가 종류마다 두 자릿수 차이가 난다. 이게 전부다:

| kind | YouTube 요청 수 | 시간당 수천 건? |
| --- | --- | --- |
| `dislikes` · `sponsor_segments` | **0** (YouTube가 아님) | **여유롭게 가능.** 팬아웃이 실제로 이득인 유일한 구간 |
| 캐시 히트 | 0 | 무제한에 가깝다. 중복 제거·`fingerprint` 캐시가 실효 처리량의 최대 지렛대 |
| `video.metadata` · `related` · `search` | ~1–3 | **가능해 보인다.** 병렬 4로 실측 ≈4,600/시간. 다만 표본 4건 |
| `channel.videos` · `playlist.items` | 페이지당 1 | 항목 수에 비례. 500개 재생목록 = 수십 요청 |
| `video.comments` | **20건당 ~1 → 2,000댓글이면 100+** | **불가능하다.** 수집 하나가 metadata 100건분이다 |

**RYD의 일일 한도가 이 풀의 진짜 정량적 근거다 — 그리고 이건 추정이 아니라 문서화된 숫자다.**
Return YouTube Dislike는 **클라이언트당 100 req/분 *그리고* 10,000 req/일**을 명시하고 초과 시 429를 준다.
분당 한도는 넉넉하지만 **일일 한도가 벽이다**: 하루 평균으로 환산하면 한 주소가 **시간당 약 416건**이다.
exit 10개면 **시간당 약 4,100건** — 목표가 이 종류에서는 충족되고, exit 수에 선형으로 비례한다.
`[lane.ryd] daily_budget = 9000`(10% 헤드룸)이 429를 모으는 대신 미리 멈추기 위해 존재한다.

SponsorBlock의 실제 한도는 **미공개**다(429는 있으나 임계는 안 밝힘). 해시 프리픽스 엔드포인트
(`/api/skipSegments/:sha256HashPrefix`)는 흔한 주장과 달리 **처리량을 거의 안 줄인다** — 4자리 hex는 65,536개
버킷 중 하나라, 시간당 서로 다른 영상 2,000건이면 버킷도 ~1,970개로 거의 그대로다. 절감은 6.5만 건을 넘거나
캐시된 코퍼스에 반복 접근할 때 나타난다. 그래도 쓴다 — 더 정중하고 공짜니까 — 다만 처리량을 여기 걸지 않는다.

정직한 결론:

1. **구속 조건은 큐도 CPU도 SQLite도 아니라 (a) YouTube의 IP당 관용과 (b) RYD의 일일 한도다.** 다른 건 근처에도
   못 온다. 16코어·15GiB에 yt-dlp 스레드 8개 + wireguard-go 프로세스 10개(각 ~35MB)는 여유롭고, WAL SQLite에
   초당 수백 write도 여유롭다.
2. **YouTube 천장은 exit을 늘려도 안 올라가고, 아마 내려간다.** 그걸 실제로 옮기는 유일한 수단은
   레지덴셜/모바일 프록시(대략 $5–15/GB)이고, `ExternalProxyEgress`가 그걸 TOML 한 줄 + env 하나로 만든다.
   **YouTube를 빠르게 하려고 Proton을 사는 것은 잘못된 구매다. RYD·SponsorBlock lane을 넓히려고 사는 것이 옳다.**
3. **댓글 수집은 두 번 비싸다.** 시간당 수천은 어떤 구성으로도 안 되고(1,000댓글 = 50+ 요청 = 1~3분,
   동시 3개면 시간당 60~180건), 더 중요하게는 **그 50여 요청이 metadata가 써야 할 바로 그 IP 예산을 갉아먹는다.**
   댓글을 대량으로 돌리면 metadata 처리량이 직접 떨어진다. `max_comments` 상한이 실질적 제어다.
4. **가장 값싼 배수는 요청 수를 줄이는 것이고, 유일하게 우리가 완전히 통제하는 축이다.** 공격적인 artifact
   캐싱, 네거티브 캐싱(SponsorBlock 404는 *결과*다, 캐시하라), 필요 없으면 `--flat-playlist`, `max_comments`
   상한, artifact에 이미 있는 것은 절대 재추출 안 함. **캐시 히트율 40%가 두 번째 IP보다 값지고 공짜다.**

### 실패 처리 — 순수 분류기 하나

`classify(lane, status, exc, body_head) -> EgressVerdict`. IO 없는 순수 표 기반 함수이고, 표의 각 행이
자기 테스트를 갖는다.

| 신호 | 판정 | egress 건강에 미치는 영향 |
| --- | --- | --- |
| HTTP 429 | `THROTTLED` | window 절반, interval 배증 |
| yt-dlp `Sign in to confirm you're not a bot` / `not available on this app` | `BLOCKED` | **그 egress만** 격리 |
| `googlevideo.com`·`/api/timedtext` 403, RYD 403(우린 항상 브라우저 UA를 보내므로 403은 주소 문제) | `BLOCKED` | 동일 |
| httpx `ConnectError`·`ProxyError`·TLS 실패 | `TRANSPORT` | 감독자 문제. **재시도 예산을 소모하지 않고** 즉시 다른 egress |
| `not available in your country` | `GEO` | 패널티 없음. 다른 *국가* egress로 재시도 |
| **SponsorBlock 404** | `NEUTRAL` | **없음 — 구간 없음은 오류가 아니라 답이다** |
| 비공개·삭제·멤버십·연령 제한 | `NEUTRAL` | 없음. 잡만 실패 |
| 200인데 못 읽는 모양 | `PARSER` | **없음.** 대신 `source_health`의 *백엔드* 서킷 브레이커를 올린다 |
| **그 외 전부** | `NEUTRAL` | **없음** |

마지막 행이 안전 속성 그 자체다: **모르는 결과가 멀쩡한 주소를 태우게 두어선 안 된다.**

`PARSER`가 egress 건강을 절대 안 건드리는 것도 같은 이유다 — YouTube가 렌더러 이름을 바꾼 날 분류기가
오작동해서 **가진 주소를 전부 격리하는** 사고가 구조적으로 불가능해야 한다. 위의 풀 전체 격리 가드가
두 번째 방어선이다.

재시도는 **반드시 다른 egress를 고른다**(`exclude=최근 3회 시도의 egress`). 같은 IP로 곧장 다시 때리는 것이
소프트 블록을 하드 블록으로 만드는 행동이다. 강등 사다리: 최근 3개 제외 → 비면 최근 1개 제외 → 비면 격리
아닌 아무거나 → 그래도 비면 **시도를 태우지 말고 잡을 백오프와 함께 큐로 되돌린다.**
- `GET /healthz`가 노출: egress별 `{name, kind, observed_public_ip, permit_limit, inflight,
  success_rate_1h, quarantined_until}` + 큐 깊이 + 워커 하트비트 + `extraction_error_24h`.
  운영자가 "왜 안 도는지"를 한 화면에서 봐야 한다.

### 설정

```
TUBEDEPTH_EGRESS_CONFIG        var/egress/pool.toml
TUBEDEPTH_EGRESS_DIR           var/egress          # 0700, .gitignore 대상
TUBEDEPTH_WORKER_SLOTS         8
TUBEDEPTH_LANE_SLOTS           cheap=2,standard=4,expensive=2
TUBEDEPTH_EGRESS_MAX_PERMITS   6
TUBEDEPTH_WIREPROXY_BINARY     wireproxy
TUBEDEPTH_EGRESS_PROBE_URL     https://api.ipify.org
```

**이 서브시스템은 파이썬 의존성을 하나도 추가하지 않는다.** `pool.toml`은 stdlib `tomllib`(3.11+),
프로세스 감독은 `asyncio.create_subprocess_exec`, 프록시는 이미 있는 httpx와 yt-dlp의 기본 기능이다.
wireproxy는 파이썬 패키지가 아니라 nix로 설치되는 외부 바이너리이며 `tool/doctor.sh`가 확인한다.
httpx에 `socks` extra가 필요 없는 것도 HTTP CONNECT를 고른 이유 중 하나다.

```toml
# var/egress/pool.toml
[[egress]]
name     = "direct"
kind     = "direct"
backends = ["yt_dlp", "innertube", "third_party"]

[[egress]]
name     = "proton-jp1"
kind     = "wireproxy"
config   = "proton-jp1.conf"          # var/egress/ 기준 상대경로, git에 안 들어감
backends = ["third_party"]            # YouTube 백엔드는 측정 성공률로만 승격
```

### 테스트

선택기·AIMD·격리는 전부 **순수 로직**이라 네트워크 없이 테스트된다. `FakeEgressProvider`가
`EgressEndpoint`를 돌려주고, wireproxy 감독은 subprocess 스텁을 프로토콜 뒤에 끼우며, probe는 respx로 모킹한다.

```
test_a_quarantined_egress_is_not_selected_for_the_next_attempt
test_a_retry_after_a_bot_check_selects_a_different_egress
test_a_bot_check_halves_the_permit_limit_and_doubles_the_minimum_interval
test_a_successful_request_increases_the_permit_limit_by_less_than_one
test_an_egress_whose_probe_returns_the_direct_public_ip_is_marked_failed
test_a_comment_harvest_cannot_occupy_the_slots_reserved_for_cheap_jobs
test_yt_dlp_and_httpx_receive_the_same_proxy_url_for_one_egress
test_third_party_jobs_fan_out_across_every_healthy_egress
test_youtube_jobs_prefer_the_direct_egress_while_its_success_rate_holds
test_an_egress_configuration_file_is_never_readable_outside_the_owner
```

`test_an_egress_whose_probe_returns_the_direct_public_ip_is_marked_failed`가 이 묶음의 핵심이다 —
조용한 IP 누출을 잡는 유일한 테스트다.

---

### 에러 taxonomy

```python
# src/tubedepth/errors.py
#
# One base class, a small taxonomy under it, caught once at each boundary — the
# FastAPI handler for HTTP, the typer wrapper for the CLI, the worker loop for
# jobs. Two properties live on the class rather than at the call site, because
# both were getting decided inconsistently: the HTTP status, and whether a
# retry is worth attempting.

class TubedepthError(Exception):
    """Base for domain errors that are safe to show a CLI user or an API client."""
    code: ClassVar[str] = "tubedepth_error"
    status_code: ClassVar[int] = 500
    retryable: ClassVar[bool] = False

class ValidationError(TubedepthError):            code, status_code = "validation_error", 400
class UnauthenticatedError(TubedepthError):       code, status_code = "unauthenticated", 401
class RevokedKeyError(TubedepthError):            code, status_code = "revoked_key", 403
class NotFoundError(TubedepthError):              code, status_code = "not_found", 404
class ConflictError(TubedepthError):              code, status_code = "conflict", 409   # 결과를 잡 완료 전에 요청
class UnavailableError(TubedepthError):           code, status_code = "unavailable", 422 # 비공개·삭제·멤버십·연령·지역
class AuthenticationRequiredError(TubedepthError):code, status_code = "authentication_required", 424
class RateLimitedError(TubedepthError):           code, status_code, retryable = "rate_limited", 429, True
class UpstreamError(TubedepthError):              code, status_code = "upstream_error", 502
class TransientUpstreamError(UpstreamError):      code, status_code, retryable = "transient_upstream_error", 503, True
class ExtractionError(TubedepthError):            code, status_code = "extraction_error", 502
class ConfigurationError(TubedepthError):         code, status_code = "configuration_error", 500
```

**`ExtractionError`를 `UpstreamError`와 분리하는 것이 핵심이다.** 렌더러 이름이 바뀌면 "네트워크 문제"가
아니라 "우리 파서가 낡았다"로 보여야 하고, 대응도 알림 대상도 다르다. 그리고 **`ExtractionError`는
재시도하지 않는다** — 우리가 이름을 바꾼 게 아무것도 없는데 15분에 걸쳐 세 번 더 때리는 것은 무의미하고,
IP를 차단당하는 바로 그 행동이다. 500이 아니라 502인 이유도 같다: 502는 운영자를 우리 트레이스백이 아니라
`renderers.py`와 에러 메시지 안의 "실제 관측된 이름 목록"으로 보낸다.

메시지는 집안 규칙대로 소문자·마침표 없음·문제된 값 포함: `f"video is not available: {video_id}"`,
`f"no data source registered for job kind: {kind.value}"`, `f"job has not finished: {job_id}"`.

> 이름 충돌 대비: 이 `ValidationError`는 pydantic 것을 가린다. 둘 다 필요한 모듈에선 pydantic 쪽을
> `PydanticValidationError`로 import한다. `knowstore`도 같은 충돌을 안고 살고 있다.

API 쪽에는 `@application.exception_handler(TubedepthError)` **하나만** 둔다 — 각 클래스가 자기 상태코드를
들고 다니므로 에러 타입 추가가 이 파일을 건드리지 않는다. `RateLimitedError`/`ConflictError`는 `Retry-After`를
붙인다. 워커 매핑: `error.retryable and attempt_count < max_attempts` → 백오프 후 재큐, 아니면 `failed` +
`error_code`. `error_code`가 taxonomy 멤버라 `GET /v1/jobs?error_code=authentication_required`가 유용한 쿼리가 된다.

---

## 저장소 스캐폴딩 (`project-scaffold` 준수)

`SCAFFOLD.md`의 파일 세트를 그대로 세운다: `.githooks/{pre-commit,pre-push,commit-msg}`,
`tool/checks/{prerequisite,format,lint,test}`, `tool/doctor.sh`, `tool/worktree.sh`, `Justfile`,
`.github/workflows/{ci.yml,secret-scan.yml}`, `docs/{status,definition-of-done,troubleshooting}.md`,
`decisions/README.md`(표만), `AGENTS.md` + 3줄 포인터 `CLAUDE.md`, `README.md`, MIT `LICENSE`.
브랜치 `master` ← `dev`(기본) ← `feature/<name>`.

**주의**: `stacks/`에 파이썬 오버레이가 없다. `tool/checks/*`는 손으로 쓰는 것이며 `SCAFFOLD.md` 3절대로
*검증된 것처럼 말하지 않는다*. `tool/checks/prerequisite`는
`/home/user1/github_prj/configs/tool/checks/prerequisite`를 그대로 복사한다 — `require_command` 헬퍼가
uv 부재를 exit 69(미검증)로, `REQUIRE_NATIVE=1`(CI가 설정)이면 exit 1로 보고한다.

```sh
tool/checks/format   # uv run ruff format --check .
tool/checks/lint     # uv run ruff check . && uv run basedpyright
tool/checks/test     # uv sync --extra dev --frozen && uv run pytest
```

`--frozen`은 속도 최적화가 아니다. 없으면 uv가 `uv.lock`에 없는 최신 의존성으로 해석해도 테스트가 통과해
아무도 커밋하지 않은 것을 검증하게 된다. 락파일도 코드만큼이나 테스트 대상이다.

`pyproject.toml`은 `note-store`/`nvim-starter` 형식: hatchling 백엔드, `packages = ["src/tubedepth"]`,
상·하한 핀(`"fastapi>=0.115,<1"`, `"SQLAlchemy>=2.0,<3"`, `"typer>=0.12,<1"`, `"httpx>=0.27,<1"`)
**단 `yt-dlp>=2026.7`는 상한 없음(위 근거)**, ruff `line-length=100` `select=["E","F","I","UP","B","SIM"]`,
basedpyright `standard`. 모든 모듈은 `from __future__ import annotations`로 시작하고 식별자를 축약하지 않는다
(`repository`·`database`·`parameters`이지 `repo`·`db`·`params`가 아니다).

> 집안 규칙에 없는 것을 하나 추가한다고 명시: 그들에겐 파이썬 포매터 선례가 없다(nvim-starter는 `nix fmt`에
> 위임). 이미 스택에 있는 도구인 `ruff format --check`를 쓴다. black은 도입하지 않는다.

`decisions/`는 **실제로 무언가 깨진 뒤에만** 쓴다. 지금 미리 채우지 않는다.

### 배포

Docker가 없으므로 **systemd user unit 2개**(`tubedepth-api.service`, `tubedepth-worker.service`),
`uv run --frozen`으로 락 고정 실행. 사설망 바인딩 + 리버스 프록시 뒤. DB와 payload_root는 리눅스 파일시스템에 둔다
(WAL이 drvfs에서 안 되는 문제 때문).

**wireproxy 프로세스는 별도 유닛으로 두지 않고 워커가 자식으로 감독한다** — 워커가 죽으면 egress도 같이
정리되어야 하고, 유닛을 나누면 "워커는 죽었는데 터널만 살아 있는" 상태가 생긴다. `tool/doctor.sh`가
`wireproxy` 존재와 `var/egress/`의 퍼미션(0700)을 확인한다.

Docker가 나중에 설치되면 `GluetunEgress`는 `pool.toml`에 `kind = "gluetun"` 항목을 추가하는 것으로 켜진다.
Docker 설치 자체는 sudo가 필요하므로 사용자가 직접 실행하는 문서화된 수동 단계다.

---

## 테스트 전략 — 마지막 층만 네트워크를 만진다

1. **정규화 단위 테스트** — 가치의 대부분. 캡처한 raw JSON → `normalize()` → 공개 스키마.
2. **가짜 소스로 파이프라인 테스트** — seam은 `SourceRegistry`가 `ExtractionService`에 *주입*된다는 것
   (절대 import 하지 않는다). `StaticSource`만 담은 레지스트리로 submit→claim→run→persist→poll→result를
   네트워크 없이 밀리초에 돈다.
3. **`respx`로 HTTP 소스 테스트** — RYD의 UA 요구와 SponsorBlock의 404가 각각 명시적 테스트를 갖는다.
4. **`httpx.ASGITransport`로 API 테스트** — 실서버도 포트도 없다.
5. **실제 SQLite 파일 대상 동시성 테스트** — K개 스레드가 시드된 큐를 claim, 각 잡이 정확히 한 번 실행됨을 단언.
   큐 설계 전체를 정당화하는 테스트이므로 반드시 CI에 있어야 한다.
6. **InnerTube fixture 회귀** — 저장된 응답을 파싱. 렌더러 이름을 바꾼 fixture는 `ExtractionError`를 내야 한다.
7. **라이브 스모크** — `@pytest.mark.live`, 기본 미선택. 사람이 의도적으로 돌린다.

**CI를 기계적으로 오프라인에 묶기**: `addopts = "-q -m 'not live'"` + `tests/conftest.py`의 autouse 픽스처가
`live` 마크가 없는 테스트에 대해 `socket.socket`을 monkeypatch해 예외를 던지게 한다. "실수로 진짜 요청을
추가했다"가 간헐적 CI 플레이크가 아니라 즉각적이고 이름이 붙은 실패가 된다.

**fixture 캡처**(`tubedepth capture-fixture`, 사람이 의도적으로 실행)는 쓰기 전에 반드시 제거한다:
`formats` 37개 서명 `googlevideo.com` URL(시한부이고 죽은 무게이며 pre-commit의 `gitleaks`가 잡는다 —
`yt-dlp --dump-json > fixture.json`으로 때우면 안 되는 구체적 이유다), `subtitles`/`automatic_captions`의
모든 `url`(언어와 포맷명은 남긴다), 쿠키·세션 자료. 이를 강제하는 회귀 테스트를 둔다:
`test_no_committed_fixture_contains_a_googlevideo_url`.

테스트 이름은 집안 스타일대로 문장으로:

```
test_a_video_without_chapters_normalizes_to_an_empty_chapter_list
test_the_hundred_heatmap_buckets_become_a_ranked_most_replayed_list
test_a_comment_whose_parent_is_root_becomes_a_top_level_comment
test_a_hearted_comment_is_reported_as_hearted_and_not_as_favorited
test_sponsorblock_returning_404_produces_an_empty_segment_list_and_not_a_failure
test_return_youtube_dislike_is_requested_with_a_browser_user_agent
test_a_caption_url_is_never_written_into_a_stored_artifact
test_an_innertube_response_with_no_known_renderer_raises_extraction_error_not_an_empty_list
test_a_renamed_renderer_in_a_fixture_fails_the_related_video_parser
test_two_workers_claiming_the_same_queue_never_run_one_job_twice
test_a_job_whose_lease_expired_is_requeued_rather_than_left_running
test_a_private_video_fails_terminally_and_is_not_retried
test_a_fresh_cached_artifact_is_returned_without_creating_a_job
test_max_age_zero_forces_a_refetch_even_when_a_fresh_artifact_exists
test_two_identical_requests_in_flight_share_one_job
test_a_request_without_an_api_key_is_rejected_before_a_job_row_is_created
test_a_revoked_api_key_is_rejected_with_403
test_a_bundle_that_loses_sponsorblock_still_succeeds_with_a_degradation
test_a_get_on_a_convenience_alias_never_triggers_a_fetch
test_repeated_bot_rejections_open_the_circuit_breaker_for_that_backend
test_an_artifact_over_the_inline_limit_is_written_to_a_gzip_file
test_an_artifact_from_an_older_schema_version_is_treated_as_stale
test_registering_two_sources_for_one_job_kind_is_rejected_at_import_time
test_every_registered_source_declares_a_parameter_model_and_a_default_freshness
test_the_cli_and_the_api_produce_the_same_normalized_document
```

끝의 세 개가 구조를 지키는 테스트다. 앞의 둘은 확장성 가드로, 레지스트리에 대한 메타 테스트가 소스 추가를
*조심스러운* 작업이 아니라 *안전한* 작업으로 만든다. 마지막 하나는 집안의 아키텍처 규칙 —
"API는 CLI와 같은 service 위에 얇게 얹는다" — 을 기계적으로 강제한다. 둘이 갈라지는 순간 빨갛게 된다.

---

## 구현 단계 — 각 단계는 눈으로 볼 수 있는 것으로 끝난다

| # | 내용 | 끝났다는 증거 |
| --- | --- | --- |
| **M0** | 저장소 스캐폴딩, git init, 훅, CI, doctor | 새 클론에서 `uv sync --extra dev`; `tool/doctor.sh`가 미설정 `core.hooksPath`에 정확한 명령을 대며 실패; 잘못된 커밋 메시지 거부; uv 없는 호스트에서 `tool/checks/test`가 exit 69 |
| **M1** | 도메인 코어, 네트워크 0. errors/models/database/repositories/identifiers/payload_store/configuration/services.jobs/worker + `StaticSource` **+ `egress/` 골격: `EgressEndpoint`·`DirectEgress`·선택기·AIMD·격리(전부 순수 로직)** | `tubedepth job submit --kind static.echo` → `job show`가 `succeeded`; 2워커 동시성 테스트 통과; 워커를 죽이면 lease 만료 후 재큐; **AIMD·격리 테스트가 네트워크 0으로 통과** |
| **M2** | 첫 실제 소스: `ytdlp_runtime` + `video_metadata` + fixture 캡처 | `tubedepth fetch metadata <url>`이 `chapters`·`most_replayed`·`tags`가 채워진 정규화 JSON 출력; 단위 테스트가 네트워크 차단 상태로 fixture에서 돎 |
| **M3** | HTTP API + **API 키 인증**. `api/`, 예외 핸들러, OpenAPI, 롱폴, Alembic 도입(스쿼시된 `0001`) | 키 없이 401 / 폐기 키 403 / 한도 초과 429; `POST /v1/videos/{id}/metadata` 202 → 폴링 → 페이로드; 즉시 재요청은 새 잡 없이 200 |
| **M4** | 자막 + httpx 소스: `transcripts`·`returnyoutubedislike`·`sponsorblock` | 자동 생성 포함 3개 언어 자막; 구간 없는 영상이 `[]` + `succeeded`; UA를 빼면 RYD가 403 나는 것이 *테스트*로 증명됨 |
| **M4.5** | **egress 풀 실물**: `WireproxyEgress`(프로세스 감독·포트 할당·probe), `pool.toml`, 백엔드별 라우팅, `egress_health` 테이블, `/healthz` 풀 상태. 서드파티 소스 직후에 두는 이유는 팬아웃이 실제로 이득인 첫 소비자가 그것들이기 때문 | wireproxy 2개가 뜨고 각각 **직결과 다른 공인 IP**를 보고; 터널이 안 붙은 egress가 probe에서 실패로 표시됨; RYD 잡이 두 egress에 번갈아 나감; 봇 검사를 주입하면 해당 조합이 격리되고 재시도가 **다른 egress**를 고름 |
| **M5** | 댓글 수집: `comments`, `SubprocessYtdlpRuntime`, 취소, lease 갱신, **비용 레인 슬롯 예약** | 2,000건 수집이 `parent_id` 스레딩과 pinned/hearted/verified 플래그와 함께 완료; 실행 중 취소가 5초 안에 subprocess 종료 + `cancelled`; **댓글 수집 여러 건이 도는 동안에도 dislike 잡이 굶지 않음** |
| **M6** | yt-dlp 탐색: `channel_profile`·`channel.videos`·`playlist`·`search` | 500개짜리 재생목록 전량; `ytsearch` 결과; 채널 프로필(팔로워는 **반올림값**으로 표기) |
| **M7** | **InnerTube** — `innertube.py` + `related`·`channel_about`·`channel_community`. **fixture 하니스를 먼저 만든다** | 렌더러 이름을 바꾼 fixture로 테스트가 실패하는 것을 시연; 관련영상·커뮤니티 글·가입일/국가/링크 반환 |
| **M8** | 합성·운영: `bundle`·`webhooks`·`retention`·`/v1/sources`, **systemd 유닛(API·워커·wireproxy 감독)**, `ExternalProxyEgress`, `GluetunEgress`(설정만 — Docker 설치는 사용자 몫), README "Honest limits", `docs/status.md` | 한 번의 bundle 요청이 메타+자막+싫어요+구간을 반환하고 실패한 하나는 degradation으로 기록; HMAC 서명 웹훅 도착; 보존 정리가 1주 된 잡과 고아 블롭 삭제; 재부팅 후 서비스와 **egress 풀이 함께** 자동 기동 |
| **M9** | **seam 증명** — 라이브 채팅 리플레이 추가 | *합격 기준은 diff 크기다*: 새 모듈 1개 + `JobKind` 1줄 + import 1줄, 그 외 변경 0. 이보다 크면 확장 설계가 틀린 것이고, 소스가 여섯 개 쌓이기 **전인** 지금이 그걸 알아낼 때다 |

> Alembic을 M0가 아니라 M3에 넣는 이유: M1–M2 동안 스키마가 매일 바뀌어 모든 마이그레이션을 다시 쓰게 된다.
> `knowstore`처럼 1일차부터 넣는 대안은 문서화된 트레이드오프다.

---

## 검증 방법

```sh
# 계층별
uv run pytest                  # 오프라인 단위·파서·큐·API 테스트 (CI가 도는 것)
uv run pytest -m live          # 실제 YouTube 대조 (수동)
just check                     # format + lint + test

# 종단간 — 실제로 돌려보기
uv run tubedepth key create --label local        # 키 발급 (한 번만 출력됨)
uv run tubedepth worker &                         # 워커
uv run tubedepth serve --port 8080                # API
KEY=...
curl -s -H "X-API-Key: $KEY" -X POST localhost:8080/v1/jobs \
     -d '{"kind":"video.bundle","target":{"video_id":"dQw4w9WgXcQ"}}'
curl -s -H "X-API-Key: $KEY" "localhost:8080/v1/jobs/$ID?wait=30"
curl -s -H "X-API-Key: $KEY" localhost:8080/v1/jobs/$ID/result \
  | jq '.data.most_replayed | length'             # 100 기대
curl -s -H "X-API-Key: $KEY" localhost:8080/v1/sources | jq '.[].kind'   # 레지스트리 확인
curl -s localhost:8080/v1/jobs                    # 키 없이 → 401, 잡 행 생성 안 됨
curl -s localhost:8080/version                    # 앱 + yt-dlp 버전

# egress 풀 — 가장 중요한 확인은 "정말 다른 IP로 나가는가"다
nix profile install nixpkgs#wireproxy              # sudo 불필요
uv run tubedepth egress probe                      # 각 egress의 공인 IP·지연·상태를 표로 출력
#   direct       119.194.145.146  KR/AS4766  12ms   ok
#   proton-jp1   <다른 IP>        JP         ..ms   ok
#   proton-nl1   119.194.145.146  KR/AS4766  ..ms   FAILED: tunnel not established
curl -s -H "X-API-Key: $KEY" localhost:8080/healthz | jq '.egress'   # permit_limit·격리 상태
```

`egress probe`가 직결과 **같은 IP**를 보고하면 그 egress는 터널이 안 붙은 것이고, 시스템은 이를 성공이 아니라
실패로 처리해야 한다. 이 한 줄이 조용한 IP 누출을 막는 유일한 방어선이다.

---

## Honest limits (README에 그대로 들어갈 내용)

- **정확한 구독자 수는 제공할 수 없다.** YouTube가 반올림 값만 공개한다(`"4.53M subscribers"`).
  Data API와 같은 한계이며 스크래핑으로도 우회 불가. 팔로워 수는 항상 "반올림값"으로 표기한다.
- **트렌딩(인기 급상승)은 없다.** YouTube가 피드를 폐지했다. 대체 기능을 흉내내지 않는다.
- **InnerTube 기반 3종(관련영상·채널 About·커뮤니티)은 깨지기 쉽다.** 렌더러 이름이 예고 없이 바뀐다
  (`compactVideoRenderer` → `lockupViewModel`이 이미 그랬다). fixture 회귀가 변경을 *알려주지만* 새 이름을
  자동으로 찾아주지는 않는다.
- **댓글 전량 수집은 오래 걸리고, 취소는 손실을 동반한다.** 20건에 약 7초였고 수십만 건은 수십 분이다.
  yt-dlp가 추출 끝에 댓글을 쓰므로 취소하면 아무것도 안 남는다. `max_comments`로 미리 상한을 두는 것이 실질적 제어다.
- **차단될 수 있다.** YouTube는 요청량에 따라 로그인·PO token을 요구할 수 있다. 쿠키·`--impersonate`·프록시
  훅은 있지만 항상 통한다는 보장은 없다. 고장 났을 때 첫 수순은 이 코드 디버깅이 아니라
  `uv lock --upgrade-package yt-dlp`이고, 두 번째는 yt-dlp 이슈 트래커 읽기다.
- **ProtonVPN을 YouTube 경로로 쓰면 오히려 느려질 수 있다.** ProtonVPN exit은 데이터센터 IP이고, 데이터센터
  IP는 YouTube 봇 검사의 1순위다. yt-dlp 커뮤니티의 표준 권고 자체가 *"VPN과 데이터센터 프록시를 피하고
  레지덴셜 프록시를 쓰거나 VPN을 끄라"*이다. 이 프로젝트의 직결 회선은 **KT 가정용 IP**라 현재 가장 좋은
  경로가 이미 직결이며, 그래서 VPN egress는 기본적으로 서드파티(RYD·SponsorBlock) 전용이고 YouTube 백엔드로의
  승격은 **측정된 성공률로만** 일어난다. 프록시를 붙였는데 YouTube 처리량이 안 오르는 것은 버그가 아니다.
- **처리량 목표의 종류별 현실**: 시간당 수천 건은 `dislikes`·`sponsor_segments`·캐시 히트에서는 여유롭고,
  `video.metadata`·`related`·`search`에서는 **달성 가능해 보이며**(병렬 4 실측 환산 ≈4,600/시간, 단 표본 4건),
  **댓글 전량 수집에서는 어떤 구성으로도 불가능하다**(2,000댓글 = metadata 100건분 요청).
  지속 부하에서의 봇 검사 임계는 미검증이며, AIMD 컨트롤러가 런타임에 찾는다.
- **진짜 확장 축은 레지덴셜/모바일 프록시다.** `ExternalProxyEgress`가 그 자리를 비워두지만, 유료이고
  이 계획은 그 비용을 전제하지 않는다.
- **wireproxy는 TCP/CONNECT만 지원한다.** UDP는 이 경로로 못 나간다(현재 트래픽이 전부 HTTPS라 무관).
- **VPN 풀의 진짜 용도는 YouTube가 아니라 RYD다.** Return YouTube Dislike는 **클라이언트당 100 req/분,
  10,000 req/일**을 문서화한다. 일일 한도가 벽이라 한 주소가 시간당 약 400건, exit 10개면 약 4,000건이다.
  **그게 프록시 풀의 사업적 근거 전부이고 YouTube와는 무관하다.** SponsorBlock에도 같은 논리가 (한도가
  비공개라 덜 정밀하게) 적용된다.
- **ProtonVPN 동시 접속 수를 반드시 확인하라 — 그리고 wireproxy 프로세스 하나가 그 한 자리를 먹는다.**
  Plus 플랜은 동시 10 연결로 알려져 있고(플랜별로 다르고 공개 수치가 엇갈리므로 본인 계정에서 확인할 것),
  **풀이 당신의 휴대폰·노트북과 같은 할당량을 놓고 경쟁한다.** exit 10개를 띄우면 휴대폰이 끊긴다.
  이건 `docs/troubleshooting.md`에 Proton이 실제로 보여주는 문구를 제목으로 넣을 항목이다.
  무료 플랜은 서버·기기 선택지가 좁아 풀 용도로 부적합하다. Proton 약관은 서비스로 제3자 시스템을 공격·남용하는
  것을 금지하며, 스크레이퍼를 그 위에서 돌리는 것을 약관과 맞추는 일은 사용자의 책임이다.
- **egress config에는 개인키가 들어 있다.** `var/egress/`는 0700이고 저장소에 절대 커밋되지 않으며,
  pre-commit의 gitleaks가 마지막 방어선이다.
- **서드파티 데이터의 성격**: RYD 수치는 YouTube의 싫어요 수가 아니라 아카이브+확장 프로그램 텔레메트리로
  **재구성한 추정치**다. 응답에서 항상 추정치로 라벨링하고 그 라벨을 떼지 않는다. SponsorBlock 데이터는
  커뮤니티 기여물이며 **CC BY-NC-SA 4.0**이라 재배포 시 출처 표시와 비상업 조건이 따라붙는다.
- **개인정보**: 댓글 작성자명·채널 ID·아바타·프로필 URL은 저장되는 순간 GDPR과 개인정보보호법상 개인정보다.
  5만 행짜리 댓글 수집물은 캐시가 아니라 개인정보 보유다. 보존 기간과 접근 통제가 필요하다.
- **법적 위치**: YouTube 이용약관은 공개 API나 허용된 인터페이스 외의 자동화 접근을 금지한다. 이 프로젝트가
  하는 일 — 시청 페이지 추출, 대량 댓글 수집, 비공식 InnerTube 응답 읽기 — 은 그 약관이 허용하는 범위 밖이며,
  데이터가 공개적으로 보인다는 사실이나 요청률이 낮다는 사실이 이를 *허용됨*으로 바꾸지는 않는다.
  이 도구는 **사설망에서 API 키로 접근하는 소수 클라이언트**를 전제로 하며, 공개 서비스로 제공하지 않는다.
  잃어도 되지 않는 계정의 쿠키로 인증하지 않는다. 수집물(자막·댓글)은 제3자 저작물이므로 재배포하지 않는다.
  낮은 동시성 기본값은 성능 설정이 아니라 준수 태세이므로 올리지 않는다.
- **검증된 것과 아닌 것의 경계** (`project-scaffold/README.md` 방식):
  *검증됨* — yt-dlp 키 집합 78개, 자막 json3 취득, 댓글 20건 6.7초, RYD의 브라우저 UA 요구,
  SponsorBlock 200/404 양쪽 동작, InnerTube `/next`·`/browse` 도달과 렌더러 존재, SQLite 3.46.1,
  `wireproxy 1.1.3`이 nixpkgs에 존재, yt-dlp `--proxy`의 socks5/http 지원, 직결 egress가 KT 가정용 IP,
  metadata 추출 병렬 4건 3.11초.
  *미검증* — 지속적인 대량 수집과 그 봇 검사 임계, PO token 요구 조건, **실제 ProtonVPN egress를 통한
  요청**(config가 아직 없어 단 한 번도 프록시로 나가본 적 없음), 실부하에서의 AIMD 수렴 거동,
  다중 워커 장시간 운용, InnerTube about 탭 params 런타임 해석(현재 하드코딩 params는 조용히 홈 탭으로
  폴백함을 확인).
