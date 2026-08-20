# 공유 PostgreSQL에서 지켜야 하는 것

이 프로젝트는 다른 스크래퍼들과 **물리 데이터베이스 하나를 나눠 쓰고, 경계는 논리적으로만
두는** 구성으로 간다. 이 문서는 그 구성의 규약이고, 함대의 다른 서비스에도 같은 것이 적용된다.

각 항목은 규칙·왜·확인 순서다. **왜를 지운 채 규칙만 옮기지 말 것** — 몇 개는 이유를 모르면
불필요한 격식으로 보여서 제일 먼저 지워진다. 순서는 값싼 순이 아니라 **위험이 큰 순**이다.

## 0. 전제 — 서비스마다 스키마 하나, 롤 하나

```sql
CREATE ROLE tubedepth LOGIN PASSWORD '...';
CREATE SCHEMA tubedepth AUTHORIZATION tubedepth;
ALTER ROLE tubedepth SET search_path = tubedepth;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
```

이 저장소에서는 `deploy/postgres-bootstrap.sql`이 그 파일이고, `just postgres`와 CI가 **같은 파일로**
서버를 세운 뒤 마이그레이션을 검사한다. 문서에만 있는 셋업은 배포와 갈라지고, 갈라진 것은
배포일에 발견된다.

논리 경계는 강제될 때만 경계다. 같은 롤과 같은 `public`을 공유하면 그건 명명 규칙일 뿐이고,
여섯 달 뒤 누군가 경계를 넘는 조인을 하나 쓴다. 그때는 다른 서비스가 이미 그 조인에
의존하므로 되돌릴 수 없다.

테이블명에 서비스 접두사를 붙이지 않는다. 스키마가 그 일을 한다.

## 1. 가장 위험 — autogenerate가 남의 테이블을 지운다

```python
context.configure(..., include_schemas=False)
```

Alembic의 autogenerate는 내 모델과 **데이터베이스에 실제로 있는 것**을 비교한다. 연결이 다른
서비스의 테이블을 볼 수 있으면 그것들을 "모델에 없는 테이블"로 판정하고 **`op.drop_table()`을
생성한다.** 리뷰에서 걸린다고 생각하기 쉽지만 생성된 마이그레이션은 잘 안 읽힌다. 이 목록에서
**다른 팀의 데이터를 지우는 유일한 항목**이다.

그래서 0번이 취향이 아니다 — 스키마 분리가 autogenerate를 안전하게 만드는 장치다. `search_path`가
`tubedepth`뿐이면 리플렉션이 남의 테이블을 아예 못 보고, `include_schemas=False`가 그 범위를
넓히지 않겠다는 선언이다.

**`version_table_schema`는 여기 같이 쓰지 않는다.** 처음 이 문서는 둘을 함께 적었는데, 실제로
돌려보니 그 조합이 **`drop_table('alembic_version')`을 만들어낸다.** alembic은 설정된 스키마와
리플렉션된 스키마를 비교해서 자기 버전 테이블을 제외하는데, `search_path` 아래의 리플렉션은
`None`을 보고하므로 `"tubedepth" != None`이 되어 제외에 실패한다. 스퓨리어스 `drop_table`을
막으려던 설정이 하나를 만든다. 0번의 `search_path`만으로 버전 테이블은 이미 자기 스키마에
들어가므로 **둘은 대안이지 짝이 아니다.**

**확인.** `alembic revision --autogenerate`를 한 번 돌려 `drop_table`이 없는지 본다. 이 저장소에서는
`tests/test_postgres_migrations.py`가 그것을 매번 한다.

## 2. `alembic_version`을 스키마마다 분리한다

기본값은 `public.alembic_version`이고, 서비스가 여럿이면 **같은 한 줄을 서로 덮어쓴다.** 그러면
A의 마이그레이션이 B의 리비전을 head로 알고 이미 적용된 것을 다시 돌리거나 건너뛴다. 조용히
깨지고, 알아챘을 때는 어디까지 적용됐는지가 추측이 된다.

분리하는 방법은 **0번의 `search_path` 하나면 된다** — 확인했다. 별도 설정이 아니라 1번에서 쓰지
말라고 한 그 설정의 대안이다. 다만 `search_path`에 기대는 만큼, 롤 설정이 빠지면 테이블이 조용히
`public`으로 간다. 그래서 이건 문서가 아니라 테스트가 지켜야 한다.

**확인.** `\dt *.alembic_version` — 스키마마다 하나씩 있어야 한다.

## 3. 커넥션은 함대 예산이다

```
max_connections >= Σ(서비스별 pool_size + max_overflow) + 운영 여유
```

이 서비스는 워커 concurrency + API 풀 + CLI 배치다. 한 서비스가 풀을 다 쓰면 **다른 서비스가
`too many clients`로 죽는다** — 원인은 멀쩡하고 피해는 남이 본다. 계산을 지금 해서 적어둔다;
세 번째 서비스가 들어올 때 그 숫자를 다시 찾을 사람이 없다.

## 4. 긴 트랜잭션은 남의 테이블 청소를 막는다

```sql
ALTER ROLE tubedepth SET idle_in_transaction_session_timeout = '30s';
```

autovacuum은 **가장 오래된 열린 트랜잭션보다 나중에 죽은 튜플을 치울 수 없고, 이건 DB
전역이다.** 한 서비스가 외부 API를 기다리며 트랜잭션을 열어두면 다른 서비스의 테이블에 죽은
튜플이 쌓인다. 증상이 원인과 다른 곳에서 나타나 진단이 가장 어렵다.

**스크래퍼가 특히 저지르기 쉽다**: "행을 잠그고 → 가져오고 → 결과를 쓴다"가 자연스러워 보이지만
그 사이가 몇 초다. 네트워크 호출을 트랜잭션 안에서 하지 않는다.

## 5. DB 밖의 파일은 백업이 한 쌍이다

이 프로젝트의 payload는 DB가 아니라 `var/payloads/`의 파일이다. **DB만 복구하면 아무것도
가리키지 않는 인덱스가 남는다** — 수집 경로는 캐시 미스로 넘어가고 `GET /v1/jobs/{id}/result`는
404가 된다. 복구는 **파일 먼저, DB 나중**이어야 인덱스가 파일을 앞지르지 않는다.

공유 DB로 가면 DB 백업이 함대 공통 절차가 되면서 이 두 번째 절반이 잊힌다. 그래서 여기 적는다.

## 6. 시작할 때 스키마를 고치지 않는다

부팅 경로에서 `create_all()`이나 `ALTER TABLE`을 호출하지 않는다. 스키마 변경 경로는
마이그레이션 하나다.

단일 서비스에서는 편의였다. 공유 DB에서는 **부팅할 때마다 남이 있는 DB의 DDL을 건드리는
서비스**가 되고, 재시작 루프에 걸리면 그걸 반복한다. 부작용도 있다: 부팅이 스키마를 고치면
버전 테이블은 그대로라 다음 마이그레이션이 **이미 있는 컬럼을 추가하려다 실패한다**
(`duplicate column name` — 이 저장소가 실제로 겪었다. `docs/troubleshooting.md` 참조).

**이 프로젝트에서는 `Database._repair_existing_tables`를 지우는 일이다.**

## 7. 읽기 경로는 읽기라고 선언한다

PostgreSQL에서 리더는 라이터를 막지 않으므로 성능 이유는 없다. 이유는 둘 — 읽기 경로에서
실수로 쓰면 거부되고, 코드가 의도를 말한다. `decisions/002`의 `readonly=True`는 유지하되
구현이 `PRAGMA query_only`에서 `SET TRANSACTION READ ONLY`로 바뀐다. 나중에 읽기를 핫스탠바이로
보낼 생각이 조금이라도 있으면 선택이 아니다.

## 8. 확장과 전역 설정은 함대 결정이다

`CREATE EXTENSION`은 **데이터베이스 전역**이다. 마이그레이션에 넣으면 한 서비스의 배포가 다른
서비스의 런타임을 바꾼다. 확장 설치는 DB 프로비저닝에서 하고, 서비스 마이그레이션에는
`IF NOT EXISTS`조차 넣지 않는다.

## 9. 시각은 전부 `timestamptz`, 저장은 UTC

서비스마다 시각 규약이 다르면 조인하는 순간 드러나고 그때는 양쪽에 데이터가 있다. naive
datetime을 저장하지 않는다 — 이 프로젝트의 `UtcDateTime` TypeDecorator가 이미 그 일을 하고,
Postgres에서도 유지한다.

## 규칙이 **아닌** 것

- 서비스마다 DB를 나눌 필요는 없다. 그게 이 구성의 전제다. 규모가 커지면 `pg_dump -n <schema>`가
  그대로 이관 단위가 되므로, 스키마 분리는 그 미래에 대한 선불이기도 하다.
- 크로스 서비스 조인이 금지는 아니다. **명시적 GRANT**를 거치면 되고, 그러면 누가 무엇에
  의존하는지가 DB에 기록으로 남는다.
- PgBouncer는 아직 필요 없다. 3번의 계산이 맞으면 서비스가 몇 개 늘 때까지 괜찮다.

## 도입 체크리스트

- [ ] 롤·스키마·`search_path` (0)
- [ ] `include_schemas=False` (1) — `version_table_schema`는 **쓰지 않는다**
- [ ] `alembic_version`이 서비스 스키마에 있는지, 테스트로 (2)
- [ ] autogenerate 한 번 돌려 `drop_table` 없는지 (1)
- [ ] 최대 커넥션 계산해서 함대 합계에 더하기 (3)
- [ ] `idle_in_transaction_session_timeout` (4)
- [ ] 백업 절차에 DB와 payload를 한 쌍으로 (5)
- [ ] 부팅 경로에 DDL 없는지 grep (6)
- [ ] 읽기 경로가 READ ONLY인지 (7)
- [ ] 마이그레이션에 `CREATE EXTENSION` 없는지 (8)
- [ ] 시각 컬럼이 전부 `timestamptz`인지 (9)

시간이 없으면 **1번과 2번만이라도** 먼저 본다. 다른 서비스를 망가뜨릴 수 있는 항목은 그 둘뿐이다.
