# 이 저장소에서 작업하기

YouTube Data API가 주지 않는 영상·채널 상세 데이터를 수집하는 **비동기 작업 큐 API**.
Python + FastAPI + SQLAlchemy + SQLite, 패키지는 `tubedepth`, 실행은 전부 `uv`.

## 매 세션 시작할 때

1. `tool/doctor.sh` — 툴체인과 git 훅이 켜져 있는지 확인한다. 건너뛰지 말 것: 클론은
   `core.hooksPath`를 설정하기 전까지 훅이 없고, SQLite가 낡았으면 워커 안에서
   `OperationalError`로 알게 된다.
2. `gh issue list --label blocked` — 이 호스트에 없던 것이 필요해서 이전 세션이 끝내지 못한 일.
3. `docs/status.md` — 현재 상태와 되돌리기 비싼 결정들.

그다음 `README.md`의 마일스톤 표에서 일을 고른다.

**무언가 말이 안 되게 깨지면**, 조사하기 전에 `docs/troubleshooting.md`를 에러 문구로 grep한다.
제목이 실제 메시지 그대로다. 처음부터 읽지 말 것 — 조회 테이블이다.

## 깨면 비싼 규칙

- **yt-dlp는 상한 없이 핀한다.** `pyproject.toml`의 `yt-dlp>=2026.7`에 `<` 가 없는 것은 실수가 아니다.
  YouTube 관련 고장의 실제 해결책은 거의 항상 업그레이드이고, 상한을 걸면 `just update-ytdlp` 한 줄이
  "먼저 pyproject를 고쳐라"가 된다. **추출이 깨졌을 때 첫 수순은 이 코드 디버깅이 아니라 `just update-ytdlp`다.**
- **시한부 URL은 절대 저장하지 않는다.** 자막 `timedtext`/`json3` URL과 yt-dlp `formats`의 서명
  `googlevideo.com` URL은 몇 시간 만에 만료된다. artifact에 넣으면 나중의 403을 보장하는 것이고,
  fixture에 넣으면 pre-commit의 gitleaks가 자격증명으로 잡는다. transcript 잡은 URL을 *쓰고 버린다*.
- **빈 결과와 "파서가 안 맞음"을 절대 같은 것으로 만들지 않는다.** InnerTube 파서는 고정 경로 대신 렌더러
  이름으로 탐색하고, 기대 렌더러가 0건이면서 *응답이 스스로 비었다고 말하는 표지*도 없으면
  `ExtractionError`를 던진다. 조용한 `[]`가 망가진 스크레이퍼가 몇 주씩 배포되어 있는 경로다.
- **`ExtractionError`(파서 문제)는 egress 건강을 절대 건드리지 않는다.** YouTube가 렌더러 이름을 바꾼 날
  분류기가 오작동해서 가진 IP를 전부 격리하는 사고가 구조적으로 불가능해야 한다. 분류기의 기본값은
  `NEUTRAL`이다 — 모르는 실패가 멀쩡한 주소를 태우게 두지 않는다.
- **`src/tubedepth/egress/` 밖에서 `httpx.AsyncClient(`나 `YoutubeDL(`를 생성하지 않는다.**
  아키텍처 테스트가 이걸 grep으로 강제한다. 두 전송 계층이 프록시 사용 여부를 두고 어긋나면 그 불일치는
  눈에 안 보이면서 출발지 IP를 흘린다.
- **WireGuard config는 저장소 바깥에 산다.** `~/.config/tubedepth/wireguard/` (0700). 렌더링된 런타임
  config는 `$XDG_RUNTIME_DIR`(tmpfs, 0600)에 쓰고 종료 시 지운다. `.gitignore`는 방어가 아니라 backstop이다 —
  히스토리에 닿은 키는 파일을 지워도 남는다.
- **DB는 리눅스 파일시스템에 둔다.** `/mnt/c`(drvfs)는 WAL이 요구하는 POSIX 잠금을 신뢰성 있게 제공하지 않고,
  증상은 간헐적 `database is locked`다. `tool/doctor.sh`가 확인한다.

## 워크플로

**코드·주석·docstring·커밋 메시지는 전부 영어로 쓴다.** 문서(README·AGENTS·docs)의 산문은 한국어,
기술 명사는 영어 그대로(`revision`, `egress`, `lane`, `renderer`).

브랜치: `master`(릴리스) ← `dev`(통합, 기본) ← `feature/<name>`·`fix/<name>`.
`dev`에서 갈라져 `dev`로 머지한다. `master`에 직접 커밋하지 않는다.

커밋은 [Conventional Commits](https://www.conventionalcommits.org). `commit-msg` 훅이 나머지를 거부한다.

일을 끝냈다는 것은 `docs/definition-of-done.md`를 충족했다는 뜻이다. 이 호스트에서 확인할 수 없는 항목은
조용히 건너뛰지 말고 `blocked` + `blocked/<무엇이-없는지>` 라벨로 이슈를 연다.

## 이 호스트

- WSL2 (Ubuntu 26.04), 16 CPU / 15 GiB. `systemctl --user` 동작.
- **Docker 없음, passwordless sudo 없음.** 그래서 egress 프록시는 Gluetun이 아니라 **wireproxy**
  (유저스페이스 WireGuard, root 불필요): `nix profile install nixpkgs#wireproxy`.
- Windows 절전/복귀 후 **벽시계가 점프한다.** 간격·윈도우·격리 기한은 전부 `time.monotonic()`을 쓴다.
- 직결 회선은 가정용 IP(KT)라 **YouTube 상대로는 이게 가장 좋은 egress다.** VPN exit은 데이터센터
  주소라 봇 검사 1순위이며, 기본적으로 서드파티(RYD·SponsorBlock) lane 전용이다.

## 병렬 세션

브랜치를 갈아끼우지 말고 워크트리를 쓴다:

```sh
tool/worktree.sh new <name> feature
tool/worktree.sh list
tool/worktree.sh done <name>
```

**병렬화되지 않는 것**: 하나의 SQLite 데이터베이스 파일, wireproxy가 바인드하는 포트 대역(`27100+`),
그리고 ProtonVPN의 동시 접속 할당량. 한 번에 한 세션만 이것들을 쥘 수 있다.

## 레이아웃

| 경로 | 내용 |
| --- | --- |
| `src/tubedepth/sources/` | 데이터 종류 하나 = 모듈 하나. 소스 추가는 여기 파일 1개 + `__init__.py` import 1줄 |
| `src/tubedepth/egress/` | 프록시 풀. **전송 클라이언트를 만드는 유일한 곳** |
| `src/tubedepth/services/` | 업무 규칙. CLI와 API가 공유한다 |
| `src/tubedepth/api/` | service 위의 얇은 층. 여기에 업무 로직을 넣지 않는다 |
| `tests/fixtures/` | 기록된 응답. 네트워크 없이 CI가 돌게 하는 것 |
| `docs/definition-of-done.md` | 마일스톤별 "끝났다"의 정의 |
| `docs/status.md` | 현재 상태와 그 뒤의 결정들 |
| `docs/troubleshooting.md` | 이미 누군가의 오후를 잡아먹은 에러들. 읽지 말고 grep |
