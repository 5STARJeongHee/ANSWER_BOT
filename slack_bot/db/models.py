# SQLAlchemy ORM 모델 정의 (conversation_message, context_embedding, context_summary, message_feedback, bot_settings, product_category)
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy.orm import DeclarativeBase, relationship, scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

import config


class Base(DeclarativeBase):
    pass


class ConversationMessage(Base):
    """Slack 채널에서 수신된 메시지를 저장하는 테이블."""

    __tablename__ = "conversation_message"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    event_id = Column(String(100), nullable=True, unique=True, index=True)
    channel_id = Column(String(20), nullable=False, index=True)
    thread_ts = Column(String(30), nullable=True, index=True)
    message_ts = Column(String(30), nullable=False, index=True)
    user_id = Column(String(20), nullable=True)
    role = Column(String(10), nullable=False)  # 'user' | 'bot'
    content = Column(Text, nullable=False)
    content_raw = Column(Text, nullable=True)  # PII 마스킹 전 원문 (옵션)
    is_question = Column(Boolean, nullable=True)
    is_fallback = Column(Boolean, default=False)
    category = Column(String(20), nullable=True)          # 미사용(레거시) — is_question으로 대체됨
    response_time_ms = Column(Integer, nullable=True)     # 봇 응답 생성 소요 시간 (ms)
    prompt_tokens = Column(Integer, nullable=True)        # LLM에 전달한 입력 토큰 수
    completion_tokens = Column(Integer, nullable=True)    # LLM이 생성한 출력 토큰 수
    rag_avg_similarity = Column(Float, nullable=True)     # RAG top-k 평균 유사도 (0~1)
    used_web_search = Column(Boolean, default=False)      # 웹 검색 보조 사용 여부
    topic = Column(String(100), nullable=True)            # LLM 추출 핵심 주제 태그 (예: "Redis 연결 오류")
    product_key = Column(String(50), nullable=True)       # LLM 분류 제품 키 (예: "iruda_backend")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("channel_id", "message_ts", name="uq_channel_message_ts"),
    )

    embeddings = relationship(
        "ContextEmbedding", back_populates="source_message", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return (
            f"<ConversationMessage id={self.id} channel={self.channel_id} "
            f"role={self.role} ts={self.message_ts}>"
        )


class ContextEmbedding(Base):
    """메시지 청크에 대한 벡터 임베딩을 저장하는 테이블."""

    __tablename__ = "context_embedding"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    source_message_id = Column(
        BigInteger,
        ForeignKey("conversation_message.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_text = Column(Text, nullable=False)
    # 'message': 단일 메시지 청크 (기본값) / 'thread': 스레드 전체 통합 청크
    chunk_type = Column(String(20), nullable=True)
    # pgvector 컬럼: ENABLE_VECTOR_SEARCH=true 일 때만 사용
    # 타입은 마이그레이션 SQL에서 직접 지정 (sqlalchemy-pgvector 또는 Raw DDL)
    embedding_json = Column(Text, nullable=True)  # fallback: JSON 직렬화 벡터
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    source_message = relationship("ConversationMessage", back_populates="embeddings")

    def __repr__(self) -> str:
        return f"<ContextEmbedding id={self.id} source_message_id={self.source_message_id}>"


class MessageFeedback(Base):
    """봇 답변에 달린 이모지 피드백을 저장하는 테이블."""

    __tablename__ = "message_feedback"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    channel_id = Column(String(20), nullable=False, index=True)
    message_ts = Column(String(30), nullable=False, index=True)  # 봇 답변 ts
    user_id = Column(String(20), nullable=False)
    reaction = Column(String(50), nullable=False)   # thumbsup, thumbsdown 등
    sentiment = Column(String(10), nullable=False)  # positive | negative
    llm_failure_reason = Column(String(30), nullable=True)   # wrong_source|hallucination|out_of_scope|format_issue
    user_failure_reason = Column(String(30), nullable=True)  # 사용자가 직접 선택한 실패 원인
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        # 같은 사용자가 같은 메시지에 같은 이모지를 중복 저장하지 않는다.
        UniqueConstraint("message_ts", "user_id", "reaction", name="uq_feedback_ts_user_reaction"),
    )

    def __repr__(self) -> str:
        return (
            f"<MessageFeedback id={self.id} ts={self.message_ts} "
            f"user={self.user_id} sentiment={self.sentiment}>"
        )


class BotSetting(Base):
    """봇 설정을 저장하는 key-value 테이블."""

    __tablename__ = "bot_settings"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    def __repr__(self) -> str:
        return f"<BotSetting key={self.key!r}>"


class ProductCategory(Base):
    """제품별 담당자 및 질문 카운트를 저장하는 테이블."""

    __tablename__ = "product_categories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    product_key = Column(String(50), unique=True, nullable=False, index=True)
    display_name = Column(String(100), nullable=False)
    owner_user_ids_json = Column(Text, nullable=False, default="[]")
    aliases_json = Column(Text, nullable=False, default="[]")
    question_count = Column(Integer, default=0, nullable=False)
    notified_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    def __repr__(self) -> str:
        return f"<ProductCategory key={self.product_key!r} owners={self.owner_user_ids_json}>"


class ContextSummary(Base):
    """채널별 기간 요약본을 저장하는 테이블 (V2 배치 사용)."""

    __tablename__ = "context_summary"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    channel_id = Column(String(20), nullable=False, index=True)
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)
    summary_text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self) -> str:
        return (
            f"<ContextSummary id={self.id} channel={self.channel_id} "
            f"{self.period_start}~{self.period_end}>"
        )


# ---------------------------------------------------------------------------
# 엔진 및 세션 팩토리
# ---------------------------------------------------------------------------

def _is_supabase_url(url: str) -> bool:
    """Supabase 연결 문자열인지 판별한다."""
    return "supabase.com" in url or "supabase.co" in url


def _create_engine_for_supabase(database_url: str):
    """
    Supabase Transaction Pooler(포트 6543) 기준 엔진을 생성한다.
    - Supabase는 서버 단에서 연결 풀을 관리하므로 NullPool 사용
    - pgvector 확장은 Supabase에 기본 내장되어 있으므로 별도 설치 불필요
    - SSL은 Supabase 접속 시 필수 (sslmode=require)
    """
    connect_args: dict = {}
    if "sslmode" not in database_url:
        connect_args["sslmode"] = "require"

    return create_engine(
        database_url,
        poolclass=NullPool,   # Supabase Pooler가 커넥션을 관리
        connect_args=connect_args,
        echo=config.DEBUG,
    )


def _create_engine_local(database_url: str):
    """로컬 PostgreSQL 연결용 엔진 (개발/테스트 환경)."""
    return create_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        echo=config.DEBUG,
    )


def _create_engine_with_pgvector(database_url: str):
    """
    DATABASE_URL이 Supabase이면 Supabase 최적화 엔진을,
    그 외에는 로컬 PostgreSQL 엔진을 반환한다.
    pgvector 확장은 Supabase에서는 이미 활성화되어 있고,
    로컬에서는 아직 없다면 경고만 출력한다.
    """
    if _is_supabase_url(database_url):
        return _create_engine_for_supabase(database_url)

    # 로컬 PostgreSQL: pgvector 확장 설치 시도
    engine = _create_engine_local(database_url)
    if config.ENABLE_VECTOR_SEARCH:
        with engine.connect() as conn:
            try:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
                conn.commit()
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning(
                    f"pgvector 확장 설치 실패 (ENABLE_VECTOR_SEARCH=false로 전환): {exc}"
                )
    return engine


def get_engine():
    """애플리케이션 전역 SQLAlchemy 엔진을 반환한다."""
    return _create_engine_with_pgvector(config.DATABASE_URL)


def get_session_factory(engine=None):
    """
    스레드 안전한 scoped_session 팩토리를 반환한다.
    threading.Thread에서 DB를 사용할 때 각 스레드마다 독립 세션을 보장한다.
    """
    if engine is None:
        engine = get_engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return scoped_session(factory)


def init_db(engine=None) -> None:
    """테이블이 없으면 생성한다 (개발용 단순 초기화, 운영은 Alembic 사용)."""
    if engine is None:
        engine = get_engine()

    # 테이블 먼저 생성한 뒤 ALTER TABLE을 실행해야 한다.
    Base.metadata.create_all(engine)

    with engine.connect() as conn:
        try:
            # chunk_type 컬럼 추가 (기존 레코드는 NULL = 'message'로 취급)
            conn.execute(
                text("ALTER TABLE context_embedding ADD COLUMN IF NOT EXISTS chunk_type VARCHAR(20);")
            )
            conn.commit()
        except Exception:
            conn.rollback()

    # V2: 히스토리·대시보드 분석 컬럼 추가
    # V3: RAG 품질·웹검색 추적 컬럼 추가
    _new_cols = [
        "ALTER TABLE conversation_message ADD COLUMN IF NOT EXISTS category VARCHAR(20);",
        "ALTER TABLE conversation_message ADD COLUMN IF NOT EXISTS response_time_ms INTEGER;",
        "ALTER TABLE conversation_message ADD COLUMN IF NOT EXISTS prompt_tokens INTEGER;",
        "ALTER TABLE conversation_message ADD COLUMN IF NOT EXISTS completion_tokens INTEGER;",
        "ALTER TABLE conversation_message ADD COLUMN IF NOT EXISTS rag_avg_similarity FLOAT;",
        "ALTER TABLE conversation_message ADD COLUMN IF NOT EXISTS used_web_search BOOLEAN DEFAULT FALSE;",
        "ALTER TABLE conversation_message ADD COLUMN IF NOT EXISTS topic VARCHAR(100);",
    ]
    with engine.connect() as conn:
        for stmt in _new_cols:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                conn.rollback()

    if config.ENABLE_VECTOR_SEARCH:
        with engine.connect() as conn:
            try:
                conn.execute(
                    text(
                        f"ALTER TABLE context_embedding "
                        f"ADD COLUMN IF NOT EXISTS embedding vector({config.EMBEDDING_DIM});"
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_context_embedding_vector "
                        "ON context_embedding USING ivfflat (embedding vector_cosine_ops) "
                        "WITH (lists = 100);"
                    )
                )
                conn.commit()
            except Exception:
                conn.rollback()

    if config.ENABLE_HYBRID_SEARCH:
        with engine.connect() as conn:
            try:
                # pg_trgm 확장 (언어 무관 키워드 검색 — 에러코드·한글 모두 지원)
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm;"))
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_context_embedding_trgm "
                        "ON context_embedding USING gin (chunk_text gin_trgm_ops);"
                    )
                )
                conn.commit()
            except Exception:
                conn.rollback()

    # 제품 분류 컬럼 추가 (fallback 담당자 라우팅용)
    with engine.connect() as conn:
        try:
            conn.execute(
                text("ALTER TABLE conversation_message ADD COLUMN IF NOT EXISTS product_key VARCHAR(50);")
            )
            conn.commit()
        except Exception:
            conn.rollback()

    # 피드백 실패 원인 컬럼 추가 (LLM 분류 + 사용자 직접 선택)
    _feedback_cols = [
        "ALTER TABLE message_feedback ADD COLUMN IF NOT EXISTS llm_failure_reason VARCHAR(30);",
        "ALTER TABLE message_feedback ADD COLUMN IF NOT EXISTS user_failure_reason VARCHAR(30);",
    ]
    with engine.connect() as conn:
        for stmt in _feedback_cols:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                conn.rollback()
