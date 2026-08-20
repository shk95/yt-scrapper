# 하나의 PostgreSQL server를 여러 서비스가 공유할 때의 운영 지시 규정

> 이 문서는 이 저장소가 정하고 개정하는 이 저장소 자신의 운영 규정이다. 다른 서비스가
> 공유 PostgreSQL server에 올라탈 때 그대로 채택할 수 있는 모양을 목표로 쓰여 있지만, 그런
> 채택을 실제로 조율하는 상위 절차는 존재하지 않는다 — 개정은 이 저장소 안에서, 이 파일을
> 고쳐서 한다. 이 저장소가 각 규칙을 실제로 어떻게 적용하는지는 규정 본문이 아니라
> `docs/status.md`의 "규정 적용" 절에 적는다. 그 구분(규칙은 여기, 적용은 거기)은 여전히
> 쓸모가 있어서 유지한다.

## 목적과 적용 범위

이 문서는 여러 서비스가 **하나의 PostgreSQL server(cluster)를 공유하되, 서비스마다 자기
database를 하나씩 소유**하는 구성에 적용한다. "하나의 database 안에서 서비스별 schema를
소유"하는 구성이 아니다 — schema별 소유는 database 안에서도 반복하는 구조라서 아래 규칙 대부분에
여전히 나오지만, 그 스스로가 서비스 사이의 경계는 아니다.

```text
PostgreSQL server / cluster
├─ database: orders    (schema: orders,   owner: orders_owner)
├─ database: catalog   (schema: catalog,  owner: catalog_owner)
└─ database: identity  (schema: identity, owner: identity_owner)
```

이 구성에서 규칙들이 강제되는 방향은 하나가 아니다. 읽을 때 자신이 어느 문단에 있는지 알아야 한다.

- **규칙 4(connection budget)는 여전히 database 경계를 넘는 유일한 규칙이고, 오히려 더 세게
  걸린다.** `max_connections`는 cluster 설정이고 role은 cluster 전역이라, 한 role의
  `CONNECTION LIMIT`은 이 server의 모든 database에 걸쳐 그 role을 제한한다. server에 남은
  공유 자원은 이것 하나뿐이다.
- **규칙 10–13**(cross-service FK, shared table, cross-service SQL, cross-schema
  transaction)은 금지가 아니라 **구조적으로 불가능**해진다. cross-database query는
  `dblink`나 `postgres_fdw`가 있어야 하는데 둘 다 extension이고, 규칙 8이 서비스 migration의
  extension 설치를 금지한다.
- **규칙 14의 extraction test는 오히려 쉬워진다.** 서비스당 database가 이미 하나씩이므로
  추출이 `pg_dump` 한 번이다.
- **규칙 0과 2의 schema 격리 장치는 불필요해지지만 무해하며, 이 저장소는 그것을 의도적으로
  전부 유지한다**(`docs/status.md` 참고). 이 문서가 그 장치를 계속 규칙으로 두는 이유는,
  한 database를 여러 서비스가 실제로 공유하게 되는 경우에도 이 문서가 그대로 작동해야
  하기 때문이다.

이 구조의 목표는 단순히 현재의 공유 server를 안전하게 사용하는 데 그치지 않는다.

> 어느 서비스든 자기 schema(또는 database)를 별도 database로 옮겼을 때 데이터 소유권과
> 애플리케이션 의미가 깨지지 않아야 한다.

문서에서 다음 용어는 강제 수준을 뜻한다.

- **금지한다 / 해야 한다**: 예외 승인 없이는 위반할 수 없는 운영 규칙
- **권장한다**: 기본 선택이며, 다르게 할 때 근거를 기록해야 하는 규칙
- **예외**: 책임자, 만료일, 제거 계획을 기록하고 승인한 한시적 결정

각 서비스는 다음 정보를 저장소 root의 `service-db.json`(JSON, machine-readable manifest)으로 관리해야 한다. 파일명과 위치 자체가 규칙이다 — 서비스마다 다른 이름이나 형식을 고르면 fleet 전체를 훑는 감사가 매번 손으로 파일을 읽어야 한다.

`connection_budget`은 총합 정수 하나가 아니라 role·인스턴스 종류별 내역을 담은 object로 선언한다. 정수 하나는 주장일 뿐이지만, 내역은 규칙 4의 예산 공식과 대조해 합이 맞는지 검사할 수 있다.

```json
{
  "manifest_version": 1,
  "service": "orders",
  "database": "orders",
  "schema": "orders",
  "roles": {
    "owner": "orders_owner",
    "migrator": "orders_migrator",
    "runtime": "orders_runtime"
  },
  "cross_service_dependencies": [],
  "required_extensions": [],
  "external_object_stores": [],
  "connection_budget": {
    "total": 20,
    "runtime": {
      "max_instances_including_rollout": 2,
      "pool_size": 4,
      "max_overflow": 2
    },
    "workers_and_schedulers": 2,
    "migration": 1,
    "rolling_deploy_overlap": 2,
    "service_spare": 3
  },
  "session_defaults": {
    "timezone": "UTC",
    "statement_timeout": "...",
    "lock_timeout": "...",
    "idle_in_transaction_session_timeout": "...",
    "transaction_timeout": "..."
  }
}
```

`session_defaults`는 규칙 5에서 요구하는 timeout류를 manifest에도 선언해 실제 설정과 대조할 수 있게 하는 자리다. 위 예시의 timeout 값은 자리표시자일 뿐 정책이 아니다 — 실제 값은 규칙 5가 말하는 대로 endpoint와 workload의 SLO에 따라 서비스마다 다르게 정한다.

---

## 0. 서비스마다 schema 하나와 소유자 하나를 둔다

**규칙.** 서비스의 테이블, sequence, view, function, type과 Alembic version table은 모두 그 서비스 schema 안에 둔다. 서비스 이름의 table prefix나 `public.shared_*` 같은 명명 규칙을 경계로 사용하지 않는다. `public`을 애플리케이션 객체의 기본 위치로 사용하지 않고, 모든 서비스 schema에서 `PUBLIC`의 권한을 회수한다.

`search_path`에 넣는 schema는 해당 로그인 주체가 신뢰할 수 있는 곳으로만 제한한다. 특히 다른 주체가 `CREATE`할 수 있는 schema를 runtime의 `search_path`에 넣지 않는다.

```sql
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

CREATE ROLE orders_owner NOLOGIN;
CREATE SCHEMA orders AUTHORIZATION orders_owner;
REVOKE ALL ON SCHEMA orders FROM PUBLIC;
```

**이유.** PostgreSQL schema는 namespace일 뿐이며, 실제 경계는 schema의 `USAGE`·`CREATE` 권한과 그 안의 객체 권한으로 형성된다. 또한 writable schema가 `search_path`에 있으면 같은 이름의 함수나 객체를 만든 주체를 신뢰하는 결과가 된다. schema 이름만 나눠 놓고 권한을 나누지 않으면 소유권 경계가 아니다.

**확인 방법.** 다음을 모두 확인한다.

- `public`과 각 서비스 schema에서 `PUBLIC`에 `CREATE`가 없다.
- runtime role의 `SHOW search_path` 결과에 자기 schema와 `pg_catalog` 외의 애플리케이션 schema가 없다.
- runtime role로 자기 schema 밖의 객체를 조회하거나 생성하면 거부된다.
- 사용자 객체가 `public`에 존재하지 않는다.

```sql
SELECT nspname, nspacl
FROM pg_namespace
WHERE nspname IN ('public', 'orders');

SELECT schemaname, tablename, tableowner
FROM pg_tables
WHERE schemaname = 'public';
```

---

## 1. owner, migrator, runtime role을 분리한다

**규칙.** 서비스마다 최소 세 role을 둔다.

```text
orders_owner     NOLOGIN, schema와 그 안의 객체 소유
orders_migrator  LOGIN, 배포 시에만 사용, SET ROLE로 owner 권한 획득
orders_runtime   LOGIN, 실행 중 필요한 DML만 허용
```

runtime role은 schema owner가 아니며 `CREATE`, `ALTER`, `DROP`, `TRUNCATE`, `REFERENCES`, `TRIGGER` 권한을 갖지 않는다. migrator credential은 애플리케이션 runtime 환경에 배포하지 않는다. migration은 `orders_owner`로 `SET ROLE`한 세션에서 실행하여 새 객체의 owner가 일관되게 `orders_owner`가 되도록 한다.

```sql
CREATE ROLE orders_owner NOLOGIN;
CREATE ROLE orders_migrator LOGIN NOINHERIT PASSWORD 'managed-secret';
CREATE ROLE orders_runtime  LOGIN NOINHERIT PASSWORD 'managed-secret';

GRANT orders_owner TO orders_migrator;

GRANT USAGE ON SCHEMA orders TO orders_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE
  ON ALL TABLES IN SCHEMA orders TO orders_runtime;
GRANT USAGE, SELECT
  ON ALL SEQUENCES IN SCHEMA orders TO orders_runtime;

ALTER DEFAULT PRIVILEGES FOR ROLE orders_owner IN SCHEMA orders
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO orders_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE orders_owner IN SCHEMA orders
  GRANT USAGE, SELECT ON SEQUENCES TO orders_runtime;

ALTER ROLE orders_runtime IN DATABASE app
  SET search_path = orders, pg_catalog;
ALTER ROLE orders_migrator IN DATABASE app
  SET search_path = pg_catalog;
```

필요한 function은 `PUBLIC`이 아니라 호출 주체에게만 `EXECUTE`를 부여한다. 기존 객체에 대한 `GRANT`와 미래 객체에 대한 `ALTER DEFAULT PRIVILEGES`를 둘 다 설정한다. 후자는 **지정한 object creator가 앞으로 만드는 객체에만** 적용된다.

**PUBLIC의 function `EXECUTE`는 예외다 — `ALTER DEFAULT PRIVILEGES`로 회수할 수 없다.**
`ALTER DEFAULT PRIVILEGES FOR ROLE orders_owner IN SCHEMA orders REVOKE EXECUTE ON
FUNCTIONS FROM PUBLIC;`는 오류 없이 실행되지만 `pg_default_acl`에 아무 행도 만들지
않는다. PUBLIC에 대한 function `EXECUTE`는 `GRANT`로 부여된 권한이 아니라 PostgreSQL의
내장 기본값이라서, 회수할 대상 자체가 없기 때문이다(실측, PostgreSQL 18.6: 위 문장을
실행한 뒤에도 owner가 새로 만든 function의 `proacl`은 `NULL`이고
`has_function_privilege('public', ..., 'EXECUTE')`는 `true`를 반환했다). 그래서 위
bootstrap 예시에서 이 문장을 뺐다 — 실행해도 아무것도 바꾸지 않는 문장을 규정에 남겨
두면, 그것이 보호하고 있다고 믿게 만드는 것 자체가 피해다.

이 규칙이 실제로 요구하는 것("필요한 function은 PUBLIC이 아니라 호출 주체에게만
`EXECUTE`를 부여한다")을 지키려면 **function을 만드는 쪽에서 매번** 명시적으로
회수해야 한다 — 이 회수를 앞서 걸어 두는 방법은 없다.

```sql
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA orders FROM PUBLIC;
```

이 문장도 그 시점에 이미 존재하는 function에만 적용된다(실측: 이 문장을 실행한 뒤에
만든 function은 다시 PUBLIC에 `EXECUTE`를 갖는다). 따라서 이것은 bootstrap 시점에 한
번 거는 설정이 아니라, function을 만드는 **모든** migration이 스스로 끝에 실행해야 하는
단계다. 사람이 놓치기 쉬운 단계이므로, 아래 확인 방법의 감사 query를 정기 감사와 배포
gate에 반드시 포함한다 — 이 규칙의 실제 강제력은 사전 설정이 아니라 그 감사에 있다.

**이유.** schema owner인 runtime credential이 탈취되거나 잘못된 SQL을 실행하면 DML 사고가 DDL 사고로 확대된다. owner를 `NOLOGIN`으로 두고 migration 경로에서만 사용하면 서비스 경계와 변경 경로를 database가 강제한다.

**확인 방법.** runtime credential로 다음 부정 테스트를 실행해 모두 거부되는지 확인한다.

```sql
CREATE TABLE orders.must_fail(id bigint);
ALTER TABLE orders.some_table ADD COLUMN must_fail text;
DROP TABLE orders.some_table;
TRUNCATE orders.some_table;
```

또한 schema 안의 모든 객체 owner가 해당 서비스 owner role인지 감사한다.

```sql
SELECT n.nspname, c.relname, c.relkind, pg_get_userbyid(c.relowner) AS owner
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'orders'
  AND pg_get_userbyid(c.relowner) <> 'orders_owner';
```

결과가 비어 있어야 한다. 승인된 예외가 있다면 manifest와 일치해야 한다.

`ALTER DEFAULT PRIVILEGES ... REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC`가 아무것도
기록하지 않으므로, PUBLIC이 여전히 `EXECUTE`를 가진 function을 다음 query로 직접 찾는다.

```sql
SELECT n.nspname, p.proname,
       has_function_privilege('public', p.oid, 'EXECUTE') AS public_can_execute
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'orders'
  AND has_function_privilege('public', p.oid, 'EXECUTE');
```

결과가 비어 있어야 한다. 비어 있지 않다면 승인 없이 PUBLIC에 노출된 function이 있다는
뜻이고, 이는 경고가 아니라 배포 gate다 — 앞 절에서 확인했듯 사전에 걸어 둘 방법이 없는
규칙이라 이 감사가 유일한 강제 지점이다.

---

## 2. Alembic autogenerate는 자기 schema만 명시적으로 allowlist한다

**규칙.** `include_schemas=False`와 `search_path`만으로 autogenerate 범위를 통제하지 않는다. 모델 metadata와 migration operation은 서비스 schema를 명시적으로 사용하고, reflection 단계에서 schema allowlist를 적용한다.

```python
SERVICE_SCHEMA = "orders"

metadata_schemas = {table.schema for table in target_metadata.tables.values()}
assert metadata_schemas == {SERVICE_SCHEMA}

def include_name(name, type_, parent_names):
    if type_ == "schema":
        return name == SERVICE_SCHEMA
    if type_ == "table":
        return parent_names.get("schema_name") == SERVICE_SCHEMA
    return True

context.configure(
    connection=connection,
    target_metadata=target_metadata,
    include_schemas=True,
    include_name=include_name,
    version_table="alembic_version",
    version_table_schema=SERVICE_SCHEMA,
)
```

migration connection의 기본 `search_path`는 `pg_catalog`처럼 fail-closed로 두고, `MetaData(schema="orders")`, `op.create_table(..., schema="orders")`처럼 대상 schema를 명시한다. 프로젝트가 다른 Alembic 전략을 선택할 수는 있지만, **실제 타 schema가 존재하는 database를 이용한 격리 테스트**로 동등한 안전성을 증명해야 한다.

생성된 revision은 실행 가능한 후보일 뿐이다. 사람이 검토하지 않은 autogenerate 결과를 자동 적용하지 않는다.

**이유.** autogenerate는 metadata와 reflection 결과의 차이를 migration 후보로 만든다. 범위가 잘못되면 다른 서비스 객체를 "metadata에 없는 객체"로 보고 삭제나 변경 대상으로 만들 수 있다. `search_path`와 dialect의 default schema 해석에만 기대면 설정 조합에 따라 반영 범위가 달라질 수 있다.

**확인 방법.** CI에서 임시 database에 다음 sentinel을 만든다.

```sql
CREATE SCHEMA foreign_sentinel;
CREATE TABLE foreign_sentinel.must_survive(id bigint PRIMARY KEY);
```

그 뒤 autogenerate를 실행하여 다음을 검증한다.

- 생성 revision에 `foreign_sentinel`이 한 번도 나타나지 않는다.
- 자기 schema 밖의 `drop_table`, `drop_column`, `alter_column`, constraint 변경이 없다.
- 빈 database에서 `upgrade head`가 성공한다.
- 현재 운영 schema를 복제한 상태에서도 `upgrade head`가 성공한다.
- `upgrade head` 후 다시 autogenerate하면 의도하지 않은 diff가 없다.

---

## 3. Alembic version state를 서비스별로 격리한다

**규칙.** 각 서비스는 자기 schema 안에 자기 Alembic version table을 둔다. 독립된 migration graph들이 같은 `public.alembic_version`을 공유하지 않는다.

```python
context.configure(
    # ...
    version_table="alembic_version",
    version_table_schema="orders",
)
```

서비스 migration은 해당 서비스 저장소와 배포 파이프라인이 소유한다. 동일 서비스의 migration 두 개가 동시에 실행되지 않도록 배포 단계를 직렬화하거나 서비스별 advisory lock을 사용한다.

**이유.** 서로 독립된 Alembic 환경이 version state 하나를 공유하면 각 migration graph의 현재 revision을 신뢰할 수 없게 된다. version table만 분리해도 동시 migration 충돌까지 해결되는 것은 아니므로 실행 직렬화도 별도로 필요하다.

**확인 방법.** 모든 서비스 schema에 version table이 정확히 하나씩 있고 `public`에는 공용 version table이 없는지 확인한다.

```sql
SELECT schemaname, tablename
FROM pg_tables
WHERE tablename = 'alembic_version'
ORDER BY schemaname;
```

각 서비스에서 `alembic current`와 `alembic heads`가 일치하는지 확인하고, 동시 실행 테스트에서 두 번째 migrator가 대기하거나 실패하도록 검증한다.

---

## 4. connection은 database 전체의 예산으로 관리한다

**규칙.** 서비스별 connection budget을 배포 인스턴스 수까지 포함해 계산하고, 합계가 일반 애플리케이션용 슬롯을 넘지 않게 한다.

```text
서비스 budget
= 최대 동시 인스턴스 수 × 인스턴스당 DB pool 상한
+ worker·scheduler·batch 전용 연결
+ 서비스 migration 연결

Σ(서비스 budget)
<= max_connections
 - superuser_reserved_connections
 - reserved_connections
 - 운영 안전 여유
```

SQLAlchemy를 사용한다면 pool 하나의 상한은 보통 `pool_size + max_overflow`이지만, 프로세스와 worker마다 pool이 따로 생기는지 반드시 반영한다. PgBouncer transaction pooling을 사용하면 애플리케이션 client 수가 아니라 PostgreSQL backend pool 상한을 budget에 사용한다. `max_connections`를 계속 올리는 것을 기본 해결책으로 삼지 않는다.

가능하면 runtime login role에 `CONNECTION LIMIT`도 설정하여 계산 실수를 hard limit로 막는다. rolling deployment로 구·신 인스턴스가 겹치는 구간도 예산에 포함한다.

**이유.** 공유 database에서 한 서비스의 connection 폭증은 다른 모든 서비스의 신규 접속을 막는다. `max_connections`를 높이면 PostgreSQL이 예약하는 일부 자원도 증가하므로 숫자만 늘리는 것은 비용 없는 해결책이 아니다.

**확인 방법.** 배포 전 manifest의 합계를 검사하고, 운영 중 role·서비스별 사용량과 대기 시간을 관찰한다.

```sql
SELECT name, setting
FROM pg_settings
WHERE name IN (
  'max_connections',
  'superuser_reserved_connections',
  'reserved_connections'
)
ORDER BY name;

SELECT usename, application_name, state, count(*) AS connections
FROM pg_stat_activity
GROUP BY usename, application_name, state
ORDER BY connections DESC;
```

최대 autoscaling과 rolling deployment를 재현한 부하 테스트에서도 운영 안전 여유가 남아야 한다.

---

## 5. statement, lock, transaction의 수명 상한을 역할별로 둔다

**규칙.** runtime role에는 최소한 `statement_timeout`, `lock_timeout`, `idle_in_transaction_session_timeout`을 설정한다. PostgreSQL 버전이 지원하면 `transaction_timeout`도 설정하고, 지원하지 않거나 prepared transaction 등 적용 제외가 있으면 애플리케이션 deadline으로 전체 transaction 수명을 제한한다. migrator와 장시간 batch는 runtime 기본값을 무력화하지 말고 별도 role 또는 승인된 세션 설정을 사용한다.

```sql
ALTER ROLE orders_runtime IN DATABASE app
  SET statement_timeout = '30s';
ALTER ROLE orders_runtime IN DATABASE app
  SET lock_timeout = '5s';
ALTER ROLE orders_runtime IN DATABASE app
  SET idle_in_transaction_session_timeout = '15s';
-- 지원 버전에서 서비스 SLO에 맞게 설정
ALTER ROLE orders_runtime IN DATABASE app
  SET transaction_timeout = '60s';
```

예시 값은 정책이 아니다. 실제 값은 endpoint와 workload의 SLO에 따라 정한다. 일반적으로 `lock_timeout`은 `statement_timeout`보다 짧게 둔다. 외부 API 호출, object upload, 사용자 입력 대기는 DB transaction 밖에서 수행한다.

**이유.** 열린 transaction은 lock을 오래 보유할 수 있고, idle transaction도 dead tuple 정리를 지연시켜 table bloat에 기여할 수 있다. 이는 같은 database의 다른 서비스에도 지연과 저장 공간 증가로 나타난다. `statement_timeout`만으로는 statement 사이에서 idle인 transaction의 수명을 제한하지 못한다.

**확인 방법.** 설정값과 오래된 transaction을 점검한다.

`pg_roles.rolconfig`는 이 규칙이 요구하는 형태의 설정을 담지 않는다. 이 규칙은
`ALTER ROLE ... IN DATABASE ... SET ...`로 role×database 조합에 설정을 거는데, 그렇게
건 값은 `pg_db_role_setting`에 저장되고 `pg_roles.rolconfig`는 NULL로 남는다(실측,
PostgreSQL 18.6: 이 규칙대로 설정한 세 role 모두 `rolconfig`가 NULL이었고
`pg_db_role_setting`에는 요구되는 설정이 전부 있었다). 반대 방향으로도 틀린다 —
`rolconfig`는 database를 지정하지 않은 `ALTER ROLE ... SET ...`(이 규칙이 요구하지 않는,
server의 모든 database에 적용되는 형태)에는 값을 채운다. 그래서 `rolconfig`로 감사하면
규정을 지킨 서비스를 미설정으로 보고하고, 규정이 요구하지 않는 database 전역 설정을
준수로 보고한다 — 양방향으로 거꾸로다. 감사는 `pg_db_role_setting`을 직접 봐야 한다.

```sql
SELECT r.rolname, d.datname, s.setconfig
FROM pg_db_role_setting s
JOIN pg_roles r ON r.oid = s.setrole
LEFT JOIN pg_database d ON d.oid = s.setdatabase
WHERE r.rolname LIKE '%\_runtime' ESCAPE '\'
ORDER BY r.rolname, d.datname;

SELECT pid, usename, application_name, state,
       now() - xact_start AS transaction_age,
       now() - state_change AS state_age,
       wait_event_type, wait_event
FROM pg_stat_activity
WHERE xact_start IS NOT NULL
ORDER BY xact_start;
```

CI 또는 staging에서 lock 대기, 장기 statement, idle-in-transaction을 각각 유도하고 의도한 timeout과 오류 처리가 작동하는지 확인한다.

---

## 6. 애플리케이션 startup에서 DDL을 실행하지 않는다

**규칙.** 애플리케이션 부팅 경로에서 `create_all()`, `CREATE TABLE`, `ALTER TABLE`, `DROP`, extension 설치나 자동 schema 수정 기능을 실행하지 않는다. schema 변경 경로는 검토된 migration 하나로 제한한다. migration 실패 시 애플리케이션이 임의로 schema를 보정하거나 `alembic stamp`하지 않는다.

`stamp`는 실제 schema가 그 revision과 동일하다는 별도 검증을 통과했을 때만 운영 절차로 실행한다.

**이유.** DDL은 명령에 따라 강한 lock을 요구할 수 있고 일부 `ALTER TABLE` 작업은 `ACCESS EXCLUSIVE` lock을 획득한다. 재시작되는 runtime이 DDL을 반복하면 다른 서비스까지 지연시킬 수 있다. startup DDL과 migration을 함께 사용하면 실제 schema와 version state가 갈라진다.

**확인 방법.** 다음 세 검사를 모두 수행한다.

- migration 디렉터리 밖에서 DDL 문자열과 ORM schema 생성 API를 정적 검색한다.
- DDL 권한이 없는 runtime role로 빈 schema에서 애플리케이션을 시작해, schema를 만들지 않고 명확히 실패하는지 확인한다.
- 정상 schema에서는 runtime 시작 전후 DDL fingerprint가 같음을 확인한다.

---

## 7. DB 밖의 object와 DB metadata 사이의 일관성을 명시한다

**규칙.** object storage나 파일시스템의 byte를 DB row가 참조하면 다음 write protocol을 사용한다.

```text
생성: immutable key로 upload
   → durability·크기·checksum 확인
   → DB metadata commit

삭제: DB reference 제거 또는 삭제 예정 표시
   → 복구 가능한 grace period
   → 재시도 가능한 garbage collection
```

DB transaction 안에서 upload나 외부 network 호출을 기다리지 않는다. 동일 key를 다른 내용으로 덮어쓰지 않고, 재시도에는 idempotency key를 사용한다. 실패한 upload, metadata commit 실패, GC 실패를 각각 안전하게 재시도할 수 있어야 한다.

백업은 "DB dump 파일 하나"가 아니라 DB snapshot과 object version/manifest가 결합된 **복구 세트**로 정의한다. 두 저장소가 원자적 snapshot을 제공하지 않으면 허용 가능한 시점 차이와 reconciliation 절차를 문서화한다.

**이유.** PostgreSQL transaction은 외부 object store와 원자적으로 commit되지 않는다. DB만 복구하면 missing object reference가, object만 복구하면 orphan object가 남을 수 있다. 단순한 복구 순서만으로 이 문제를 일반적으로 해결할 수 없으므로 write protocol, versioning, GC와 reconciliation이 함께 필요하다.

**확인 방법.** 다음 failure injection과 복구 리허설을 수행한다.

- upload 성공 후 DB commit 실패: orphan이 grace period 뒤 수거된다.
- upload 실패: DB reference가 생성되지 않는다.
- DB reference 제거 후 GC 실패: object가 다시 수거되며 사용자 경로에는 노출되지 않는다.
- 복구 세트로 새 환경을 만든 뒤 sample 또는 전수 checksum 검증에서 missing·mismatch가 없다.
- missing object와 orphan object 수를 측정하는 정기 reconciliation job이 있다.

---

## 8. extension과 database·cluster 수준 설정은 중앙에서 관리한다

**규칙.** 서비스 migration은 `CREATE EXTENSION`, `ALTER EXTENSION`, `DROP EXTENSION`, `ALTER DATABASE`, `ALTER SYSTEM`을 실행하지 않는다. extension과 공유 설정은 database provisioning 또는 플랫폼 변경 절차에서 승인·적용한다.

각 서비스는 필요한 extension, 최소·최대 호환 version, 필요한 schema, 사용 기능을 manifest에 선언한다. 서비스별 timeout이나 `search_path`처럼 role 범위가 적절한 설정은 중앙 정책 안에서 `ALTER ROLE ... IN DATABASE`로 내린다.

**이유.** extension은 현재 database에 등록되고 object는 특정 schema에 놓일 수 있지만, 설치 상태와 version은 같은 database의 서비스들이 공유한다. database·cluster 수준 설정 변경도 다른 서비스의 동작과 자원 사용을 바꿀 수 있다. 따라서 개별 서비스 배포가 공유 환경을 임의로 변경해서는 안 된다.

**확인 방법.** migration에서 금지 구문을 정적 검사하고 실제 상태를 manifest와 비교한다.

```sql
SELECT e.extname, e.extversion, n.nspname AS object_schema
FROM pg_extension e
JOIN pg_namespace n ON n.oid = e.extnamespace
ORDER BY e.extname;

SELECT name, setting, source, sourcefile
FROM pg_settings
WHERE source NOT IN ('default', 'override')
ORDER BY name;
```

extension upgrade는 공유 staging database에서 모든 소비 서비스의 회귀 테스트를 통과한 뒤 수행한다.

---

## 9. timestamp는 의미에 따라 type을 선택한다

**규칙.** 실제 세계의 발생 시점, 생성·수정 시각, 만료 시각처럼 하나의 instant를 나타내는 값은 `timestamptz`를 사용한다. 애플리케이션은 timezone-aware 값만 전달하고, 세션 `TimeZone`은 출력과 암묵 변환의 예측 가능성을 위해 기본적으로 `UTC`로 통일한다.

다음 값은 의미에 맞는 다른 type을 사용할 수 있다.

```text
달력 날짜                  → date
매일 09:00 같은 지역 시각  → time 또는 timestamp without time zone
지역 규칙이 필요한 일정     → local date/time + IANA timezone name
기간                        → interval 또는 명시한 단위의 수치
```

`timestamptz`는 instant를 내부적으로 UTC 기준으로 저장하지만 원래 입력한 timezone 이름을 보존하지 않는다. 원래 지역 규칙이 업무 의미라면 `Asia/Seoul` 같은 IANA timezone name을 별도 column에 저장한다. `timestamp without time zone`은 금지 type이 아니라 **instant를 표현하는 데 사용하면 안 되는 type**이다.

**이유.** 모든 시간 값을 무조건 `timestamptz`로 바꾸면 달력상의 지역 시각 의미를 훼손할 수 있고, 반대로 instant를 timezone 없는 값으로 저장하면 해석이 session 설정과 코드 관례에 의존한다. 두 의미를 구분해야 DST와 서비스 간 직렬화에서 일관성을 유지할 수 있다.

**확인 방법.** timestamp column을 열거하고 각 column의 의미와 type이 schema 문서에 대응하는지 검사한다.

```sql
SELECT table_schema, table_name, column_name, data_type
FROM information_schema.columns
WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
  AND data_type LIKE 'timestamp%'
ORDER BY table_schema, table_name, ordinal_position;
```

instant column에 naive datetime을 넣으면 애플리케이션이 거부하는지, DST 전환 구간의 round trip과 API 직렬화가 동일 instant를 유지하는지 테스트한다.

---

## 10. cross-service foreign key와 DB-level 참조 무결성 결합을 금지한다

**규칙.** 한 서비스 schema의 table이 다른 서비스 schema의 table을 참조하는 foreign key를 만들지 않는다. 동일한 결합을 trigger, function, generated expression 등으로 우회 구현하지 않는다. 다른 서비스의 identifier는 일반 값으로 저장하고, 유효성은 API, event, local projection과 보상 절차로 관리한다.

**이유.** cross-service FK는 migration 순서, 삭제 정책, restore, 장애, 배포와 schema extraction을 하나의 database 수명 주기에 묶는다. 같은 database에서 기술적으로 가능하다는 사실은 서비스 경계에 적합하다는 뜻이 아니다.

**확인 방법.** 다음 query 결과가 비어 있어야 한다.

```sql
SELECT c.conname,
       src_ns.nspname AS source_schema,
       src.relname AS source_table,
       dst_ns.nspname AS target_schema,
       dst.relname AS target_table
FROM pg_constraint c
JOIN pg_class src ON src.oid = c.conrelid
JOIN pg_namespace src_ns ON src_ns.oid = src.relnamespace
JOIN pg_class dst ON dst.oid = c.confrelid
JOIN pg_namespace dst_ns ON dst_ns.oid = dst.relnamespace
WHERE c.contype = 'f'
  AND src_ns.nspname <> dst_ns.nspname
  AND src_ns.nspname NOT IN ('pg_catalog', 'information_schema')
  AND dst_ns.nspname NOT IN ('pg_catalog', 'information_schema');
```

또한 trigger와 function 정의에서 타 서비스 schema를 참조하는지 정적·catalog 검사를 수행하고, extraction test에서 원래 database에 대한 연결을 차단한다.

---

## 11. shared table을 만들지 않는다

**규칙.** 둘 이상의 서비스가 공동 소유하는 table을 만들지 않는다. `public.common_*`, `shared_lookup`, `global_status` 같은 무소유 또는 공동소유 table도 금지한다. 공유 개념에는 반드시 한 서비스 owner를 정하고, 다른 서비스는 API·event·local projection을 기본 경로로 사용한다.

reference data가 작고 안정적이어도 각 소비 서비스가 자기 schema에 versioned copy를 가질 수 있다. 이때 원본 owner, 배포 방식, version 호환 규칙을 기록한다.

**이유.** 공동소유 table은 schema migration과 데이터 의미 변경의 단일 책임자를 없애고, 서비스별 schema를 이름뿐인 경계로 만든다. 특히 `public`의 공유 table은 시간이 지나면서 사실상의 중앙 도메인 모델이 되기 쉽다.

**확인 방법.** 다음을 운영 gate로 둔다.

- `public`에 사용자 table이 없다.
- 모든 사용자 table이 정확히 한 서비스 manifest의 schema와 owner에 매핑된다.
- 두 서비스 migration 저장소가 같은 객체를 생성·변경하지 않는다.
- local projection은 원본이 아니라 파생본으로 표시되고 재구축할 수 있다.

---

## 12. cross-service SQL 접근은 명시적 GRANT와 의존성 기록을 요구한다

**규칙.** 기본 통신 경로는 API 또는 event다. 같은 database의 직접 `SELECT`나 JOIN이 꼭 필요하면 예외로 취급하여 다음 조건을 모두 만족해야 한다.

- provider가 소유한 안정된 view 또는 승인된 최소 column에만 권한을 준다.
- consumer에는 provider schema의 `USAGE`와 필요한 객체의 `SELECT`만 부여한다.
- provider schema를 consumer의 `search_path`에 추가하지 않고 항상 schema-qualified name을 쓴다.
- `INSERT`, `UPDATE`, `DELETE`, `TRUNCATE`, `REFERENCES`, `TRIGGER`, sequence 권한은 부여하지 않는다.
- 의존성 registry에 provider, consumer, object·column, 목적, 데이터 의미, freshness, 변경 통지, 승인자, 만료일, 제거·extraction 계획을 기록한다.

```sql
GRANT USAGE ON SCHEMA catalog TO orders_runtime;
GRANT SELECT (product_id, sellable)
  ON catalog.orders_product_contract_v1 TO orders_runtime;
```

명시적 `GRANT`는 접근을 정당화하거나 extraction-safe하게 만들지 않는다. 단지 권한과 의존성을 보이게 만드는 최소 조건이다.

**이유.** 직접 SQL 의존은 provider의 물리 schema와 동시 가용성에 consumer를 결합한다. 암묵 접근보다 명시적 grant가 낫지만, 별도 database로 옮기면 같은 query가 더 이상 작동하지 않으므로 제거 또는 대체 경로가 필요하다.

**확인 방법.** ACL을 dependency registry와 대조하여 등록되지 않은 grant를 실패 처리한다.

```sql
SELECT table_schema, table_name, grantee, privilege_type
FROM information_schema.role_table_grants
WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
ORDER BY table_schema, table_name, grantee, privilege_type;

SELECT table_schema, table_name, column_name, grantee, privilege_type
FROM information_schema.role_column_grants
WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
ORDER BY table_schema, table_name, column_name, grantee, privilege_type;
```

provider contract 변경 테스트와 extraction test에서 consumer가 API·event·local projection 대체 경로로 계속 동작하는지 확인한다. 대체 경로가 없으면 그 서비스는 extraction gate를 통과하지 못한다.

---

## 13. 서비스 transaction은 자기 소유 schema 안에서 끝낸다

**규칙.** 하나의 application transaction에서 여러 서비스 schema를 함께 쓰지 않는다. 다른 서비스의 상태 변경은 API 또는 event로 요청하고, 원자성이 필요하면 outbox/inbox, idempotency key와 보상 절차를 사용한다. 직접 읽기 예외가 있더라도 consumer transaction에서 provider data를 잠그거나 변경하지 않는다.

**이유.** 같은 database이므로 cross-schema transaction은 쉽게 작성할 수 있지만, 이는 transaction boundary를 물리 배치에 결합한다. schema를 별도 database로 옮기는 순간 같은 ACID transaction을 유지할 수 없으며 서비스 장애와 배포도 결합된다.

**확인 방법.** SQL tracing 또는 query log에서 하나의 transaction ID가 둘 이상의 서비스 schema에 DML을 수행하는지 검사한다. extraction test에서는 service database 외의 DB 권한과 network 경로를 차단한 상태로 쓰기 흐름, 중복 event, consumer 지연, 부분 실패를 검증한다.

---

## 14. schema extraction test를 실제 release gate로 운영한다

**규칙.** 신규 서비스의 운영 투입 전, 그리고 cross-service grant·extension·external object 계약이 바뀔 때마다 해당 schema를 깨끗한 별도 database로 옮기는 extraction test를 실행한다. 정기적으로도 반복한다. `pg_dump -n` 파일 생성 성공만으로 통과로 간주하지 않는다.

최소 절차는 다음과 같다.

1. 원본 database의 schema, ACL, extension, cross-schema dependency, large object 사용을 inventory한다.
2. `pg_dump --format=custom --schema=orders`로 대상 schema를 dump한다.
3. 깨끗한 target database를 만들고, 중앙 승인된 extension과 필수 설정만 provisioning한다.
4. `pg_restore --no-owner --no-privileges`로 복원하고 서비스 role·grant를 target에 다시 적용한다.
5. Alembic version state가 보존되고 `upgrade head` 및 무차이 autogenerate 검사가 성공하는지 확인한다.
6. 대상 서비스의 DB 연결만 target으로 바꾸고 원본 database에 대한 DB 권한과 우회 network 경로를 차단한다.
7. read/write, migration, outbox/inbox, API/event contract, timeout, backup·external object 복구 smoke test를 수행한다.
8. 기존 cross-service SQL consumer는 등록된 대체 경로로 전환하여 계속 동작하는지 확인한다.

통과 기준은 다음과 같다.

- clean target에 unresolved cross-schema dependency 없이 복원된다.
- 서비스 runtime과 migration이 target database만으로 성공한다.
- 다른 서비스의 정상 흐름이 유지된다.
- cross-service FK, shared table, 타 서비스 schema write가 없다.
- 승인된 extension·설정·external object 의존성이 manifest와 일치한다.
- 모든 직접 SQL grant에 동작하는 extraction 대체 경로가 있다.

**이유.** PostgreSQL은 `pg_dump -n`이 선택한 schema가 의존하는 외부 객체를 자동으로 포함하지 않으며, 그 dump가 깨끗한 database에 독립 복원된다고 보장하지 않는다. "나중에 분리할 수 있다"는 설계 설명은 실제 복원과 전환 테스트를 통과해야만 검증된 사실이 된다.

**확인 방법.** CI 또는 staging 결과물로 다음 증거를 보존한다.

- dump와 restore log
- cross-schema dependency scan 결과
- target의 schema·owner·ACL inventory
- Alembic current/head와 no-diff 결과
- 서비스 smoke·failure test 결과
- 원본 DB 차단 증거
- cross-service consumer 대체 경로 테스트 결과
- external object checksum/reconciliation 결과

어느 하나라도 해결되지 않으면 `PASS WITH EXCEPTION`으로 완화하지 않는다. extraction 가능성은 통과하거나 실패한다. 실패 항목은 owner, 수정 계획과 재시험 날짜를 가진 명시적 부채로 기록한다.

---

## 신규 서비스 도입 체크리스트

- [ ] 서비스 schema와 `NOLOGIN` owner를 만들었다.
- [ ] migrator와 runtime credential·권한·배포 위치를 분리했다.
- [ ] `PUBLIC`과 runtime에서 DDL 권한을 제거했다.
- [ ] 기존 객체와 미래 객체의 runtime 권한을 각각 설정했다.
- [ ] 모든 서비스 객체가 자기 schema와 owner에 속한다.
- [ ] Alembic metadata와 operation이 schema-qualified다.
- [ ] autogenerate reflection에 schema allowlist와 foreign-schema sentinel test가 있다.
- [ ] 서비스별 `alembic_version`과 migration 직렬화가 있다.
- [ ] 최대 scale·rolling deploy를 포함한 connection budget이 승인됐다.
- [ ] statement, lock, idle transaction, 전체 transaction 수명 제한이 검증됐다.
- [ ] startup 경로에 DDL이 없고 runtime DDL 부정 테스트가 통과했다.
- [ ] external object의 write·delete·backup·reconciliation 절차가 있다.
- [ ] extension과 공유 설정이 중앙 manifest 및 provisioning으로 관리된다.
- [ ] timestamp column마다 instant와 calendar-local 의미가 구분돼 있다.
- [ ] cross-service FK와 우회 참조 무결성 결합이 없다.
- [ ] shared table이 없고 모든 table owner가 하나다.
- [ ] cross-service SQL grant가 registry, 만료일, 대체 경로와 일치한다.
- [ ] cross-schema write transaction이 없다.
- [ ] clean target database extraction test가 통과했다.

---

## 운영 중 정기 감사

| 주기 | 확인 대상 |
|---|---|
| 배포마다 | migration review, schema allowlist, version head, startup DDL, connection budget |
| 매일 | connection 사용량, 오래된 transaction, timeout, missing/orphan object 지표 |
| 권한 변경마다 | role membership, schema/table/function ACL, dependency registry |
| extension·공유 설정 변경마다 | 전체 소비 서비스 회귀 테스트와 extraction test |
| 정기 리허설 | backup 복구와 서비스별 clean-database extraction |

감사에서 발견한 미등록 cross-service dependency, runtime DDL 권한, cross-service FK, shared table은 단순 경고가 아니라 release blocker로 처리한다.

---

## 공식 문서 근거

- [PostgreSQL: Schemas and secure `search_path` usage](https://www.postgresql.org/docs/current/ddl-schemas.html)
- [PostgreSQL: Privileges](https://www.postgresql.org/docs/current/ddl-priv.html)
- [Alembic: Autogenerate and schema filtering](https://alembic.sqlalchemy.org/en/latest/autogenerate.html)
- [PostgreSQL: Client connection defaults and timeouts](https://www.postgresql.org/docs/current/runtime-config-client.html)
- [PostgreSQL: Connection settings and reserved slots](https://www.postgresql.org/docs/current/runtime-config-connection.html)
- [PostgreSQL: Date/time types](https://www.postgresql.org/docs/current/datatype-datetime.html)
- [PostgreSQL: `CREATE EXTENSION`](https://www.postgresql.org/docs/current/sql-createextension.html)
