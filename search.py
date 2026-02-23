import asyncio

from ddgs import DDGS


async def web_search(query: str, max_results: int = 3, snippet_chars: int = 300) -> str:
    """Run a DuckDuckGo search and return a formatted results block."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _ddg_search, query, max_results, snippet_chars)


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
