import asyncpg
from contextlib import asynccontextmanager
from app.config import settings
import logging

logger = logging.getLogger(__name__)

class DatabasePool:
    _pool = None
    
    @classmethod
    async def create_pool(cls):
        if cls._pool is None:
            try:
                cls._pool = await asyncpg.create_pool(
                    **settings.db_connection_params,
                    min_size=2,
                    max_size=45,
                    max_queries=50000,  # Recycle connection after 50k queries
                    max_inactive_connection_lifetime=300,  # Close idle connections after 5 minutes
                    command_timeout=60,  # Command timeout in seconds
                    timeout=10  # Fail fast if pool is saturated
                )
                logger.info(f" Database pool created: {settings.db_name}@{settings.db_host}")
            except Exception as e:
                logger.error(f"L Failed to create database pool: {e}")
                raise
        return cls._pool
    
    @classmethod
    async def close_pool(cls):
        if cls._pool:
            await cls._pool.close()
            cls._pool = None
            logger.info("Database pool closed")

@asynccontextmanager
async def get_db_connection(use_transaction: bool = True):
    """
    Equivalent to withPostgresClient from warolabs.com

    Args:
        use_transaction: If True, wraps operations in a transaction.
                        Set to False for read-only operations to avoid transaction overhead.

    Usage:
    async with get_db_connection() as conn:
        result = await conn.fetchrow("SELECT * FROM table WHERE id = $1", id)

    Read-only usage (no transaction):
    async with get_db_connection(use_transaction=False) as conn:
        result = await conn.fetchrow("SELECT * FROM table WHERE id = $1", id)
    """
    pool = await DatabasePool.create_pool()
    async with pool.acquire() as connection:
        if use_transaction:
            async with connection.transaction():
                yield connection
        else:
            yield connection