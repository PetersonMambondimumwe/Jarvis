import json
import numpy as np
from pathlib import Path
import sys
import time

def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR = get_base_dir()
VECTOR_DB_PATH = BASE_DIR / "memory" / "vector_memory.json"
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

class VectorMemory:
    def __init__(self):
        self.db_path = VECTOR_DB_PATH
        self.embeddings = [] # List of { "text": str, "vector": list, "metadata": dict }
        self._load()
        self.client = None
        self._init_client()

    def _init_client(self):
        try:
            from google import genai
            with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
                key = json.load(f)["gemini_api_key"]
            self.client = genai.Client(api_key=key)
        except Exception as e:
            print(f"[VectorMemory] Init error: {e}")

    def _load(self):
        if self.db_path.exists():
            try:
                self.embeddings = json.loads(self.db_path.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"[VectorMemory] Load error: {e}")
                self.embeddings = []

    def _save(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.write_text(json.dumps(self.embeddings, indent=2, ensure_ascii=False), encoding="utf-8")

    def _get_embedding(self, text: str) -> list:
        if not self.client:
            return []
        try:
            # Use Gemini's embedding model
            result = self.client.models.embed_content(
                model="models/gemini-embedding-2",
                contents=text
            )
            return result.embeddings[0].values
        except Exception as e:
            print(f"[VectorMemory] Embedding error: {e}")
            return []

    def add(self, text: str, metadata: dict = None):
        if not text.strip():
            return
        
        vector = self._get_embedding(text)
        if not vector:
            return

        self.embeddings.append({
            "text": text,
            "vector": vector,
            "metadata": metadata or {},
            "timestamp": time.time()
        })
        self._save()

    def search(self, query: str, top_k: int = 5) -> list:
        if not self.embeddings or not query.strip():
            return []

        query_vector = self._get_embedding(query)
        if not query_vector:
            return []

        # Simple cosine similarity
        results = []
        q_vec = np.array(query_vector)
        
        for entry in self.embeddings:
            e_vec = np.array(entry["vector"])
            similarity = np.dot(q_vec, e_vec) / (np.linalg.norm(q_vec) * np.linalg.norm(e_vec))
            results.append((similarity, entry))

        # Sort by similarity descending
        results.sort(key=lambda x: x[0], reverse=True)
        return [r[1] for r in results[:top_k]]

# Singleton instance
vector_mem = VectorMemory()

def add_to_vector_memory(text: str, metadata: dict = None):
    vector_mem.add(text, metadata)

def search_vector_memory(query: str, top_k: int = 5) -> str:
    results = vector_mem.search(query, top_k)
    if not results:
        return ""
    
    output = "\nRelevant past information:\n"
    for r in results:
        output += f"- {r['text']}\n"
    return output
