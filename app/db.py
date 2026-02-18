from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from typing import Generator
import os

# DATABASE_URL defaults to a local SQLite file at ./data.db (relative to backend/app's working directory).
# Override via the DATABASE_URL environment variable for staging/production.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data.db")

# Build the SQLAlchemy engine with backend-specific settings.
# - SQLite (dev/local): allow same-thread access since it's a file-based database.
# - Server DBs (e.g., MySQL/Postgres): enable safe pooling to avoid stale or dropped connections under load.
if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        pool_recycle=280,  # recycle connections periodically to prevent 'MySQL server has gone away'
        pool_size=10,
        max_overflow=20,
    )

# Session factory: one session per request; autocommit and autoflush disabled for explicit transaction control
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base class for ORM models declared via SQLAlchemy's declarative API
Base = declarative_base()


def get_db() -> Generator:
    """
    FastAPI dependency.

    Yields a database session for the lifetime of the request and guarantees it
    is closed afterwards, even if an exception is raised.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
