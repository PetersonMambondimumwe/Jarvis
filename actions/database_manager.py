import json
import sys
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from pathlib import Path

def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR        = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

def _get_config() -> dict:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def execute_query(query: str) -> str:
    """Executes a SQL query on the Neon database. Tries direct TCP first, then falls back to Data API."""
    # 1. Try Direct TCP (psycopg2)
    conn = None
    try:
        config = _get_config()
        endpoint = config.get("neon_db_endpoint", "")
        host = endpoint.split("//")[-1].split("/")[0]
        user = "neondb_owner"
        password = config.get("neon_db_password", "npg_A5x7fGqTIDHk")
        dbname = "neondb"
        
        conn_str = f"postgresql://{user}:{password}@{host}/{dbname}?sslmode=require"
        
        print(f"[Database] 🔌 Connecting to {host}...")
        conn = psycopg2.connect(conn_str, connect_timeout=5)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query)
            if cur.description:
                rows = cur.fetchall()
                return json.dumps(rows, indent=2, default=str) if rows else "No rows returned."
            conn.commit()
            return f"Success. Rows affected: {cur.rowcount}"
            
    except Exception as tcp_err:
        if conn: conn.rollback()
        print(f"[Database] ⚠️ TCP Connection failed: {tcp_err}")
        
        # 2. Fallback to Data API (HTTP)
        try:
            api_key = config.get("neon_api_key")
            if not api_key or not endpoint:
                return f"Database error: Direct connection failed ({tcp_err}) and Data API config is missing."

            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            # Try 'sql' key first, then 'query' key
            for key in ["sql", "query"]:
                response = requests.post(endpoint, headers=headers, json={key: query}, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    return json.dumps(data.get("rows", data), indent=2, default=str)
                
            return f"Database error: Both direct connection and Data API failed. TCP Error: {tcp_err}. API Response: {response.text}"
        except Exception as api_err:
            return f"Database error: Direct connection failed ({tcp_err}) and Data API fallback failed ({api_err})."
    finally:
        if conn: conn.close()

def list_tables() -> str:
    """Lists all tables in the database."""
    query = "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';"
    return execute_query(query)

def get_table_schema(table_name: str) -> str:
    """Returns the schema for a specific table."""
    query = f"SELECT column_name, data_type FROM information_schema.columns WHERE table_name = '{table_name}';"
    return execute_query(query)

def database_manager(parameters: dict, player=None, **kwargs) -> str:
    params = parameters or {}
    action = params.get("action", "query").lower()
    query  = params.get("query", "")
    table  = params.get("table_name", "")

    if player:
        player.write_log(f"[Database] Action: {action}")

    try:
        if action == "list_tables":
            return list_tables()
        if action == "get_schema" and table:
            return get_table_schema(table)
        if action == "query" and query:
            return execute_query(query)
        
        return "Invalid action or missing parameters for database_manager."
    except Exception as e:
        return f"Database Manager Error: {e}"
