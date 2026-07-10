import json
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None
    RealDictCursor = None


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
NEON_API_BASE = "https://console.neon.tech/api/v2"

_URI_CACHE: dict[str, str | float] = {}


def _get_config() -> dict:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _log(message: str) -> None:
    """Print without letting Windows console encoding break the database action."""
    try:
        print(message)
    except UnicodeEncodeError:
        print(message.encode("ascii", "replace").decode("ascii"))


def _first_config_value(config: dict, *keys: str) -> str:
    for key in keys:
        value = config.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _bool_config(config: dict, key: str, default: bool = True) -> bool:
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
        cache_key = json.dumps(
            {
                "project_id": project_id,
                "database": database,
                "role": role,
                "branch": branch_id,
                "endpoint": endpoint_id,
                "pooled": pooled,
            },
            sort_keys=True,
        )

        if _URI_CACHE.get("key") == cache_key and time.time() < float(_URI_CACHE.get("expires", 0)):
            return str(_URI_CACHE.get("uri", ""))

        params = {
            "database_name": database,
            "role_name": role,
            "pooled": str(pooled).lower(),
        }
        if branch_id:
            params["branch_id"] = branch_id
        if endpoint_id:
            params["endpoint_id"] = endpoint_id

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

        _URI_CACHE.update({"key": cache_key, "uri": uri, "expires": time.time() + 300})
        return uri

    return _legacy_connection_uri(config)


def _format_rows(rows: list[dict]) -> str:
    return json.dumps(rows, indent=2, default=str) if rows else "No rows returned."


def execute_query(query: str, params: tuple | list | None = None) -> str:
    """Execute SQL on Neon using a real PostgreSQL connection URI."""
    if psycopg2 is None:
        return "Database error: psycopg2 is not installed. Run: pip install psycopg2-binary"

    conn = None
    try:
        config = _get_config()
        conn_uri = _get_neon_connection_uri(config)
        if not conn_uri:
            return (
                "Database error: missing Neon connection settings. Add neon_project_id "
                "and neon_api_key, or add neon_database_url/database_url."
            )

        host = urlsplit(conn_uri).hostname or "configured Neon host"
        _log(f"[Database] Connecting to {host}...")
        conn = psycopg2.connect(conn_uri, connect_timeout=10)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, params)
            if cur.description:
                return _format_rows(cur.fetchall())
            conn.commit()
            return f"Success. Rows affected: {cur.rowcount}"
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
            conn.close()


def list_tables() -> str:
    """List all public tables in the database."""
    query = (
        "SELECT table_name "
        "FROM information_schema.tables "
        "WHERE table_schema = 'public' "
        "ORDER BY table_name;"
    )
    return execute_query(query)


def get_table_schema(table_name: str) -> str:
    """Return the schema for a public table."""
    query = (
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s "
        "ORDER BY ordinal_position;"
    )
    return execute_query(query, ("public", table_name))


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
    return execute_query(query)


def database_manager(parameters: dict, player=None, **kwargs) -> str:
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
            return execute_query(query)

        return "Invalid action or missing parameters for database_manager."
    except Exception as e:
        return f"Database Manager Error: {e}"
