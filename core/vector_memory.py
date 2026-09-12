"""
core/vector_memory.py
─────────────────────
Persistent vector memory backed by ChromaDB (local SQLite — no server needed).

Replaces the old JSON-file + brute-force O(n) cosine search implementation.
ChromaDB stores embeddings in a persistent HNSW index so queries stay fast
even with thousands of entries, and nothing is loaded into RAM on startup.

Embedding model: Gemini gemini-embedding-2 (same as before — we supply our
own vectors so ChromaDB's built-in embedder / onnxruntime is never used).

Migration
---------
On first run, if memory/vector_memory.json exists and has not already been
migrated, all entries are imported into ChromaDB automatically.  The JSON
file is then renamed to vector_memory.json.migrated so the migration never
runs again.

Public API (unchanged — callers are unaffected)
---------
    add_to_vector_memory(text, metadata=None)
    search_vector_memory(query, top_k=5)  -> str
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import time
from typing import Optional


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = get_base_dir()
CHROMA_DB_PATH  = BASE_DIR / "memory" / "chroma_db"
LEGACY_JSON     = BASE_DIR / "memory" / "vector_memory.json"
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


class VectorMemory:
    """ChromaDB-backed vector store with Gemini embeddings."""

    def __init__(self) -> None:
        self._collection = None
        self._gemini_client = None
        self._init_chroma()
        self._init_gemini()

    # ── Initialisation ───────────────────────────────────────────────────────

    def _init_chroma(self) -> None:
        try:
            import chromadb
            from chromadb.config import Settings

            client = chromadb.PersistentClient(
                path=str(CHROMA_DB_PATH),
                settings=Settings(anonymized_telemetry=False),
            )
            # embedding_function=None → we supply our own vectors every time;
            # ChromaDB's built-in embedder (onnxruntime) is never loaded.
            self._collection = client.get_or_create_collection(
                name="jarvis_memory",
                metadata={"hnsw:space": "cosine"},
                embedding_function=None,
            )
            count = self._collection.count()
            print(f"[VectorMemory] ChromaDB ready — {count} entries")
            self._migrate_legacy_json()

        except ImportError:
            print("[VectorMemory] chromadb not installed — vector memory disabled. "
                  "Run: pip install chromadb")
        except Exception as exc:
            print(f"[VectorMemory] ChromaDB init error: {exc}")

    def _init_gemini(self) -> None:
        try:
            from google import genai
            with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
                key = json.load(f)["gemini_api_key"]
            self._gemini_client = genai.Client(api_key=key)
        except Exception as exc:
            print(f"[VectorMemory] Gemini client init error: {exc}")

    # ── One-time migration from the old JSON store ───────────────────────────

    def _migrate_legacy_json(self) -> None:
        if not LEGACY_JSON.exists():
            return
        migrated_marker = LEGACY_JSON.with_suffix(".json.migrated")
        if migrated_marker.exists():
            return  # Already migrated previously.

        try:
            old_entries = json.loads(LEGACY_JSON.read_text(encoding="utf-8"))
            if not isinstance(old_entries, list):
                return

            imported = 0
            for i, entry in enumerate(old_entries):
                text   = entry.get("text", "")
                vector = entry.get("vector", [])
                meta   = entry.get("metadata") or {}
                if text and vector:
                    try:
                        self._collection.add(
                            ids=[f"migrated_{i}"],
                            documents=[text],
                            embeddings=[vector],
                            metadatas=[meta],
                        )
                        imported += 1
                    except Exception:
                        pass  # Skip duplicates / malformed entries.

            print(f"[VectorMemory] Migrated {imported}/{len(old_entries)} entries from JSON → ChromaDB")
            LEGACY_JSON.rename(migrated_marker)

        except Exception as exc:
            print(f"[VectorMemory] Migration error: {exc}")

    # ── Embedding ─────────────────────────────────────────────────────────────

    def _get_embedding(self, text: str) -> Optional[list]:
        if not self._gemini_client:
            return None
        try:
            result = self._gemini_client.models.embed_content(
                model="models/gemini-embedding-2",
                contents=text,
            )
            return result.embeddings[0].values
        except Exception as exc:
            print(f"[VectorMemory] Embedding error: {exc}")
            return None

    # ── Public methods ────────────────────────────────────────────────────────

    def add(self, text: str, metadata: dict = None) -> None:
        if not text.strip() or self._collection is None:
            return
        vector = self._get_embedding(text)
        if not vector:
            return
        uid = f"mem_{int(time.time() * 1000)}"
        try:
            self._collection.add(
                ids=[uid],
                documents=[text],
                embeddings=[vector],
                metadatas=[metadata or {}],
            )
        except Exception as exc:
            print(f"[VectorMemory] Add error: {exc}")

    def search(self, query: str, top_k: int = 5) -> list:
        if self._collection is None or not query.strip():
            return []
        count = self._collection.count()
        if count == 0:
            return []
        vector = self._get_embedding(query)
        if not vector:
            return []
        try:
            results = self._collection.query(
                query_embeddings=[vector],
                n_results=min(top_k, count),
            )
            docs  = results.get("documents", [[]])[0]
            metas = results.get("metadatas", [[]])[0]
            return [{"text": d, "metadata": m} for d, m in zip(docs, metas)]
        except Exception as exc:
            print(f"[VectorMemory] Search error: {exc}")
            return []


# ── Singleton instance ────────────────────────────────────────────────────────

vector_mem = VectorMemory()


def add_to_vector_memory(text: str, metadata: dict = None) -> None:
    vector_mem.add(text, metadata)


def search_vector_memory(query: str, top_k: int = 5) -> str:
    results = vector_mem.search(query, top_k)
    if not results:
        return ""
    output = "\nRelevant past information:\n"
    for r in results:
        output += f"- {r['text']}\n"
    return output
