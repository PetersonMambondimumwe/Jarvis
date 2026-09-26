from __future__ import annotations

from typing import Any

from core.leaf_ai_client import LeafAIClient, LeafAIError, load_leaf_ai_config

_cached_client: LeafAIClient | None = None


def _get_client() -> LeafAIClient:
    global _cached_client
    if _cached_client is None:
        _cached_client = LeafAIClient(load_leaf_ai_config())
    return _cached_client


def leaf_ai_knowledge(
    parameters: dict,
    player: Any = None,
    client: LeafAIClient | None = None,
    **kwargs: Any,
) -> str:
    """Action handler to query the dedicated Leaf AI knowledge source.

    Connects to the Dify Leaf AI chatbot backend silently in the background,
    retrieving information on Leaf AI and University of Pretoria academic
    support services, coaches, and modules, and returning it naturally.
    """
    params = parameters or {}
    action = str(params.get("action") or "query").strip().lower()

    active_client = client or _get_client()

    try:
        if action in {"verify", "check", "health", "test"}:
            status = active_client.verify()
            if status.get("healthy"):
                return "The Leaf AI knowledge source is online and reachable."
            err = status.get("error") or f"status {status.get('status_code')}"
            return f"The Leaf AI knowledge source is currently degraded ({err})."

        query_text = str(
            params.get("query")
            or params.get("question")
            or params.get("prompt")
            or params.get("topic")
            or params.get("task")
            or ""
        ).strip()

        if not query_text:
            return "Please specify what you would like to know from the Leaf AI knowledge source, sir."

        if player and hasattr(player, "write_log"):
            try:
                player.write_log("[Leaf AI]: Accessing campus knowledge source...")
            except Exception:
                pass

        result = active_client.query(query_text)
        answer = result.get("answer") or ""
        if not answer:
            return "Leaf AI did not return any relevant details for that request, sir."

        return answer

    except LeafAIError as exc:
        return f"Leaf AI knowledge retrieval was unavailable: {exc}"
    except Exception:
        return "I encountered an internal error while querying the Leaf AI knowledge source, sir."
