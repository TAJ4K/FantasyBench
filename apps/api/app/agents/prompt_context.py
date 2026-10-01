from __future__ import annotations

import json
from typing import Any

TABLE_GUIDANCE = (
    "Context tables use columns (lists of nested object keys) and rows of values in the same "
    "order. Each row is one record; for example columns=[[\"player_id\"],"
    "[\"performance\",\"season_points\"]], rows=[[\"abc\",10]] means "
    "{\"player_id\":\"abc\",\"performance\":{\"season_points\":10}}. "
    "Null remains unknown, never zero. Use the original decision JSON schema for your response."
)


def compact_context(value: Any) -> Any:
    """Share repeated field names without dropping players, stats, or provenance."""
    if isinstance(value, dict):
        return {key: compact_context(item) for key, item in value.items()}
    if not isinstance(value, list):
        return value
    ordinary = [compact_context(item) for item in value]
    if len(value) < 4 or not _matching_objects(value):
        return ordinary
    columns: list[list[str]] = []

    def visit(items: list[Any], path: list[str]) -> None:
        if _matching_objects(items):
            for key in sorted(items[0]):
                visit([item[key] for item in items], [*path, key])
        else:
            columns.append(path)

    visit(value, [])

    def cell(item: Any, path: list[str]) -> Any:
        for key in path:
            item = item[key]
        return compact_context(item)

    table = {"columns": columns, "rows": [[cell(item, path) for path in columns] for item in value]}
    return table if len(_json(table)) < len(_json(ordinary)) else ordinary


def _matching_objects(items: list[Any]) -> bool:
    return bool(items) and all(
        isinstance(item, dict) and bool(item) and item.keys() == items[0].keys()
        for item in items
    )


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
