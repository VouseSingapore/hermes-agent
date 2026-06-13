#!/usr/bin/env python3
"""Curated memory search tools for Xuanyue/Tifa.

Thin wrappers around the local curated-vault search implementation in
``scripts/curation/memory_search.py``.  These provide short, unprefixed tool
names (``search_memory`` and ``get_memory_note``) in Hermes sessions, while the
same implementation is also exposed through the ``tifa-memory`` MCP server.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from tools.registry import registry

_CURATIONS_DIR = Path("C:/Users/Yanjie/AppData/Local/hermes/scripts/curation")


def _check_requirements() -> bool:
    return (_CURATIONS_DIR / "memory_search.py").exists()


def _load_memory_search():
    cur = str(_CURATIONS_DIR)
    if cur not in sys.path:
        sys.path.insert(0, cur)
    import memory_search  # type: ignore

    return memory_search


def _json_result(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def search_memory(query: str, limit: int = 5, task_id: str | None = None) -> str:
    """Search Xuanyue's curated memory vault and PM docs."""
    q = (query or "").strip()
    if not q:
        return _json_result({"error": "query is required"})
    try:
        lim = int(limit or 5)
    except Exception:
        lim = 5
    lim = max(1, min(lim, 20))
    try:
        memory_search = _load_memory_search()
        return _json_result({"query": q, "results": memory_search.search(q, lim)})
    except Exception as exc:
        return _json_result({"error": str(exc) or repr(exc)})


def get_memory_note(slug: str, task_id: str | None = None) -> str:
    """Return the full text of a curated memory note by slug."""
    s = (slug or "").strip()
    if not s:
        return _json_result({"error": "slug is required"})
    try:
        memory_search = _load_memory_search()
        note = memory_search.get_note(s)
        if note is None:
            return _json_result({"error": f"memory note not found: {s}", "slug": s})
        return _json_result({"slug": s, "text": note})
    except Exception as exc:
        return _json_result({"error": str(exc) or repr(exc), "slug": s})


registry.register(
    name="search_memory",
    toolset="memory",
    schema={
        "name": "search_memory",
        "description": (
            "Search Xuanyue's curated memory vault and PM docs. Prefer this for "
            "standards, preferences, lessons, how-tos, capabilities, and project "
            "status. Returns slug/type/domain/status/path/snippet; use "
            "get_memory_note(slug) for full text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "limit": {
                    "type": "integer",
                    "description": "Maximum results to return (1-20).",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    handler=lambda args, **kw: search_memory(
        query=args.get("query", ""),
        limit=args.get("limit", 5),
        task_id=kw.get("task_id"),
    ),
    check_fn=_check_requirements,
    description="Search curated memory vault and PM docs",
)

registry.register(
    name="get_memory_note",
    toolset="memory",
    schema={
        "name": "get_memory_note",
        "description": "Return the full text of a curated memory note by slug from search_memory results.",
        "parameters": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "The note slug returned by search_memory."}
            },
            "required": ["slug"],
        },
    },
    handler=lambda args, **kw: get_memory_note(
        slug=args.get("slug", ""),
        task_id=kw.get("task_id"),
    ),
    check_fn=_check_requirements,
    description="Fetch a curated memory note by slug",
)
