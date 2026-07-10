#web_search.py
import json
import sys
import threading
from pathlib import Path

def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


def _get_api_keys() -> dict:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _gemini_search(query: str) -> str:
    from google import genai

    client   = genai.Client(api_key=_get_api_keys()["gemini_api_key"])
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=query,
        config={"tools": [{"google_search": {}}]},
    )

    text = ""
    for part in response.candidates[0].content.parts:
        if hasattr(part, "text") and part.text:
            text += part.text

    text = text.strip()
    if not text:
        raise ValueError("Gemini returned an empty response.")
    return text


def _openai_synthesize(prompt: str) -> str:
    """Uses OpenAI's cheapest model (gpt-4o-mini) for synthesis."""
    try:
        from openai import OpenAI
        keys = _get_api_keys()
        if "openai_api_key" not in keys:
            return ""
        
        client = OpenAI(api_key=keys["openai_api_key"])
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"[WebSearch] ⚠️ OpenAI synthesis failed: {e}")
        return ""


def _ddg_search(query: str, max_results: int = 6) -> list[dict]:
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS

    results = []
    with DDGS() as ddgs:
        for r in ddgs.text(query, max_results=max_results):
            results.append({
                "title":   r.get("title",  ""),
                "snippet": r.get("body",   ""),
                "url":     r.get("href",   ""),
            })
    return results


def _ddg_news(query: str, max_results: int = 8) -> list[dict]:
    """DDG news search — returns actual articles, not website homepages."""
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS

    results = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.news(query, max_results=max_results):
                results.append({
                    "title":   r.get("title",  ""),
                    "snippet": r.get("body",   ""),
                    "url":     r.get("url",    ""),
                    "source":  r.get("source", ""),
                })
    except Exception as e:
        print(f"[WebSearch] ⚠️ DDG news() failed ({e}) — falling back to text search")
        results = _ddg_search(query, max_results=max_results)
    return results


def _format_ddg(query: str, results: list[dict]) -> str:
    if not results:
        return f"No results found for: {query}"

    lines = [f"Search results for: {query}\n"]
    for i, r in enumerate(results, 1):
        if r.get("title"):   lines.append(f"{i}. {r['title']}")
        if r.get("snippet"): lines.append(f"   {r['snippet']}")
        if r.get("url"):     lines.append(f"   Source: {r['url']}")
        lines.append("")
    return "\n".join(lines).strip()


def _format_news(query: str, results: list[dict]) -> str:
    if not results:
        return f"No news found for: {query}"

    lines = [f"Latest news: {query}\n"]
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        if not title:
            continue
        src = f"  [{r['source']}]" if r.get("source") else ""
        lines.append(f"{i}. {title}{src}")
        if r.get("snippet"):
            lines.append(f"   {r['snippet'][:140]}")
        if r.get("url"):
            lines.append(f"   {r['url']}")
        lines.append("")
    return "\n".join(lines).strip()


# ── Briefing helper ────────────────────────────────────────────────────────────

def _gemini_headlines(n: int = 5) -> tuple[list[str], str]:
    """
    Fetches current headlines via Gemini grounded search.
    Optimised for speed: minimal prompt + strict token cap.
    Returns (headline_list, raw_text_for_display).
    """
    import re
    from google import genai

    client = genai.Client(api_key=_get_api_keys()["gemini_api_key"])
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=f"Current world news: {n} headlines. Numbered list, titles only.",
        config={"tools": [{"google_search": {}}]},
    )

    raw = ""
    for part in response.candidates[0].content.parts:
        if hasattr(part, "text") and part.text:
            raw += part.text

    headlines = []
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        # Only accept lines that begin with a number — skips preamble/closing sentences
        if not re.match(r'^[\d]+[.\)\-]', line):
            continue
        clean = re.sub(r'^[\d]+[.\)\-]\s*', '', line)
        clean = re.sub(r'^\*+\s*',          '', clean).strip()
        if clean and len(clean) > 10:
            headlines.append(clean)

    return headlines[:n], raw.strip()


# ── Modes ──────────────────────────────────────────────────────────────────────

def _search(query: str) -> str:
    """Default search — Gemini grounded, DDG fallback."""
    try:
        return _gemini_search(query)
    except Exception as e:
        print(f"[WebSearch] ⚠️ Gemini failed ({e}) — trying DDG...")
        results = _ddg_search(query)
        return _format_ddg(query, results)


def _news(query: str) -> str:
    """
    Runs Gemini grounded search AND DDG news in parallel.
    Returns whichever delivers a valid result first; cancels the other.
    """
    gemini_query = f"latest news today: {query}" if query else "top world news today"
    ddg_query    = query if query else "world news today"

    result_box  = [None]   # first valid result lands here
    lock        = threading.Lock()
    done_evt    = threading.Event()
    failures    = [0]

    def _store(r: str) -> None:
        if r and len(r) > 60:
            with lock:
                if result_box[0] is None:
                    result_box[0] = r
            done_evt.set()
        else:
            with lock:
                failures[0] += 1
                if failures[0] >= 2:   # both failed — unblock caller
                    done_evt.set()

    def _try_gemini():
        try:
            _store(_gemini_search(gemini_query))
        except Exception as e:
            print(f"[WebSearch] ⚠️ Gemini news failed ({e})")
            _store("")

    def _try_ddg():
        try:
            results = _ddg_news(ddg_query, max_results=8)
            _store(_format_news(ddg_query, results))
        except Exception as e:
            print(f"[WebSearch] ⚠️ DDG news failed ({e})")
            _store("")

    threading.Thread(target=_try_gemini, daemon=True).start()
    threading.Thread(target=_try_ddg,    daemon=True).start()

    done_evt.wait(timeout=10.0)
    return result_box[0] or f"No news found for: {query}"


def _research(query: str) -> str:
    """
    Deep dive — asks Gemini for a comprehensive answer with context.
    Falls back to a wider DDG fetch.
    """
    research_query = (
        f"Comprehensive, detailed explanation of: {query}. "
        "Include background context, key facts, current state, and important nuances."
    )
    try:
        # For research, try OpenAI synthesis if available
        synthesis_prompt = (
            f"Write a comprehensive research report on: '{query}'. "
            "Base it on your internal knowledge and ensure it is structured professionally."
        )
        oa_res = _openai_synthesize(synthesis_prompt)
        if oa_res:
            return oa_res
        
        return _gemini_search(research_query)
    except Exception as e:
        print(f"[WebSearch] ⚠️ Research failed ({e}) — DDG fallback...")
        results = _ddg_search(query, max_results=10)
        return _format_ddg(query, results)


def _deep_search(query: str) -> str:
    """
    Deep Search — Performs multi-stage search and synthesis.
    1. Initial search to find key aspects.
    2. Targeted searches for each aspect.
    3. Final synthesis by OpenAI (fallback to Gemini).
    """
    print(f"[WebSearch] 🚀 Deep Search initiated: {query}")
    
    # Stage 1: Get key sub-topics/aspects
    try:
        from google import genai
        client = genai.Client(api_key=_get_api_keys()["gemini_api_key"])
        
        planner_prompt = (
            f"I need to perform a deep search on: '{query}'. "
            "List 3-4 specific sub-topics or search queries that would provide a comprehensive understanding. "
            "Format: One query per line, no numbering."
        )
        
        plan_resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=planner_prompt
        )
        sub_queries = [q.strip() for q in plan_resp.text.strip().split("\n") if q.strip()][:4]
        if not sub_queries:
            sub_queries = [query]
    except Exception:
        sub_queries = [query]

    # Stage 2: Gather data for each sub-query
    aggregated_context = []
    
    def _fetch_sub(q: str):
        try:
            res = _ddg_search(q, max_results=4)
            aggregated_context.append(f"--- Results for: {q} ---\n" + _format_ddg(q, res))
        except Exception:
            pass

    threads = []
    for q in sub_queries:
        t = threading.Thread(target=_fetch_sub, args=(q,))
        t.start()
        threads.append(t)
    
    for t in threads:
        t.join(timeout=5)

    # Stage 3: Final Synthesis (Prefer OpenAI)
    synthesis_prompt = (
        f"Perform a DEEP ANALYSIS on the topic: '{query}'.\n\n"
        "Here is the gathered research data:\n"
        + "\n\n".join(aggregated_context) + "\n\n"
        "Synthesize this into a comprehensive report. Use professional tone, include key facts, "
        "and organize with clear sections. If sources are provided, mention them."
    )
    
    # Try OpenAI first
    oa_res = _openai_synthesize(synthesis_prompt)
    if oa_res:
        print("[WebSearch] ✅ OpenAI synthesis successful")
        return oa_res
    
    # Fallback to Gemini
    try:
        return _gemini_search(synthesis_prompt)
    except Exception as e:
        return f"Deep Search Synthesis failed: {e}\n\nAggregated Data:\n" + "\n".join(aggregated_context[:5])


def _price(query: str) -> str:
    """Product price lookup — searches for current market prices."""
    price_query = f"current price of {query} — how much does it cost today"
    try:
        return _gemini_search(price_query)
    except Exception as e:
        print(f"[WebSearch] ⚠️ Price Gemini failed ({e}) — DDG fallback...")
        results = _ddg_search(f"{query} price buy", max_results=6)
        return _format_ddg(query, results)


def _compare(items: list[str], aspect: str) -> str:
    query = (
        f"Compare {', '.join(items)} in terms of {aspect}. "
        "Give specific facts and data."
    )
    try:
        # Try OpenAI for comparison synthesis
        oa_res = _openai_synthesize(query)
        if oa_res:
            return oa_res
            
        return _gemini_search(query)
    except Exception as e:
        print(f"[WebSearch] ⚠️ Comparison failed: {e} — falling back to DDG")

    all_results: dict[str, list] = {}
    for item in items:
        try:
            all_results[item] = _ddg_search(f"{item} {aspect}", max_results=3)
        except Exception:
            all_results[item] = []

    lines = [f"Comparison — {aspect.upper()}", "─" * 40]
    for item in items:
        lines.append(f"\n▸ {item}")
        for r in all_results.get(item, [])[:2]:
            if r.get("snippet"):
                lines.append(f"  • {r['snippet']}")
            if r.get("url"):
                lines.append(f"    {r['url']}")
    return "\n".join(lines)


# ── Public entry point ─────────────────────────────────────────────────────────

def web_search(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    query  = params.get("query", "").strip()
    mode   = params.get("mode",  "search").lower().strip()
    items  = params.get("items", [])
    aspect = params.get("aspect", "general").strip() or "general"

    if not query and not items:
        return "Please provide a search query."

    if items and mode not in ("compare",):
        mode = "compare"

    if player:
        player.write_log(f"[Search:{mode}] {query or ', '.join(items)}")

    print(f"[WebSearch] 🔍 mode={mode!r}  query={query!r}")

    try:
        if mode == "compare" and items:
            return _compare(items, aspect)
        if mode == "news":
            return _news(query)
        if mode == "research":
            return _research(query)
        if mode == "deep":
            return _deep_search(query)
        if mode == "price":
            return _price(query)
        return _search(query)

    except Exception as e:
        print(f"[WebSearch] ❌ All backends failed: {e}")
        return f"Search failed: {e}"
