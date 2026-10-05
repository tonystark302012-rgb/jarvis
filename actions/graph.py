"""graph — tool adapter for memory/graph.py (Graphiti-lite KG).

The implementation lives in `memory.graph` (sqlite, temporal edges,
offline extraction, BFS search); this thin adapter registers it with
JARVIS's one action registry so the model can call `graph` like every
other tool.
"""
from __future__ import annotations

from memory.graph import TOOL, graph, extract, link, search, timeline

__all__ = ["TOOL", "graph", "extract", "link", "search", "timeline"]
