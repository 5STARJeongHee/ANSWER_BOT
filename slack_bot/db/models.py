# SQLAlchemy ORM 모델 정의 (conversation_message, context_embedding, context_summary)
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    text,
)
from sqlalchemy.orm import DeclarativeBase, relationship, scoped_session, sessionmaker

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
    # pgvector 컬럼: ENABLE_VECTOR_SEARCH=true 일 때만 사용
    # 타입은 마이그레이션 SQL에서 직접 지정 (sqlalchemy-pgvector 또는 Raw DDL)
    embedding_json = Column(Text, nullable=True)  # fallback: JSON 직렬화 벡터
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    source_message = relationship("ConversationMessage", back_populates="embeddings")

    def __repr__(self) -> str:
        return f"<ContextEmbedding id={self.id} source_message_id={self.source_message_id}>"


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

def _create_engine_with_pgvector(database_url: str):
    """
    PostgreSQL 엔진을 생성하고 pgvector 확장 활성화를 시도한다.
    ENABLE_VECTOR_SEARCH=false이면 확장 없이 생성한다.
    """
    engine = create_engine(
        database_url,
        pool_pre_ping=True,      # 끊어진 커넥션 자동 재연결
        pool_size=5,
        max_overflow=10,
        echo=config.DEBUG,
    )

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

    Base.metadata.create_all(engine)
