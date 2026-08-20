# tubedepth

YouTube Data API가 주지 않는 영상·채널 데이터를 수집하는 자체 호스팅 API.

*[English](README.md) (정본)*

챕터, "가장 많이 본 구간" 히트맵, 태그, 정확한 업로드 시각, 자막 본문, 댓글 전량,
SponsorBlock 구간, 관련 영상, 채널 About, 커뮤니티 게시물. 클라이언트가 API 키로
**작업(job)을 등록**하고 정규화된 JSON을 받아간다.

```sh
curl -X POST -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
     -d '{"kind":"video.metadata","target":"dQw4w9WgXcQ"}' localhost:8080/v1/jobs
# 202 + job_id → 폴링 → chapters, most_replayed(100버킷), tags, published_at …
# 이미 신선한 결과가 있으면 잡을 만들지 않고 200 + 결과
```

전체 엔드포인트 레퍼런스: [`docs/api.ko.md`](docs/api.ko.md).

## 수집할 수 있는 것

<!-- kinds:start -->

| kind | 주는 것 | 공식 API로는 |
| --- | --- | --- |
| `video.metadata` | 챕터, 히트맵 100버킷, 태그, 정확한 업로드 시각, 라이선스, 자막 트랙 목록 | 태그는 소유자만, 나머지는 필드 없음 |
| `video.transcript` | 자막 본문 — 영상 자체 언어로, 사람이 쓴 것 우선 | 소유자 OAuth 없이 불가 |
| `video.comments` | 댓글 전량, `parent_id` 스레딩, 고정·하트·인증 플래그 | 가능하나 쿼터가 감당 안 됨 |
| `video.sponsor_segments` | SponsorBlock 구간 (커뮤니티 기여, CC BY-NC-SA 4.0) | 없음 |
| `video.related` | 관련 영상 | 없음 |
| `video.bundle` | 위 중 넷을 한 번에, 빠진 것은 `degradations`에 이유와 함께 | — |
| `channel.about` | 가입일, 국가, 외부 링크, **정확한 총 조회수**, 설명, 태그, 아바타 | 대부분 없음 |
| `channel.community` | 커뮤니티 게시물 | 없음 |
| `channel.videos` · `playlist.items` · `search.videos` | 목록 — `--then`으로 항목별 수집까지 팬아웃 | 가능하나 쿼터 소모 |
| `trending.videos` | YouTube 자신이 인기라고 부르는 것을, 그 순서 그대로 | chart 엔드포인트이고, 이 소스가 쓰는 것이 그것이다 |

<!-- kinds:end -->

`tubedepth sources` 또는 `GET /v1/sources`가 항상 실제 목록을 말한다.

## 왜 필요한가

Data API v3는 공개 영상에 대해서도 많은 것을 감춘다. `snippet.tags`는 영상 소유자에게만
반환되고, 자막 본문은 소유자 OAuth 없이 못 받으며, 챕터·히트맵·관련 영상·커뮤니티 게시물은
필드 자체가 없다. 댓글은 있지만 쿼터 소모가 커서 대량 수집에 못 쓴다.

## 시작하기

```sh
git config core.hooksPath .githooks   # 클론은 훅이 꺼진 상태로 온다
tool/doctor.sh                        # 툴체인·PostgreSQL 접속·훅 확인
uv sync --extra dev
just check                            # format + lint + 테스트 스위트 (Docker 필요)

uv run tubedepth key create --label local   # 키는 이때 한 번만 출력된다
uv run tubedepth serve --port 8080 &        # API (기본 127.0.0.1)
uv run tubedepth work --concurrency 6       # 워커는 별도 프로세스
```

```sh
KEY=ytd_...
curl -s -X POST -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
     -d '{"kind":"video.metadata","target":"https://youtu.be/dQw4w9WgXcQ"}' \
     localhost:8080/v1/jobs                  # 202 + job_id, 캐시에 있으면 200 + 결과
curl -s -H "X-API-Key: $KEY" localhost:8080/v1/jobs/$ID/result
```

**키별 rate limit은 프로세스 안에서만 셉니다.** API 프로세스를 두 개 띄우면 각자
자기 몫을 갖게 됩니다 — 한 대로 운영하는 전제이고, 그게 아니면 이 값은 믿을 수 없습니다.

## 대시보드

```sh
uv run tubedepth serve --port 8080
```

`http://localhost:8080/` 에서 큐 상태, 소스별 건강, 24시간 완료 추이, 그리고 잡·수집물
레코드 브라우저를 볼 수 있다. 페이지 자체는 키가 필요 없고, 브라우저에서 키를 입력하면
그 뒤의 모든 조회에 `X-API-Key` 헤더로 실린다. 키는 `tubedepth key create`로 만든다.

외부 리소스를 하나도 참조하지 않으므로 인터넷이 닿지 않는 사설망에서도 그대로 뜬다.

## 배포

systemd **유저 유닛**이 `deploy/`에 있다. 어느 것도 root가 필요 없고, 권한을 조용히 얻을 수도
없다. 그중 둘이 서비스 본체다:

```sh
mkdir -p ~/.config/tubedepth
echo 'TUBEDEPTH_DATABASE_URL=postgresql+psycopg://tubedepth_runtime:...@host/db' \
  > ~/.config/tubedepth/worker.env
cp ~/.config/tubedepth/worker.env ~/.config/tubedepth/database.env   # api.service가 읽는 파일
chmod 0600 ~/.config/tubedepth/worker.env ~/.config/tubedepth/database.env

cp deploy/tubedepth-api.service deploy/tubedepth-worker.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tubedepth-api tubedepth-worker
loginctl enable-linger $USER    # 로그아웃 후에도 살아있게 — 없으면 재부팅이 크래시처럼 보인다
```

SQLite 대체 경로는 없다: 모든 유닛이 정상 동작하는 `TUBEDEPTH_DATABASE_URL` 없이는 시작을
거부한다. 그 URL이 가리키는 role과 schema를 만드는 것이 `deploy/postgres-bootstrap.sql`이고,
그 뒤의 규정이 `docs/shared-postgres.md`다.

셋째는 선택이고 기본적으로 꺼져 있다. `tubedepth-watch.timer`가 매시간 `tubedepth watch`를
돌려서 watch list 전체를 큐에 넣되 신선도 기간을 강제로 넘겨서, 매 회차가 새 관측을 기록하게
한다. `GET /v1/artifacts`를 캐시가 아니라 미분 가능한 이력으로 만드는 것이 이것이고, 이력은
실시간으로만 쌓이므로 필요해지기 전에 시작해두는 편이 낫다.

```sh
mkdir -p ~/.config/tubedepth
cp deploy/watchlist.example.txt ~/.config/tubedepth/watchlist.txt
$EDITOR ~/.config/tubedepth/watchlist.txt        # 한 줄에 타입 붙은 directive 하나
cp deploy/tubedepth-watch.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tubedepth-watch.timer
```

목록에는 타입이 붙는다 — `video`, `channel`, `search`, `trending` 다음에 타깃 — 그래서 스케줄
하나가 고정된 영상 묶음, 채널 업로드, 트렌드 키워드, 지역 차트를 한꺼번에 수집한다. 넷 중 하나가
아닌 directive는 줄 번호를 짚어 거부한다. 조용히 아무것도 수집하지 않는 오타야말로 watch list가
가장 못 보여주는 실패이기 때문이다. 타이머가 없는 환경 — compose — 에서는
`tubedepth watch --every 3600`이 상주한다.

**목록 크기는 의도해서 정하고, 네 타입의 값이 같지 않다는 것을 안다.** `video` 한 줄은 발화마다
강제 수집 한 건이고, 나머지 전부가 쓰는 것과 같은 per-address 예산에서 나간다. 30줄을 시간당
도는 것은 측정된 처리량의 약 1%다. `channel`·`search`·`trending` 한 줄은 찾아낸 영상마다
`video.metadata` 잡으로 퍼지며, `TUBEDEPTH_LISTING_LIMIT`(기본 100)까지 간다 — **그런 줄 하나가
수집 한 건이 아니라 백 건일 수 있다.** 산수는 `deploy/watchlist.example.txt`에 있다. 그보다 한참
위의 지속 부하에서 이 시스템이 어떻게 움직이는지는 측정된 바 없다.

API와 워커를 나눈 이유는 취향이 아니다. yt-dlp 추출은 블로킹이고 메모리를 쓰므로,
같이 돌리면 댓글 수집 하나가 `GET /v1/jobs/{job_id}`의 p99를 결정하고 yt-dlp 크래시가
API를 같이 죽인다.

API는 기본적으로 **loopback에만** 바인딩한다. 이 프로젝트의 인증은 헤더이고 그건 TLS의
대체물이 아니므로, 외부에 열려면 리버스 프록시를 앞에 둔다.

## 문서

| | |
| --- | --- |
| [`docs/api.ko.md`](docs/api.ko.md) | REST 레퍼런스 — 엔드포인트, 오류 코드, 웹훅 계약 |
| [`docs/status.md`](docs/status.md) | 현재 상태와 그 뒤의 결정들 |
| [`docs/troubleshooting.md`](docs/troubleshooting.md) | 이미 누군가의 오후를 잡아먹은 에러들. 읽지 말고 grep |
| [`docs/shared-postgres.md`](docs/shared-postgres.md) | 다른 스크래퍼들과 공유하는 PostgreSQL 인스턴스의 규약 |
| [`CHANGELOG.ko.md`](CHANGELOG.ko.md) | 릴리스별 변경 내역 |
| [`docs/releasing.md`](docs/releasing.md) | 릴리스를 내는 절차 |
| [`AGENTS.ko.md`](AGENTS.ko.md) | 이 저장소에서 작업하는 법 |

`README.md`·`docs/api.md`·`CHANGELOG.md`·`AGENTS.md`가 정본이고, 옆의 `.ko.md`가 번역이다.
나머지 문서는 한국어로만 있다.

## 버전 관리

패키지 버전은 `src/tubedepth/__init__.py` 한 곳에만 쓰여 있고 `pyproject.toml`이 그것을
읽는다. `tubedepth version`, `GET /healthz`, OpenAPI 문서가 전부 같은 값을 보고한다.

`/v1`은 **HTTP 계약의 버전**이고 별도로 움직인다 — [`docs/api.ko.md`](docs/api.ko.md)에
대고 작성한 클라이언트가 깨질 때만 올라간다. 패키지 버전은 클라이언트가 눈치채지 못할
이유로도 움직인다.

릴리스는 [semver](https://semver.org)를 따르고 [`CHANGELOG.ko.md`](CHANGELOG.ko.md)에
기록되며 `master`에 `vX.Y.Z`로 태그된다. 절차는 [`docs/releasing.md`](docs/releasing.md).
1.0 이전에는 minor 상향에서 수집 payload 모양이 바뀔 수 있고, 저장된 artifact는 각 소스의
`schema_version`으로 키가 잡힌다.

## 상태

계획서(M0–M9)의 모든 단계가 구현되었고, 계획에 없던 대시보드가 추가됐다.
계획에 있었으나 **의도적으로 만들지 않은 것 둘**은 이유와 함께 기록되어 있다:
`video.dislikes`는 제거했고(수치를 아무도 판정할 수 없어서), `channel.profile`은 취소했다
(그 내용이 `channel.about`이 이미 하는 호출의 응답에 들어 있어서).

무엇이 검증되었고 무엇이 아닌지는 [`docs/status.md`](docs/status.md)에 있다.
그 문서와 [`docs/plan.md`](docs/plan.md)가 어긋나면 status.md가 맞다 — plan.md는 기록이지
지시가 아니다.

## Honest limits

이 절은 이 프로젝트가 **할 수 없는 것**과 **아직 확인하지 않은 것**을 적는다.

**원천적으로 불가능한 것**

- **정확한 구독자 수.** YouTube가 반올림 값만 공개한다 — InnerTube 응답 자체가 `"4.53M subscribers"`
  문자열이고 yt-dlp의 정수는 그것을 파싱한 값이다. Data API와 같은 한계이고 스크래핑으로도 우회할 수 없어서,
  **정확한 값을 약속하는 필드를 아예 두지 않았다.** `subscriber_count_approximate`와 원본 문자열만 준다.
- **트렌딩(인기 급상승).** YouTube가 피드를 폐지했다(`/feed/trending`은 홈으로 리다이렉트된다).
  빠뜨린 게 아니라 없는 것이므로 비슷한 것으로 흉내내지 않는다.

**깨지기 쉬운 것**

- **관련 영상·채널 About·커뮤니티 게시물은 InnerTube 렌더러 파싱에 의존한다.** 이름이 예고 없이 바뀐다 —
  `compactVideoRenderer`가 이미 `lockupViewModel`로 바뀌었다. `tests/fixtures/innertube/`의 날짜가 각 표면이
  마지막으로 동작한 시점이다. 응답의 `degradations`에 `parse_mismatch`가 있으면 그중 하나가 깨진 것이다.
  CI의 fixture 회귀는 **우리 코드가 퇴행하지 않았음**을 증명할 뿐, YouTube가 지금 무엇을 보내는지에 대해서는
  아무것도 증명하지 못한다. 그건 `just contract`가 한다.
- **차단될 수 있다.** 요청량에 따라 로그인·PO token을 요구받을 수 있다. 고장 났을 때 첫 수순은
  `just update-ytdlp`이고, 두 번째는 yt-dlp 이슈 트래커이며, 이 코드 디버깅은 세 번째다.

**프록시에 대해 — 기대와 반대다**

- **ProtonVPN으로 YouTube 트래픽을 보내지 마라.** yt-dlp의 공식 권고 자체가 "VPN을 끄고 레지덴셜 회선을
  쓰라"이다. YouTube의 봇 검사는 데이터센터 주소 대역을 표적으로 하고, 상용 VPN exit은 전부 그 대역이다.
  이 프로젝트를 개발한 머신의 **직결 회선은 가정용 IP라 현재 잘 동작하며, VPN exit은 아마 그렇지 않을 것이다.**
  이것을 켜는 설정은 없다. 켤 것이 아직 만들어지지 않았기 때문이다. 만들어지면, 그것은 고침이 아니라
  *측정*으로 다뤄야 한다는 것이 요점이다.
- **프록시 풀에는 현재 측정된 근거가 없다.** 원래의 정량적 근거는 Return YouTube Dislike의 문서화된
  일일 한도(10,000/일 → exit 10개면 시간당 약 4,000건)였는데, **그 소스를 제거하면서 근거도 함께 사라졌다.**
  남은 서드파티는 SponsorBlock 하나이고 그 한도는 비공개다. YouTube 쪽은 위 항목 그대로 VPN이 도움이 안 된다.
  즉 지금 exit을 늘려서 확실히 나아지는 것은 **아무것도 측정되지 않았다.** 풀을 만들기 전에 그것부터 재라.
- **YouTube 처리량을 실제로 올리는 것은 레지덴셜/모바일 프록시뿐**이며 대략 $5–15/GB다.
  `ProxiedEgress`가 그 자리다.
- **ProtonVPN 동시 접속 수를 확인하라.** wireproxy 프로세스 하나가 한 자리를 먹으므로, 풀이 당신의 휴대폰·
  노트북과 같은 할당량을 놓고 경쟁한다. 무료 플랜은 풀 용도로 부적합하다.

**처리량 — 종류마다 두 자릿수 차이가 난다**

| 종류 | 시간당 수천 건? |
| --- | --- |
| SponsorBlock · 캐시 히트 | 가능. 캐시 히트가 유일하게 우리가 완전히 통제하는 축이다 |
| 영상 메타 · 관련영상 · 검색 | **지속 실측 ~3,100/시간**(474건, 실패 0, 430초, 동시 8). 40건 버스트는 8,417/시간까지 가는데, 그건 버스트가 재는 값이다 |
| 댓글 전량 수집 | **불가능.** 1,000댓글 = 50+ 요청 = 1–3분. 게다가 그 요청들이 메타 수집이 써야 할 IP 예산을 갉아먹는다 |

**법적 위치.** YouTube 이용약관은 공개 API 외의 자동화 접근을 금지한다. 데이터가 공개적으로 보인다는 사실이나
요청률이 낮다는 사실이 이를 허용됨으로 바꾸지 않는다. 사설망에서 소수 클라이언트가 쓰는 것을 전제로 하며
공개 서비스로 제공하지 않는다. 수집물(자막·댓글)은 제3자 저작물이고, 댓글 작성자 정보는 저장되는 순간
개인정보다. SponsorBlock 데이터는 CC BY-NC-SA 4.0이라 재배포에 출처 표시와 비상업 조건이 따른다.
**싫어요 수는 제공하지 않는다.** 유튜브가 2021년 말 비공개로 돌린 뒤로 원본이 존재하지 않으며, 재구성
추정치를 제공하던 소스는 의도적으로 제거했다 — 이유는 `docs/status.md`에 있다.

**검증된 것과 아닌 것.** 계획 단계에서 이 머신에서 직접 확인: yt-dlp 78키, 자막 json3 취득, 댓글 20건 6.7초,
SponsorBlock 200/404 양쪽, InnerTube `/next`·`/browse` 도달,
PostgreSQL 연결 확인(그 당시 backend였던 SQLite는 3.46.1), wireproxy 1.1.3 가용,
메타 추출 병렬 4건 3.11초.

그 뒤 지속 부하에서: 474건, 실패 0, AIMD 창은 상한에 붙었고 격리 연속 카운트는 0 — 이 속도에서
컨트롤러는 진동하지 않고 수렴하며, 봇 검사에는 닿지 않았다. **여전히 확인하지 않음**: 봇 검사 임계가
실제로 어디인지, PO token 조건, **실제 VPN egress를 통한 요청**(config가 아직 없어 한 번도 프록시로
나가본 적 없다), 장시간 다중 워커 운용. 미검증으로 남은 것들은 여기 서술로만 두지 않고
[`verification`](https://github.com/slopindustries/yt-scrapper/issues?q=is%3Aissue+is%3Aopen+label%3Averification)
라벨의 이슈로 추적한다.

## 라이선스

MIT. [`LICENSE`](LICENSE) 참조.
