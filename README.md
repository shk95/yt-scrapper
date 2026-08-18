# tubedepth

YouTube Data API가 주지 않는 영상·채널 데이터를 수집하는 자체 호스팅 API.

챕터, "가장 많이 본 구간" 히트맵, 태그, 정확한 업로드 시각, 자막 본문, 댓글 전량, 싫어요 추정치,
SponsorBlock 구간, 관련 영상, 채널 About, 커뮤니티 게시물. 클라이언트가 API 키로 **작업(job)을 등록**하고
정규화된 JSON을 받아간다.

```sh
curl -H "X-API-Key: $KEY" -X POST localhost:8080/v1/videos/dQw4w9WgXcQ/metadata
# 202 → 폴링 → chapters, most_replayed(100버킷), tags, published_at …
```

## 왜 필요한가

Data API v3는 공개 영상에 대해서도 많은 것을 감춘다. `snippet.tags`는 영상 소유자에게만 반환되고,
자막 본문은 소유자 OAuth 없이 못 받으며, 챕터·히트맵·싫어요·관련 영상·커뮤니티 게시물은 필드 자체가 없다.
댓글은 있지만 쿼터 소모가 커서 대량 수집에 못 쓴다.

## 시작하기

```sh
git config core.hooksPath .githooks   # 클론은 훅이 꺼진 상태로 온다
tool/doctor.sh                        # 툴체인·SQLite·훅 확인
uv sync --extra dev
just check                            # format + lint + 오프라인 테스트

uv run tubedepth key create --label local   # 키는 이때 한 번만 출력된다
uv run tubedepth worker &
uv run tubedepth serve --port 8080
```

작업 방식은 [`AGENTS.md`](AGENTS.md)에, 현재 상태는 [`docs/status.md`](docs/status.md)에 있다.

## 마일스톤

| # | 내용 | 상태 |
| --- | --- | --- |
| M0 | 저장소 스캐폴딩, 훅, CI, doctor | 진행 중 |
| M1 | 도메인 코어 + egress 골격 (네트워크 0) | |
| M2 | 첫 yt-dlp 소스 — 영상 메타 | |
| M3 | HTTP API + API 키 인증 | |
| M4 | 자막 · 싫어요 · SponsorBlock | |
| M4.5 | egress 풀 (wireproxy) + 적응 라우팅 | |
| M5 | 댓글 수집 | |
| M6 | 채널 · 재생목록 · 검색 | |
| M7 | InnerTube — 관련영상 · About · 커뮤니티 | |
| M8 | 합성 · 웹훅 · 보존 · systemd | |
| M9 | 확장 지점 증명 (라이브 채팅 추가) | |

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
  `TUBEDEPTH_EGRESS_ALLOW_VPN_FOR_YOUTUBE`의 기본값이 `0`인 이유다. 켜는 것은 고침이 아니라 *측정*이고,
  안 통하면 컨트롤러가 물어보기 전에 이미 강등해 놨을 것이다.
- **VPN 풀의 진짜 용도는 RYD다.** Return YouTube Dislike는 **클라이언트당 100 req/분, 10,000 req/일**을
  문서화한다. 일일 한도가 벽이라 한 주소가 시간당 약 400건, exit 10개면 약 4,000건이다. 그게 프록시 풀의
  근거 전부이고 YouTube와는 무관하다. SponsorBlock에도 같은 논리가 (한도가 비공개라 덜 정밀하게) 적용된다.
- **YouTube 처리량을 실제로 올리는 것은 레지덴셜/모바일 프록시뿐**이며 대략 $5–15/GB다.
  `ExternalProxyEgress`가 그걸 코드 변경이 아니라 설정 한 줄로 만들어 둔다.
- **ProtonVPN 동시 접속 수를 확인하라.** wireproxy 프로세스 하나가 한 자리를 먹으므로, 풀이 당신의 휴대폰·
  노트북과 같은 할당량을 놓고 경쟁한다. 무료 플랜은 풀 용도로 부적합하다.

**처리량 — 종류마다 두 자릿수 차이가 난다**

| 종류 | 시간당 수천 건? |
| --- | --- |
| 싫어요 · SponsorBlock · 캐시 히트 | 가능. 풀이 실제로 도움 되는 유일한 구간 |
| 영상 메타 · 관련영상 · 검색 | 아마도 (직결 IP 하나로 900–1,800/시간이 계획용 밴드) |
| 댓글 전량 수집 | **불가능.** 1,000댓글 = 50+ 요청 = 1–3분. 게다가 그 요청들이 메타 수집이 써야 할 IP 예산을 갉아먹는다 |

**법적 위치.** YouTube 이용약관은 공개 API 외의 자동화 접근을 금지한다. 데이터가 공개적으로 보인다는 사실이나
요청률이 낮다는 사실이 이를 허용됨으로 바꾸지 않는다. 사설망에서 소수 클라이언트가 쓰는 것을 전제로 하며
공개 서비스로 제공하지 않는다. 수집물(자막·댓글)은 제3자 저작물이고, 댓글 작성자 정보는 저장되는 순간
개인정보다. SponsorBlock 데이터는 CC BY-NC-SA 4.0이라 재배포에 출처 표시와 비상업 조건이 따른다.
RYD 수치는 YouTube의 싫어요 수가 아니라 **추정치**이며 응답에서 항상 그렇게 라벨링된다.

**검증된 것과 아닌 것.** 계획 단계에서 이 머신에서 직접 확인: yt-dlp 78키, 자막 json3 취득, 댓글 20건 6.7초,
RYD의 브라우저 UA 요구와 일일 한도 문서, SponsorBlock 200/404 양쪽, InnerTube `/next`·`/browse` 도달,
SQLite 3.46.1, wireproxy 1.1.3 가용, 메타 추출 병렬 4건 3.11초.
**아직 확인하지 않음**: 지속 부하에서의 봇 검사 임계, PO token 조건, **실제 VPN egress를 통한 요청**
(config가 아직 없어 한 번도 프록시로 나가본 적 없다), AIMD의 실부하 수렴 거동, 장시간 다중 워커 운용.

## 라이선스

MIT. [`LICENSE`](LICENSE) 참조.
