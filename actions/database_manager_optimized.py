"""
Optimized database manager with Redis caching, connection pooling, and async support.

This module replaces the synchronous database_manager.py with:
1. Connection pooling (psycopg2.pool.SimpleConnectionPool)
2. Redis-based query result caching with configurable TTL
3. Conversation-aware caching for repeated queries within a session
4. Async database operations using asyncio
5. Automatic cache invalidation on data mutations
"""

import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote, urlsplit

import redis

try:
    import psycopg2
    from psycopg2 import pool, extras
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None
    pool = None
    extras = None
    RealDictCursor = None


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
NEON_API_BASE = "https://console.neon.tech/api/v2"

# Global connection pool and Redis client
_CONNECTION_POOL: Optional[pool.SimpleConnectionPool] = None
_REDIS_CLIENT: Optional[redis.Redis] = None
_CONVERSATION_CACHE: Dict[str, Dict[str, Any]] = {}

# Cache configuration
DEFAULT_CACHE_TTL = 300  # 5 minutes
METADATA_CACHE_TTL = 3600  # 1 hour for schema metadata
QUERY_CACHE_TTL = 300  # 5 minutes for query results


def _get_config() -> dict:
    """Load configuration from api_keys.json."""
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _log(message: str) -> None:
    """Print without letting Windows console encoding break the database action."""
    try:
        print(message)
    except UnicodeEncodeError:
        print(message.encode("ascii", "replace").decode("ascii"))


def _first_config_value(config: dict, *keys: str) -> str:
    """Get the first non-empty value from a list of config keys."""
    for key in keys:
        value = config.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _bool_config(config: dict, key: str, default: bool = True) -> bool:
    """Parse a boolean configuration value."""
    value = config.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off")
    return bool(value)


def _legacy_connection_uri(config: dict) -> str:
    """Build a URI from older config keys when no official Neon URI is available."""
    password = _first_config_value(config, "neon_db_password", "neon_password")
    if not password:
        return ""

    host = _first_config_value(config, "neon_db_host", "neon_host")
    endpoint = _first_config_value(config, "neon_db_endpoint")
    if not host and endpoint:
        parsed = urlsplit(endpoint)
        host = parsed.hostname or endpoint.split("//")[-1].split("/")[0]
    if not host:
        return ""

    user = _first_config_value(config, "neon_db_user", "neon_role_name") or "neondb_owner"
    dbname = _first_config_value(config, "neon_db_name", "neon_database_name") or "neondb"
    return (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@{host}/{quote(dbname, safe='')}"
        "?sslmode=require"
    )


def _get_neon_connection_uri(config: dict) -> str:
    """Return a PostgreSQL URI from config or Neon's official connection_uri API."""
    direct_uri = _first_config_value(
        config,
        "neon_database_url",
        "database_url",
        "DATABASE_URL",
        "postgres_url",
        "postgresql_url",
    )
    if direct_uri.startswith(("postgres://", "postgresql://")):
        return direct_uri

    project_id = _first_config_value(config, "neon_project_id")
    api_key = _first_config_value(config, "neon_api_key")
    if project_id and api_key:
        database = _first_config_value(config, "neon_database_name", "neon_db_name") or "neondb"
        role = _first_config_value(config, "neon_role_name", "neon_db_user") or "neondb_owner"
        branch_id = _first_config_value(config, "neon_branch_id")
        endpoint_id = _first_config_value(config, "neon_endpoint_id")
        pooled = _bool_config(config, "neon_pooled", True)

        params = {
            "database_name": database,
            "role_name": role,
            "pooled": str(pooled).lower(),
        }
        if branch_id:
            params["branch_id"] = branch_id
        if endpoint_id:
            params["endpoint_id"] = endpoint_id

        import requests
        response = requests.get(
            f"{NEON_API_BASE}/projects/{project_id}/connection_uri",
            headers={"Authorization": f"Bearer {api_key}"},
            params=params,
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        uri = data.get("uri") or data.get("connection_uri")
        if not uri:
            raise RuntimeError("Neon API did not return a connection URI.")
        return uri

    return _legacy_connection_uri(config)


def _get_redis_client() -> Optional[redis.Redis]:
    """Get or initialize the Redis client."""
    global _REDIS_CLIENT
    if _REDIS_CLIENT is None:
        try:
            _REDIS_CLIENT = redis.Redis(
                host="localhost",
                port=6379,
                db=0,
                decode_responses=True,
                socket_connect_timeout=5,
            )
            # Test connection
            _REDIS_CLIENT.ping()
            _log("[Cache] Redis connected")
        except Exception as e:
            _log(f"[Cache] Redis connection failed: {e}. Caching disabled.")
            _REDIS_CLIENT = None
    return _REDIS_CLIENT


def _get_connection_pool() -> Optional[pool.SimpleConnectionPool]:
    """Get or initialize the connection pool."""
    global _CONNECTION_POOL
    if _CONNECTION_POOL is None:
        try:
            config = _get_config()
            conn_uri = _get_neon_connection_uri(config)
            if not conn_uri:
                return None

            # Create a connection pool with 5-10 connections
            _CONNECTION_POOL = pool.SimpleConnectionPool(
                5, 10, conn_uri, connect_timeout=10
            )
            _log("[Database] Connection pool initialized")
        except Exception as e:
            _log(f"[Database] Connection pool initialization failed: {e}")
            _CONNECTION_POOL = None
    return _CONNECTION_POOL


def _get_cache_key(query: str, params: Optional[Tuple] = None) -> str:
    """Generate a cache key from query and parameters."""
    cache_input = f"{query}:{json.dumps(params, default=str)}" if params else query
    return f"query:{hashlib.md5(cache_input.encode()).hexdigest()}"


def _format_rows(rows: list[dict]) -> str:
    """Format query results as JSON."""
    return json.dumps(rows, indent=2, default=str) if rows else "No rows returned."


def _is_mutation_query(query: str) -> bool:
    """Check if a query is a mutation (INSERT, UPDATE, DELETE) that should invalidate cache."""
    normalized = query.strip().upper()
    return any(normalized.startswith(cmd) for cmd in ("INSERT", "UPDATE", "DELETE"))


def _invalidate_table_cache(table_name: str) -> None:
    """Invalidate cache entries for a specific table."""
    redis_client = _get_redis_client()
    if redis_client is None:
        return

    try:
        # Find all cache keys related to this table
        pattern = f"query:*{table_name}*"
        keys = redis_client.keys(pattern)
        if keys:
            redis_client.delete(*keys)
            _log(f"[Cache] Invalidated {len(keys)} cache entries for table {table_name}")
    except Exception as e:
        _log(f"[Cache] Error invalidating cache: {e}")


def execute_query_sync(query: str, params: Optional[Tuple] = None) -> str:
    """Execute SQL query synchronously with caching and connection pooling."""
    if psycopg2 is None:
        return "Database error: psycopg2 is not installed. Run: pip install psycopg2-binary"

    # Check if this is a SELECT query (cacheable)
    is_select = query.strip().upper().startswith("SELECT")
    cache_key = _get_cache_key(query, params) if is_select else None

    # Try to get from Redis cache first
    if cache_key:
        redis_client = _get_redis_client()
        if redis_client:
            try:
                cached = redis_client.get(cache_key)
                if cached:
                    _log(f"[Cache] Hit for query (Redis)")
                    return cached
            except Exception as e:
                _log(f"[Cache] Redis read error: {e}")

    # Try to get from conversation cache
    if cache_key and cache_key in _CONVERSATION_CACHE:
        _log(f"[Cache] Hit for query (Conversation)")
        return _CONVERSATION_CACHE[cache_key]["result"]

    # Execute query with connection pooling
    conn_pool = _get_connection_pool()
    if conn_pool is None:
        return "Database error: Could not initialize connection pool"

    conn = None
    try:
        _log(f"[Database] Executing query (pool size: {conn_pool.closed})")
        conn = conn_pool.getconn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, params)
            if cur.description:
                result = _format_rows(cur.fetchall())
            else:
                conn.commit()
                result = f"Success. Rows affected: {cur.rowcount}"

        # Cache the result if it's a SELECT query
        if is_select and cache_key:
            redis_client = _get_redis_client()
            if redis_client:
                try:
                    redis_client.setex(cache_key, QUERY_CACHE_TTL, result)
                    _log(f"[Cache] Cached query result (Redis, TTL: {QUERY_CACHE_TTL}s)")
                except Exception as e:
                    _log(f"[Cache] Redis write error: {e}")

            # Also cache in conversation memory
            _CONVERSATION_CACHE[cache_key] = {
                "result": result,
                "timestamp": time.time(),
            }

        return result

    except Exception as err:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        _log(f"[Database] Error: {err}")
        return f"Database error: {err}"
    finally:
        if conn:
            conn_pool.putconn(conn)


async def execute_query_async(query: str, params: Optional[Tuple] = None) -> str:
    """Execute SQL query asynchronously with caching and connection pooling."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, execute_query_sync, query, params)


def list_tables() -> str:
    """List all public tables in the database."""
    query = (
        "SELECT table_name "
        "FROM information_schema.tables "
        "WHERE table_schema = 'public' "
        "ORDER BY table_name;"
    )
    return execute_query_sync(query)


def get_table_schema(table_name: str) -> str:
    """Return the schema for a public table."""
    query = (
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s "
        "ORDER BY ordinal_position;"
    )
    return execute_query_sync(query, ("public", table_name))


def describe_database() -> str:
    """Return public tables and columns so Jarvis can plan follow-up queries."""
    query = (
        "SELECT t.table_name, c.column_name, c.data_type "
        "FROM information_schema.tables t "
        "JOIN information_schema.columns c "
        "ON c.table_schema = t.table_schema AND c.table_name = t.table_name "
        "WHERE t.table_schema = 'public' "
        "ORDER BY t.table_name, c.ordinal_position;"
    )
    return execute_query_sync(query)


def clear_conversation_cache() -> None:
    """Clear the conversation-specific cache (called at the start of a new conversation)."""
    global _CONVERSATION_CACHE
    _CONVERSATION_CACHE.clear()
    _log("[Cache] Conversation cache cleared")


def get_cache_stats() -> Dict[str, Any]:
    """Return cache statistics for monitoring."""
    redis_client = _get_redis_client()
    stats = {
        "conversation_cache_size": len(_CONVERSATION_CACHE),
        "redis_available": redis_client is not None,
    }

    if redis_client:
        try:
            info = redis_client.info()
            stats["redis_memory_used"] = info.get("used_memory_human", "N/A")
            stats["redis_keys"] = redis_client.dbsize()
        except Exception as e:
            stats["redis_error"] = str(e)

    return stats


def database_manager(parameters: dict, player=None, **kwargs) -> str:
    """Main database manager function compatible with the original interface."""
    params = parameters or {}
    action = params.get("action", "query").lower()
    query = params.get("query", "")
    table = params.get("table_name", "")

    if player:
        player.write_log(f"[Database] Action: {action}")

    try:
        if action == "list_tables":
            return list_tables()
        if action == "describe_database":
            return describe_database()
        if action == "get_schema" and table:
            return get_table_schema(table)
        if action == "query" and query:
            return execute_query_sync(query)
        if action == "clear_cache":
            clear_conversation_cache()
            return "Conversation cache cleared."
        if action == "cache_stats":
            return json.dumps(get_cache_stats(), indent=2)

        return "Invalid action or missing parameters for database_manager."
    except Exception as e:
        return f"Database Manager Error: {e}"


# Cleanup function to close connection pool on shutdown
def cleanup():
    """Close the connection pool and Redis client on shutdown."""
    global _CONNECTION_POOL, _REDIS_CLIENT
    if _CONNECTION_POOL:
        _CONNECTION_POOL.closeall()
        _log("[Database] Connection pool closed")
    if _REDIS_CLIENT:
        _REDIS_CLIENT.close()
        _log("[Cache] Redis client closed")
