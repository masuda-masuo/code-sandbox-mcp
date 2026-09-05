"""Read saved foreground output without repeating commands."""
from __future__ import annotations

import json

from sunaba.output_store import OUTPUT_STORE


def read_output(container_id: str, output_id: str, offset: int = 0,
                limit: int = 100, tail_lines: int | None = None) -> str:
    """Read a saved sandbox_exec output page without rerunning it.
    IDs expire after 1 hour, eviction, or server restart.

    Args:
        container_id: Same ID used for the exec.
        output_id: ID returned by sandbox_exec.
        offset: Zero-based line offset.
        limit: Page size, 1..1000 lines.
        tail_lines: Last N lines, 1..1000; requires offset=0.
    """
    return json.dumps(OUTPUT_STORE.read(
        container_id, output_id, offset=offset, limit=limit, tail_lines=tail_lines,
    ), ensure_ascii=False)
