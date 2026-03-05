import asyncio
import functools
import time
from ddgs import DDGS

# Simple in-memory cache for search results
_SEARCH_CACHE = {}
CACHE_TTL = 3600  # 1 hour

async def web_search(query: str, max_results: int = 3, snippet_chars: int = 300) -> str:
    """Run a DuckDuckGo search and return a formatted results block. Results are cached."""
    query_key = query.strip().lower()
    now = time.time()
    
    if query_key in _SEARCH_CACHE:
        result, expiry = _SEARCH_CACHE[query_key]
        if now < expiry:
            return result
            
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _ddg_search, query, max_results, snippet_chars)
    
    _SEARCH_CACHE[query_key] = (result, now + CACHE_TTL)
    return result


def _ddg_search(query: str, max_results: int, snippet_chars: int) -> str:
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        if not results:
            return "No results found."
        parts = [
            f"[{r['title']}]\n{(r.get('body') or '')[:snippet_chars]}\n{r['href']}"
            for r in results
        ]
        return "--- WEB SEARCH RESULTS ---\n" + "\n\n".join(parts) + "\n---"
    except Exception as e:
        return f"Search error: {e}"
