# 릴리스 절차

버전은 **한 곳**에만 쓰여 있다: `src/tubedepth/__init__.py`의 `__version__`.
`pyproject.toml`은 `dynamic = ["version"]`으로 그 줄을 읽고, `tubedepth version`과
`GET /healthz`와 OpenAPI 문서가 전부 거기서 온다. 그래서 버전 상향은 편집 한 번이고,
절반만 성공하는 상태가 존재하지 않는다.

## 무엇을 올릴지 고르기

[semver](https://semver.org). 1.0 이전이므로 major는 움직이지 않고, 판단은 둘 사이에서만 한다.

| | 언제 |
| --- | --- |
| **minor** (`0.2.0`) | 새 kind, 새 엔드포인트, 수집 payload 모양 변경, 설정 기본값 변경 |
| **patch** (`0.1.1`) | 버그 수정, 문서, 성능, 파서 복구 |

**패키지 버전과 `/v1`은 별개다.** `/v1`은 [`api.md`](api.md)에 대고 작성한 클라이언트가
깨질 때만 움직인다. 파서를 고쳐 같은 필드를 다시 채우는 것은 patch이지 새 API 버전이 아니다.

**payload 모양을 바꿨다면 그 소스의 `schema_version`도 올린다.** artifact는 그것으로 키가
잡히므로, 올리지 않으면 낡은 payload가 새 모양인 척하며 캐시 히트로 나간다. 이것이 릴리스에서
가장 놓치기 쉬운 항목이다.

**그리고 올렸다면, 직전 버전을 `src/tubedepth/schema_versions.py`의 `PREVIOUS_VERSIONS`에
한 줄 추가한다.** 소스는 자기가 지금 무엇인지만 알고 fingerprint는 SHA-256이라 되돌려주지
않으므로, 그 목록이 없으면 그 버전으로 쓰인 payload는 **어떤 방법으로도** 그 버전에 귀속시킬 수
없게 된다. 지금 이 사실을 아는 유일한 순간이 여기다.

**이제 CI가 그 절반을 강제한다.** payload 모델의 모양이 바뀌었는데 `schema_version`이 그대로면
`tests/test_payload_shapes.py`가 어느 kind의 어느 줄이 바뀌었는지 대고 실패한다. bump한 뒤
`just record-payload-shapes`로 lock에 추가한다 — bump하지 않은 채 기록하려 하면 거부한다.
초록이라는 것은 *기록되지 않은 모양 변경이 없다*는 뜻이지 *bump가 필요 없었다*는 뜻이 아니다.
필드 모양은 그대로인데 그 필드의 의미가 바뀌는 변경은 기계가 잡지 못한다.

## 절차

`dev`가 초록이고 `master`에 머지할 준비가 된 상태에서 시작한다.

```sh
git switch dev && git pull
just check                       # 여기서 빨간 것을 릴리스로 밀지 않는다
```

1. **CHANGELOG 확정.** `CHANGELOG.md`의 `## [Unreleased]` 아래 내용을
   `## [0.2.0] - YYYY-MM-DD`로 바꾸고, 새 빈 `## [Unreleased]`를 위에 만든다.
   맨 아래 링크 두 줄도 갱신한다.
2. **`CHANGELOG.ko.md`도 똑같이.** 번역이 빠지면 테스트가 잡는다 —
   두 파일의 버전 헤딩 집합이 같아야 한다.
3. **버전 상향.** `src/tubedepth/__init__.py`의 `__version__` 한 줄.
4. **검사.**

   ```sh
   just check
   uv run tubedepth version      # 새 번호가 나오는지 눈으로 확인
   ```

   `uv sync` 없이 실행하면 예전 번호가 나올 수 있다. `just check`가 `--frozen`으로
   재설치하므로 순서를 지키면 된다.
5. **커밋.**

   ```sh
   git commit -am "chore(release): v0.2.0"
   ```
6. **`master`로.**

   ```sh
   git switch master && git pull
   git merge --no-ff dev
   ```
7. **태그.** 주석 태그로 남긴다. 경량 태그는 누가 언제 찍었는지를 기록하지 않는다.

   ```sh
   git tag -a v0.2.0 -m "v0.2.0"
   git push origin master --follow-tags
   git switch dev && git merge master && git push
   ```

## 릴리스가 아닌 것

- **태그를 옮기지 않는다.** 이미 밀어낸 태그를 다른 커밋으로 옮기면, 그 태그를 이미 가져간
  클론은 조용히 다른 코드를 가리킨 채로 남는다. 잘못 찍었으면 새 patch를 낸다.
- **`master`에 직접 커밋하지 않는다.** 릴리스 커밋도 `dev`에서 만들어 머지한다.
- **`CHANGELOG`에 커밋 목록을 붙여넣지 않는다.** `git log`가 이미 그것이다.
  변경 기록은 *읽는 사람이 무엇을 해야 하는지*를 적는 곳이다.

## 검사가 강제하는 것

`tests/test_documentation_is_true.py`가 릴리스에 대해 세 가지를 확인한다. 셋 다
사람이 잊는 항목이라서 있다.

- `__version__`이 `CHANGELOG.md`의 최신 릴리스와 같은가
- 두 CHANGELOG가 같은 릴리스 집합을 기록하는가
- `pyproject.toml`이 자기 버전을 따로 갖고 있지 않은가

태그 자체는 검사하지 않는다 — 작업 트리에 없는 것이고, 없다고 CI를 빨갛게 만들면
릴리스 커밋이 항상 빨간 상태로 머지된다.
