from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.pool import QueuePool
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")

if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(
    DATABASE_URL,
    # QueuePool keeps a pool of reusable connections
    poolclass=QueuePool,

    # How many connections to keep open permanently
    # These are always ready — no setup time when a request comes in
    pool_size=10,

    # How many EXTRA connections allowed during traffic spikes
    # So maximum simultaneous connections = pool_size + max_overflow = 20
    max_overflow=10,

    # How long (seconds) a request waits for a free connection
    # If all 20 connections are busy after 30s, raise an error
    pool_timeout=30,

    # Recycle connections older than 30 minutes
    # Neon closes idle connections — this prevents "connection closed" errors
    pool_recycle=1800,

    # Test each connection before using it
    # If a connection died silently, this detects it and gets a fresh one
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()