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
                    min_size=5,
                    max_size=20,
                    command_timeout=60
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
async def get_db_connection():
    """
    Equivalent to withPostgresClient from warolabs.com
    
    Usage:
    async with get_db_connection() as conn:
        result = await conn.fetchrow("SELECT * FROM table WHERE id = $1", id)
    """
    pool = await DatabasePool.create_pool()
    async with pool.acquire() as connection:
        async with connection.transaction():
            yield connection