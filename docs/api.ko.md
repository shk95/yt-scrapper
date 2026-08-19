# REST API 레퍼런스

이 서비스가 서빙하는 전부, 각 응답이 무엇이며 무엇을 뜻하는지.
정본은 [`api.md`](api.md)이며, 두 문서가 어긋나면 그쪽이 맞다.

여기서 설명하는 버전은 패키지가 보고하는 버전이다 — `GET /healthz`가 돌려주고,
변경 내역은 [`../CHANGELOG.ko.md`](../CHANGELOG.ko.md)에 있다.

## 공통 사항

| | |
| --- | --- |
| Base URL | 기본 `http://127.0.0.1:8080` — API는 loopback에 바인딩한다 |
| 인증 | `/v1` 아래 전부에 `X-API-Key: ytd_...` |
| 요청 본문 | JSON, `Content-Type: application/json` |
| 응답 본문 | JSON, 항상 객체 |
| 오류 | `{"error": {"code": "...", "message": "..."}}` |
| 시각 | RFC 3339, UTC |

`/v1`은 **HTTP 계약의 버전**이지 패키지 버전이 아니다. 이 문서에 대고 작성한 클라이언트가
깨질 변경이면 `/v2`가 되고, 패키지 버전은 클라이언트가 눈치채지 못할 이유로도 움직인다.

**여기에는 TLS가 없고 키는 헤더로 다닌다.** 앞에 리버스 프록시 없이 loopback 밖에
바인딩하면 그 키가 평문으로 회선에 실린다.

`/docs`에 대화형 OpenAPI 문서가 있다. 이 문서가 손으로 설명하는 것과 같은 라우트 정의에서
생성된다.

## 인증

키는 서비스를 돌리는 머신에서 만든다.

```sh
uv run tubedepth key create --label ingest
# ytd_4f3a9c21_9d1c...  ← 이때 한 번만 출력되고, 저장은 SHA-256 해시로만 된다
```

모든 `/v1` 요청에 실어 보낸다.

```sh
curl -s -H "X-API-Key: $KEY" localhost:8080/v1/sources
```

키가 없거나, 형식이 틀렸거나, 모르는 키이거나, 폐기된 키인 네 경우 모두 같은 401
(`unauthenticated`)이다. 이 엔드포인트를 넷 중 무엇인지 알아내는 도구로 쓸 수 없게 하기
위해서다. `/healthz`와 `/` 대시보드는 키를 받지 않는다 — 자격증명을 쥔 사람이 생기기 전에
배포 상태를 진단할 수 있어야 한다.

키마다 할당량이 있고 기본값은 분당 60요청이다. 초과하면 429 (`rate_limited`).

**할당량은 한 프로세스 안에서만 센다.** API 프로세스가 둘이면 같은 키가 각각에서 할당량을
온전히 받는다. 이 프로젝트가 전제하는 단일 인스턴스 배포에서는 정직한 값이고, 그 밖에서는
이 숫자에 의미가 없다.

## 잡(job)이란

추출이 느리기 때문에 수집은 비동기다 — 댓글 전량 수집은 몇 분씩 걸린다. 클라이언트는 잡을
등록하고 id를 받아, 폴링하거나 콜백을 받는다.

```
POST /v1/jobs  ─┬─→ 200 + 결과                 이미 신선한 artifact가 있었다
                └─→ 202 + job_id  →  queued → running →  succeeded → GET .../result
                                                      ├→ failed     error_code가 이유를 말한다
                                                      └→ cancelled  실행되지 않았다
```

200은 클라이언트가 무시해도 되는 최적화 세부사항이 아니다. 신선도 기간 안에 같은 것을 두 번
요청하면 그게 정상 응답이고, 폴링 한 번을 아낀다. 그래도 수집시키려면 `"refresh": true`.

그래서 `"refresh": true`를 실은 제출은 항상 202이고 200이 되지 않는다 — 받아들일 캐시된 답이
없기 때문이다. 이 플래그는 요청에서 소비되는 대신 잡에 실려 가므로, 워커가 그 잡에 도달했을 때
다시 수집하고, 그 잡이 재시도되어도 여전히 강제 수집이다. **`GET /v1/artifacts`가 캐시가 아니라
이력인 것은 이 때문이다:** 강제 수집은 새 관측을 기록하고, 조용히 캐시로 답한 수집은 기록하지
않는다.

## kind

잡이 요청할 수 있는 것. `GET /v1/sources`가 레지스트리에서 같은 표를 돌려주므로 그쪽은
낡을 수 없고, 여기 사본은 각각이 무엇을 위한 것인지를 말한다.

<!-- kinds:start -->

| kind | target | lane | cost | 신선도 | 수집하는 것 |
| --- | --- | --- | --- | --- | --- |
| `video.metadata` | video | youtube | standard | 6시간 | 챕터, 100버킷 히트맵, 태그, 정확한 업로드 시각, 라이선스, 자막 트랙 목록 |
| `video.transcript` | video | youtube | standard | 30일 | 영상 자체 언어의 자막 본문, 사람이 쓴 것 우선 |
| `video.comments` | video | youtube | expensive | 24시간 | 댓글 전량, `parent_id` 스레딩, 고정·하트·인증 플래그 |
| `video.sponsor_segments` | video | sponsorblock | cheap | 6시간 | SponsorBlock 구간 (커뮤니티 데이터, CC BY-NC-SA 4.0) |
| `video.related` | video | youtube | cheap | 1시간 | 관련 영상 목록 |
| `video.bundle` | video | youtube | expensive | 6시간 | 메타·자막·스폰서 구간·관련 영상을 한 잡으로. 빠진 것은 `degradations`에 이름이 남는다. 댓글은 의도적으로 제외 — 몇 분짜리 수집을 접으면 모든 bundle이 시스템에서 가장 비싼 잡이 된다 |
| `channel.about` | channel | youtube | cheap | 7일 | 가입일, 국가, 링크, **정확한 총 조회수**, 설명, 태그, 아바타 |
| `channel.community` | channel | youtube | cheap | 6시간 | 커뮤니티 게시물 |
| `channel.videos` | channel | youtube | cheap | 6시간 | 채널 업로드 목록 |
| `playlist.items` | playlist | youtube | cheap | 6시간 | 재생목록 항목 |
| `search.videos` | query | youtube | cheap | 6시간 | 검색 결과 |
| `trending.videos` | region | youtube_data_api | cheap | 15분 | YouTube 자신이 인기라고 부르는 것을, 그 순서대로. 관측이 아니라 순위를 보고하는 유일한 kind이고, per-address 예산 대신 Google API 쿼터를 쓰는 유일한 kind다 |

<!-- kinds:end -->

**채널을 통째로 열거하려면 그 채널의 업로드 재생목록에 `playlist.items`를 쓴다** — 채널 id의
`UC`를 `UU`로 바꾼 것이다. `channel.videos`는 `/videos` 탭을 읽는데, 그 탭에는 Shorts도 지난
라이브 스트림도 없다. 상한을 아무리 올려도 그렇다. 697개짜리 채널 하나에서 측정, 2026-08-20:

| | 항목 | 요청 |
| --- | --- | --- |
| `UU…`에 대한 `playlist.items` | **698** | **8** |
| `channel.videos` | 474 | 16 |

업로드 재생목록이 더 넓으면서 동시에 더 싸다 — 한 번에 100개씩 넘기고, 탭은 30개씩 넘긴다.
추가되는 224개는 Shorts 216개, 지난 라이브 3개, 그리고 제목과 조회수를 달고 그리드에 나타나지만
전혀 볼 수 없는 5개다. 그 다섯은 `not_found`로 실패하는 잡이 된다 — 평평한 목록에서는 살아있는
영상과 구별할 방법이 없기 때문이다.

상한에 유의: `TUBEDEPTH_LISTING_LIMIT`은 배포 전역이라, 기본값 100이면 698개 중 100개가 나온다.

`target`은 등록 요청의 `target` 필드가 가리켜야 하는 것이다. 영상은 id·`youtu.be` 링크·
`watch?v=` URL을, 채널은 id·`@handle`·채널 URL을 받는다. 정규화는 잡이 기록되기 전에
일어나므로 원장에는 정규형 하나만 남는다.

`lane`은 요청이 어느 업스트림으로 나가는지, `cost`는 큐가 그것을 어떻게 값매기는지다.
댓글 수집이 나머지를 굶기지 못하게 하려고 둘 다 있다.

---

## `GET /healthz`

인증 없음. 서비스가 살아 있는지, 어떤 버전인지, 큐와 각 소스가 무엇을 하고 있었는지.

```sh
curl -s localhost:8080/healthz
```

```json
{
  "status": "ok",
  "version": "0.1.0",
  "queued": 3,
  "running": 1,
  "sources": [
    {
      "kind": "video.metadata",
      "status": "ok",
      "consecutive_failures": 0,
      "last_success_at": "2026-08-19T09:12:44Z",
      "last_failure_at": null,
      "last_error_code": null,
      "last_error_message": null
    }
  ]
}
```

`lanes`는 rate controller가 각 경로에 현재 허용하는 것이다. 워커가 기록한다 — 컨트롤러의
상태는 워커 메모리의 dict이고 프로세스와 함께 죽기 때문이다.

```json
{
  "lanes": [
    {
      "egress": "direct",
      "lane": "youtube",
      "window": 3.5,
      "in_flight": 1,
      "quarantine_streak": 0,
      "quarantined_until": null,
      "observed_at": "2026-08-20T09:12:44Z"
    }
  ]
}
```

`window`는 설정이 아니라 **측정된** 상한이다. 업스트림이 거부하면 절반이 되고 성공하면 다시
자란다. 그래서 1보다 한참 작은 window가 큐가 느리게 빠지는 이유를 설명하는 숫자다.
`quarantined_until`은 경로가 열려 있으면 null이고, 값이 있으면 그때까지 그 경로로 아무것도
시도하지 않는다는 뜻이다 — 밖에서 보면 빈 큐와 구분되지 않으므로, 누군가 말해주지 않으면 모른다.

개별 소스가 성치 않아도 `status`는 `"ok"`로 남는다. 이 엔드포인트는 프로세스를 재시작하는
것들이 읽고, 파서 하나가 깨진 것은 나머지 열 종류가 여전히 수집 중인 API를 재기동할 이유가
아니기 때문이다. 나쁜 소식은 사람이 읽는 `sources`에 실린다 — 그리고 `last_error_code`가 아니라
`last_error_message`에 실린다. 코드는 `parse_mismatch`라고 말하고, 메시지는 더 이상 맞지 않는
렌더러의 이름을 말한다. 소스가 깨졌다는 것을 아는 것과 무엇을 고쳐야 하는지 아는 것의 차이다.

소스의 `status`는 고치는 방법이 서로 다른 원인들을 구분한다.

| 값 | 뜻 | 무엇이 고치나 |
| --- | --- | --- |
| `ok` | 최근 성공 | — |
| `degraded` | 최근 실패 1건 | 보통 아무것도. 지켜본다 |
| `broken` | 우리 파서가 안 맞기 시작 | 코드 변경 — `degradations`의 `parse_mismatch` 확인 |
| `blocked` | 주소가 거부당하고 있음 | 다른 egress, 또는 대기 |
| `stale` | 최근에 아무도 돌리지 않음 | 잡을 하나 돌린다 |
| `unknown` | 이 인스턴스에서 시도된 적 없음 | 잡을 하나 돌린다 |

`unknown`과 `stale`은 일부러 초록이 아니다. 아무도 돌리지 않은 것을 건강하다고 표시하는
대시보드는 모른다고 인정하는 대시보드보다 나쁘다.

---

## `GET /v1/control`, `PATCH /v1/control`

워커가 잡을 집고 있는지, 그리고 집지 말라고 말하는 유일한 방법.

```sh
curl -s -X PATCH -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
     -d '{"paused": true, "reason": "쿼터 지켜보는 중"}' \
     localhost:8080/v1/control
```

```json
{ "paused": true, "reason": "쿼터 지켜보는 중", "changed_at": "2026-08-20T09:12:44Z" }
```

**이것은 워커에 손을 뻗지 않는다.** API와 워커는 의도적으로 별개 프로세스다 — yt-dlp 크래시가
API를 같이 죽이면 안 되기 때문이다 — 그래서 여기서 무언가를 직접 멈출 수는 없다. 워커가 매
drain 시작에 읽는 행을 쓸 뿐이고, `tubedepth work`는 drain하고 종료하며 유닛이 10초마다
재시작하므로, 일시정지는 대략 그 안에 효력이 생긴다.

**이미 실행 중인 잡은 끝까지 간다.** 일시정지는 "새로 집지 않는다"이지 취소가 아니며, 진행 중인
추출은 끝날 때까지 계속 요청을 쓴다. 그것을 멈추려면 그 잡을 취소한다.

큐에 있던 잡은 큐에 그대로 남고 들어오는 길에 실패 처리되는 것도 없으므로, 재개가 되돌리기의
전부다. `reason`은 선택이지만 채울 값어치가 있다 — 한 시간 뒤에 아무도 설명하지 못하는
일시정지는 아무도 못 푸는 일시정지다.

행이 아직 없다는 것은 아무도 이것을 멈춘 적 없다는 뜻이고, 오류가 아니라 "돌고 있음"으로 보고된다.

---

## `GET /v1/sources`

이 빌드가 수집할 수 있는 것. 레지스트리에서 읽으므로 코드에 추가된 소스는 아무도 목록을
편집하지 않아도 여기 나타난다.

```sh
curl -s -H "X-API-Key: $KEY" localhost:8080/v1/sources
```

```json
{
  "video.metadata": {
    "kind": "video.metadata",
    "target": "video",
    "lane": "youtube",
    "cost": "standard",
    "freshness_seconds": 21600,
    "cache_parameters": {}
  }
}
```

---

## `POST /v1/jobs`

데이터를 요청한다. 202와 잡을 주거나, 이미 신선한 결과가 있으면 200과 결과를 준다.

| 필드 | 타입 | 기본값 | |
| --- | --- | --- | --- |
| `kind` | string | 필수 | 위 kind 중 하나 |
| `target` | string | 필수 | id, handle, URL, 또는 검색어 |
| `refresh` | bool | `false` | 신선한 artifact가 있어도 수집한다 |
| `webhook_url` | URL | `null` | 잡이 종료 상태에 도달하면 한 번 호출된다 |

```sh
curl -s -X POST -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
     -d '{"kind":"video.metadata","target":"https://youtu.be/dQw4w9WgXcQ"}' \
     localhost:8080/v1/jobs
```

**202 Accepted** — 큐에 들어갔다. `Location` 헤더에 잡의 URL이 실린다.

```json
{
  "job_id": "j_2f7c1d9a",
  "kind": "video.metadata",
  "target": "dQw4w9WgXcQ",
  "state": "queued",
  "attempt_count": 0
}
```

**200 OK** — 신선한 artifact가 있었고, 본문은 잡이 아니라 수집된 데이터 자체다.
둘은 모양이 아니라 **상태 코드로** 구분한다.

분기할 가치가 있는 실패: 모르는 `kind`나 해석되지 않는 `target`은 422 `invalid_request`.
형식이 틀린 `webhook_url`은 저장되지 않고 여기서 거부된다 — 잘못된 URL을 저장하는 것은
이후 모든 전송 시도에서 영원히 실패하는 배달을 만드는 일이기 때문이다.

---

## `POST /v1/jobs/batch`

kind 하나, 타깃 여럿, 요청 하나. 202로 답한다.

```sh
curl -s -X POST -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
     -d '{"kind":"video.metadata","targets":["dQw4w9WgXcQ","nfgdJyL-Jmg"]}' \
     localhost:8080/v1/jobs/batch
```

```json
{
  "queued": [{ "job_id": "j_2f7c1d9a", "kind": "video.metadata", "target": "nfgdJyL-Jmg", "state": "queued", "attempt_count": 0 }],
  "held": [{ "target": "dQw4w9WgXcQ", "digest": "b9f4c0e2..." }]
}
```

**편의 기능이 아니다.** 키 하나는 분당 60요청이므로, 영상 100개짜리 스윕을 한 건씩 제출하면
절반도 못 가서 rate limit에 걸린다. 스윕을 *표현할 수 있는* API와 *실행할 수 있는* API의 차이다.

**전부 아니면 전무.** 한 줄이라도 큐에 넣기 전에 모든 타깃을 정규화하므로, 잘못된 id 하나면
나머지 99개를 넣고 202로 답하는 대신 배치 전체를 422로 거부한다. 부분 스윕은 가능한 결과 중
가장 나쁘다 — 호출자는 실행됐다고 믿고, 빠진 부분은 나중에 아무도 안 찾는 부재로 드러난다.

타깃은 최대 500개이고 그 이상은 422. `POST /v1/jobs`와 달리 payload를 절대 돌려주지 않는다.
이미 보유한 타깃은 `digest`로 이름만 알려주며, 그것이 `GET /v1/artifacts/{digest}`가 받는 값이다.
본문 100개를 돌려주는 것은 제출을 대량 다운로드로 만드는 일이다.

---

## `GET /v1/jobs/{job_id}`

잡 하나의 현재 상태.

```sh
curl -s -H "X-API-Key: $KEY" localhost:8080/v1/jobs/$JOB
```

```json
{
  "job_id": "j_2f7c1d9a",
  "kind": "video.metadata",
  "target": "dQw4w9WgXcQ",
  "state": "succeeded",
  "attempt_count": 1,
  "error_code": null,
  "error_message": null,
  "payload_bytes": 26417,
  "created_at": "2026-08-19T09:12:31Z",
  "finished_at": "2026-08-19T09:12:44Z"
}
```

| state | |
| --- | --- |
| `queued` | 워커를 기다리는 중 |
| `running` | 워커가 리스를 쥐고 있고, 작업하는 동안 갱신된다 |
| `succeeded` | 결과가 `/v1/jobs/{job_id}/result`에 있다 |
| `failed` | `error_code`와 `error_message`가 이유를 말한다. 재시도는 이미 소진됐다 |
| `cancelled` | 요청됐다가 취소됨. 실행되지 않았다 |

이 인스턴스에 없는 id는 404 `not_found`.

---

## `GET /v1/jobs/{job_id}/result`

수집된 데이터 원본 — 저장된 payload 그대로이며, 다시 인코딩한 것이 아니다.

```sh
curl -s -H "X-API-Key: $KEY" localhost:8080/v1/jobs/$JOB/result
```

잡은 있으나 아직 끝나지 않았으면 **409 `conflict`**. 404가 아닌 이유는, "기다려라"와
"그런 것은 없다"의 차이가 재시도하는 클라이언트와 포기하는 클라이언트의 차이이기 때문이다.

잡은 끝났으나 그 결과가 retention 기간을 지나 사라졌으면 **404 `not_found`**. 이것은 오류가
아니라 오래된 잡의 정상적인 최종 상태다 — retention은 artifact를 지우고 잡 원장은 건드리지
않으므로, 수집한 것이 사라진 뒤에도 잡은 자기가 무엇을 했는지 계속 답할 수 있다. **결과는
영구적이지 않고, 잡 원장이 영구적이다.** retention 기간 너머까지 데이터가 필요한 클라이언트는
가져올 때 자기 쪽에 저장해야 한다.

모든 payload에는 `degradations` 목록이 있다. 깨끗한 수집에서는 비어 있고, 그렇지 않으면
얻지 못한 것의 이름이 들어간다 — 댓글이 꺼진 영상의 `video.bundle`이라든가, 렌더러가 더
이상 맞지 않는 표면(`parse_mismatch`). **빈 목록은 약속이고, 빠진 것에는 항상 이름이 있다.**
요청받은 것보다 조용히 적게 돌려주는 것이 이 프로젝트가 구조적으로 불가능하게 만들려는
실패다.

---

## `DELETE /v1/jobs/{job_id}`

더 이상 원하지 않는 잡을 멈춘다. 행은 남는다 — 무엇을 멈추라고 들었는지 잊는 큐는 왜
아무것도 오지 않았는지 답할 수 없다.

```sh
curl -s -X DELETE -H "X-API-Key: $KEY" localhost:8080/v1/jobs/$JOB
```

**돌아온 `state`를 읽어라. 그게 답이다.**

- `cancelled` — 실행된 적 없고 앞으로도 없다.
- `running` — 요청은 기록됐다. 이 잡은 재시도되지 않고 결과도 돌려주지 않지만, 이미 진행
  중인 추출은 끝날 때까지 계속 요청을 쓴다. 여기서 `cancelled`라고 답하는 것은 멈추지 않은
  비용을 멈췄다고 알리는 일이다.

---

## `GET /v1/jobs`

잡 원장, 최신순.

| 파라미터 | |
| --- | --- |
| `state` | `queued`, `running`, `succeeded`, `failed`, `cancelled` |
| `kind` | 위 kind 중 하나 |
| `target` | 저장된 정규형 target |
| `since` / `until` | RFC 3339, `created_at` 기준 |
| `limit` | 기본 50, 최대 500 |
| `cursor` | 이전 페이지에서 받은 값 |

```sh
curl -s -H "X-API-Key: $KEY" 'localhost:8080/v1/jobs?state=failed&limit=20'
```

```json
{
  "jobs": [{ "job_id": "j_2f7c1d9a", "state": "failed", "error_code": "upstream_error" }],
  "cursor": "MjAyNi0wOC0xOVQwOToxMjozMSswMDowMHxqXzJmN2MxZDlh"
}
```

---

`cache_parameters`는 kind와 target 외에 그 소스의 답을 다른 답으로 만드는 것이다 — 리스팅의
상한, 댓글 수집의 정렬과 상한, 자막의 언어 우선순위. **응답한 프로세스에서 실제로 적용 중인
값이다.** `tubedepth serve`와 `tubedepth work`는 각각 별개 프로세스에서 환경변수를 한 번씩
읽고, 둘이 어긋나면 API가 워커가 기록하는 것과 다른 캐시 키를 계산한다 — 워커가 쓰는 것과는
더 이상 맞지 않으면서, 변경 이전의 행과는 계속 맞는다. 둘 사이에서 이 라우트를 비교하는 것이
그것을 잡는 방법이다.

## `GET /v1/artifacts`

요청된 것이 아니라 **실제로 수집된 것**. `kind`, `target`, `since`, `until`, `limit`,
`cursor`를 받고 `fetched_at`으로 거른다.

```sh
curl -s -H "X-API-Key: $KEY" 'localhost:8080/v1/artifacts?target=dQw4w9WgXcQ'
```

```json
{
  "artifacts": [
    {
      "kind": "video.metadata",
      "target": "dQw4w9WgXcQ",
      "schema_version": "1",
      "digest": "b9f4c0e2...",
      "byte_count": 26417,
      "fetched_at": "2026-08-19T09:12:44Z",
      "fresh_until": "2026-08-19T15:12:44Z"
    }
  ],
  "cursor": null
}
```

`schema_version`은 그 kind의 normalizer 중 어느 버전이 이 바이트를 썼는지다. 컬럼이 생기기 전에
수집된 것은 `null`이다 — fingerprint가 버전을 품고 있지만 SHA-256이라 행에서 되돌릴 수 없다.
**`schema_version`이 다른 두 관측은 직접 비교할 수 없다**: bump는 모양이 바뀌었다는 뜻이고, 한쪽에
있는 필드를 다른 쪽은 애초에 수집하지 않았을 수 있다.

artifact 테이블은 덮어쓰지 않고 덧붙이므로, `target`으로 거르면 영상 하나의 이력이 나온다 —
수치가 어떻게 움직였는지. 잡 원장은 그걸 답할 수 없고, 그것을 보관하는 것이 이 테이블이다.

`digest`는 저장된 payload의 내용 주소다. 같은 바이트를 만든 두 수집은 하나를 공유한다.
`fetched_at`이 다른데 digest가 같다면 그 사이에 아무것도 바뀌지 않았다는 뜻이다.

---

---

## `GET /v1/artifacts/{digest}`

관측 하나를, 그 내용 주소로. 이력을 읽는 방법이 이것이다 — 목록 라우트가 digest를 내주고,
이 라우트가 그것을 실제 데이터로 바꾼다.

```sh
curl -s -H "X-API-Key: $KEY" localhost:8080/v1/artifacts/b9f4c0e2...
```

```json
{
  "digest": "b9f4c0e2...",
  "kind": "video.metadata",
  "target": "dQw4w9WgXcQ",
  "observations": 9,
  "first_fetched_at": "2026-08-19T15:12:56Z",
  "fetched_at": "2026-08-19T23:24:55Z",
  "schema_version": "1",
  "current_schema_version": "1",
  "payload_fields": ["chapters", "most_replayed", "tags", "view_count"],
  "current_fields": ["chapters", "most_replayed", "published_date", "tags", "view_count"],
  "payload": { "...": "수집된 그대로의 바이트" }
}
```

**digest 하나는 관측 하나가 아니다.** 저장소는 내용 주소 방식이라, 수치가 움직이지 않은 영상은
같은 digest에 새 행을 남긴다 — digest가 같으면 "아무것도 안 변했다"로 읽히는 이유가 그것이고,
샘플러가 설계상 만들어내는 상황이다. `observations`는 이 바이트를 공유하는 행의 수이고
`first_fetched_at`은 그중 가장 이른 것이다. 둘을 합치면 쓸모 있는 진술이 된다: 이 payload가
그 기간 동안 이 영상의 모습이었다. `fetched_at`은 가장 최근이다.

**payload는 그대로 돌려주며 다시 파싱하지 않는다.** 옛 normalizer가 쓴 payload도 저장된 모습
그대로 나온다. 보관할 가치가 있는 것은 원래의 관측이고, 오늘의 모델로 다시 모양을 잡는 것이
이력이 이력이기를 그만두는 방식이기 때문이다.

`payload_fields`와 `current_fields`는 선언이 아니라 계산된 값이고, 그 차이가 "이 옛 관측에는
무엇이 없는가"에 대한 정직한 답이다. 옛 버전이 애초에 수집하지 않은 필드는 `payload_fields`에
**없다** — null보다 강한 진술이다.

그 관측을 수집한 버전을 소스가 철회했다면 **410 `retracted`**. 그 버전의 payload는 낡은 것이
아니라 틀린 것이고, 그것을 이력으로 내주는 것은 잘못된 것으로 알려진 관측을 세탁하는 일이다.
404가 아닌 이유는, 관측은 실제로 일어났고 404는 일어나지 않았다고 말하기 때문이다.

관측의 schema 버전이 기록된 적 없고 그 kind가 어떤 버전을 철회한 적이 있다면 **409 `conflict`**.
null 버전은 "괜찮다"가 아니라 "모른다"이고, 버전을 철회한 kind에는 컬럼보다 오래된 행이 있어서
둘 중 어느 쪽인지 알 수 없다. `tubedepth backfill-schema-versions`를 돌린 뒤 다시 물으면 된다.

이 인스턴스가 저장한 적 없는 digest, 그리고 payload가 retention을 지나 사라진 digest는
404 `not_found`.

## 페이지네이션

두 목록 엔드포인트 모두 `cursor`를 돌려주고, `null`이면 마지막 페이지였다는 뜻이다 —
클라이언트는 세지 말고 응답을 읽어서 멈춘다.

```sh
curl -s -H "X-API-Key: $KEY" "localhost:8080/v1/jobs?cursor=$CURSOR"
```

커서는 불투명하며 마지막 행의 시각과 id로 키가 잡힌다. offset이 아니다. offset은 건너뛴
것을 다시 읽고 페이징 도중 행이 들어오면 어긋나는데, 워커가 활발히 쓰는 테이블에서 그것은
같은 잡을 두 번 보여주고 다른 잡을 빠뜨린다는 뜻이다. 직접 만들지 마라 — 이 API가 발급하지
않은 커서는 422 `invalid_request`다.

## 오류

모든 오류는 같은 모양이다.

```json
{ "error": { "code": "not_found", "message": "job not found: j_2f7c1d9a" } }
```

| 상태 | 코드 | 뜻 |
| --- | --- | --- |
| 401 | `unauthenticated` | 키가 없거나, 형식이 틀렸거나, 모르는 키이거나, 폐기됨 |
| 422 | `invalid_request` | 모르는 kind, 해석 불가한 target, 이 API가 발급하지 않은 커서 |
| 404 | `not_found` | 그런 잡이 없음 — 또는 영상이 요청된 것을 갖고 있지 않음 |
| 409 | `conflict` | 잡은 있으나 아직 끝나지 않음 |
| 410 | `retracted` | 이 관측을 수집한 버전이 철회됨 |
| 429 | `rate_limited` | 키 할당량 초과, 또는 업스트림이 이 주소를 거부 |
| 502 | `parse_mismatch` | YouTube는 답했고 우리 파서가 그것을 더는 이해하지 못함 |
| 502 | `upstream_error` | 업스트림이 답했으나 그 답을 쓸 수 없음 |
| 500 | `internal_error` | 우리 버그 |

`parse_mismatch`는 다른 모든 업스트림 실패와 분리되어 있고 **절대 재시도되지 않는다.**
일시적인 것도 네트워크 문제도 아니다. 재시도는 멀쩡히 답한 주소에 대고 요청을 낭비할 뿐이고,
고치는 것은 코드 변경뿐이다. 500이 아니라 502인 것도 같은 이유다 — 500은 운영자를 우리
트레이스백으로 보내고, 502는 메시지에 적힌 렌더러 이름으로 보낸다.

`message`는 사람에게 보여줄 것으로 쓰였고 문제가 된 값을 이름으로 담는다.

## 웹훅

등록 요청에 `webhook_url`을 넣으면 잡이 종료 상태에 도달할 때 한 번 `POST`한다. 폴링의
대체물은 아니다 — 여기서 폴링은 싸다 — 다만 몇 분씩 도는 댓글 수집에서는 대안이 몇 초마다
깨어나 "아직"을 듣는 것뿐이다.

```http
POST /your-endpoint
Content-Type: application/json
X-Tubedepth-Timestamp: 2026-08-19T09:12:44.183726+00:00
X-Tubedepth-Signature: 4a7f...
```

```json
{
  "job_id": "j_2f7c1d9a",
  "kind": "video.metadata",
  "target": "dQw4w9WgXcQ",
  "state": "succeeded",
  "error_code": null,
  "payload_bytes": 26417
}
```

본문에 데이터는 없다 — 콜백이 `succeeded`라고 하면 결과를 가져가면 된다.

**서명을 검증하라.** `f"{timestamp}." + body`에 대한 HMAC-SHA256을 hex로 인코딩한 것이고,
키는 `TUBEDEPTH_WEBHOOK_SECRET`이다. 타임스탬프가 서명 대상 옆이 아니라 **안에** 있으므로,
누가 기록해 둔 전송을 나중에 새 시계로 재생할 수 없다 — 자기 허용 오차보다 오래된 것은
거부하면 된다.

```python
material = f"{timestamp}.".encode() + body
expected = hmac.new(secret.encode(), material, hashlib.sha256).hexdigest()
hmac.compare_digest(expected, presented)
```

콜백 URL은 잡 등록 요청에 실려 다니므로 비밀이 아니고, 비밀처럼 다룰 수도 없다. 이 서비스가
보낸 전송과 URL을 알게 된 아무나가 보낸 것을 구분하는 것은 서명이다.

전송은 at-least-once이고 8회 시도 후 포기한다. 2xx면 배달된 것으로 세고, 그 밖이면 잡은
빚진 상태로 남아 다음 스윕이 다시 시도한다.

## 이 API가 하지 않는 것

- **TLS 없음, OAuth 없음, 스코프 없음.** 헤더 하나, 할당량 하나, 머신 하나.
- **결과 푸시 없음.** 웹훅은 잡이 끝났다고 말할 뿐 수집물을 싣지 않는다.
- **정확한 구독자 수와 싫어요 수 없음.** YouTube가 둘 다 공개하지 않으므로 약속하는 필드를
  두지 않았다. [README](../README.ko.md)의 honest limits 참조.
- **스트리밍이나 부분 결과 없음.** 잡의 결과는 한 번에 통째로 존재한다.
