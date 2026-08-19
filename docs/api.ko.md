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

<!-- kinds:end -->

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
      "last_error_code": null
    }
  ]
}
```

개별 소스가 성치 않아도 `status`는 `"ok"`로 남는다. 이 엔드포인트는 프로세스를 재시작하는
것들이 읽고, 파서 하나가 깨진 것은 나머지 열 종류가 여전히 수집 중인 API를 재기동할 이유가
아니기 때문이다. 나쁜 소식은 사람이 읽는 `sources`에 실린다.

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
    "freshness_seconds": 21600
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
      "digest": "b9f4c0e2...",
      "byte_count": 26417,
      "fetched_at": "2026-08-19T09:12:44Z",
      "fresh_until": "2026-08-19T15:12:44Z"
    }
  ],
  "cursor": null
}
```

artifact 테이블은 덮어쓰지 않고 덧붙이므로, `target`으로 거르면 영상 하나의 이력이 나온다 —
수치가 어떻게 움직였는지. 잡 원장은 그걸 답할 수 없고, 그것을 보관하는 것이 이 테이블이다.

`digest`는 저장된 payload의 내용 주소다. 같은 바이트를 만든 두 수집은 하나를 공유한다.
`fetched_at`이 다른데 digest가 같다면 그 사이에 아무것도 바뀌지 않았다는 뜻이다.

---

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
