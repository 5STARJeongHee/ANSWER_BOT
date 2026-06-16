# Supabase 설정 가이드

## 1. Supabase 프로젝트 생성

1. https://supabase.com 에서 로그인 후 **New Project** 생성
2. 프로젝트 이름, DB 비밀번호, 리전(서울: ap-northeast-2) 설정
3. 생성 완료까지 약 1분 대기

## 2. 데이터베이스 스키마 적용

Supabase 대시보드 → **SQL Editor** → **New query** 탭 열기

`slack_bot/db/migrations/001_initial_schema.sql` 전체 내용을 붙여넣고 **Run** (F5) 클릭.

pgvector는 Supabase에 기본 내장되어 있으므로 별도 확장 설치 불필요.

## 3. 연결 문자열 확인

Supabase 대시보드 → **Project Settings** → **Database** → **Connection string** 탭

| 연결 방식 | 포트 | 용도 |
|----------|------|------|
| Transaction Pooler | 6543 | 일반 쿼리 (운영 환경 권장) |
| Session Pooler | 5432 | LISTEN/NOTIFY가 필요한 경우 |
| Direct Connection | 5432 | 마이그레이션 실행 시에만 |

## 4. .env 파일 설정

`slack_bot/.env.example` 을 `slack_bot/.env` 로 복사 후 아래 값 채우기.

```env
# Transaction Pooler 연결 문자열 — 대시보드에서 직접 복사
DATABASE_URL=postgresql://postgres.PROJECT_REF:PASSWORD@aws-0-ap-northeast-2.pooler.supabase.com:6543/postgres?sslmode=require

# Supabase 프로젝트 정보 (REST API 사용 시)
SUPABASE_URL=https://PROJECT_REF.supabase.co
SUPABASE_ANON_KEY=your-anon-key
```

> `PROJECT_REF`는 Supabase 대시보드 URL의 `https://supabase.com/dashboard/project/PROJECT_REF` 부분.

## 5. pgvector 벡터 인덱스 확인

스키마 적용 후 Supabase SQL Editor에서 확인:

```sql
SELECT * FROM pg_extension WHERE extname = 'vector';

SELECT indexname, indexdef
FROM pg_indexes
WHERE tablename = 'context_embedding';
```

## 6. 연결 테스트

```bash
cd slack_bot
python -c "
from db.models import get_engine
engine = get_engine()
with engine.connect() as conn:
    from sqlalchemy import text
    result = conn.execute(text('SELECT version()'))
    print('연결 성공:', result.fetchone()[0])
"
```

## 7. 주의사항

- **NullPool 사용**: Supabase Transaction Pooler는 서버 측에서 연결을 관리하므로 SQLAlchemy 측 pool은 NullPool로 자동 설정됨.
- **SSL 필수**: `sslmode=require`가 없으면 Supabase 연결 거부.
- **Direct Connection은 마이그레이션 전용**: 운영 중에는 Transaction Pooler(6543) 사용.
- **무료 플랜 제한**: Supabase 무료 플랜은 7일 비활성 시 일시 정지. 봇이 주기적으로 DB를 사용하므로 실제로는 정지될 가능성 낮음.
