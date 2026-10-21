"""Display text of fact values for the site views: the archive's strings, numbers and dict / list shapes."""

from __future__ import annotations

from typing import Any


def text_of(value: Any, limit: int = 400) -> str | None:
    """Short display text of a fact value (string, number, or the archive's dict / list shapes):
    a permit as 'agency number (type)', a tranche or facility as 'name $X.XB', else the dict's own
    text field, else its scalar and list entries."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int | float):
        return f"{value:,}" if isinstance(value, int) else f"{value:,.2f}".rstrip("0").rstrip(".")
    if isinstance(value, str):
        return value[:limit] if value.strip() else None
    if isinstance(value, dict):
        return _dict_text(value, limit)
    if isinstance(value, list):
        parts = [t for t in (text_of(v, 160) for v in value) if t]
        return "; ".join(parts)[:limit] or None
    return str(value)[:limit]


def _money(usd: Any) -> str | None:
    return f"${usd / 1e9:,.2f}B".replace(".00B", ".0B") if isinstance(usd, int | float) and usd > 0 else None


def _dict_text(value: dict[str, Any], limit: int) -> str | None:
    if value.get("agency") and value.get("number"):  # an air permit
        kind = f" ({value['type']})" if value.get("type") else ""
        return f"{value['agency']} {value['number']}{kind}"[:limit]
    name = next((value[k] for k in ("tranche", "name", "project") if isinstance(value.get(k), str)), None)
    size = _money(value.get("amount_usd") or value.get("size_usd") or value.get("commitment_usd"))
    drawn = _money(value.get("drawn_usd"))
    if name and (size or drawn):
        parts = [name, size or "size n/d"] + ([f"drawn {drawn}"] if drawn else [])
        return " ".join(parts)[:limit]
    for key in ("short", "text", "description", "detail", "what", "name", "title", "value"):
        if isinstance(value.get(key), str) and value[key].strip():
            return value[key][:limit]
    parts = []
    for k, v in value.items():
        if isinstance(v, str | int | float) and not isinstance(v, bool):
            parts.append(f"{k}: {text_of(v, 80)}")
        elif isinstance(v, list) and v and all(isinstance(x, str) for x in v):
            parts.append(f"{k}: {', '.join(v)}")
    return "; ".join(parts)[:limit] or None
