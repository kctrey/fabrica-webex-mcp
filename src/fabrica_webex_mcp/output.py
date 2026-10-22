"""Keep tool output small: field selection and a hard character budget."""
import json


def _pick(obj, path: list[str]):
    if not path:
        return obj
    if isinstance(obj, list):
        return [_pick(o, path) for o in obj]
    if isinstance(obj, dict) and path[0] in obj:
        return _pick(obj[path[0]], path[1:])
    return None


def _set(out: dict, path: list[str], value) -> None:
    for p in path[:-1]:
        out = out.setdefault(p, {})
    out[path[-1]] = value


def select(obj, fields: list[str] | None):
    """Keep only dotted-path fields (e.g. "emails", "address.city") from each item."""
    if not fields:
        return obj
    if isinstance(obj, list):
        return [select(o, fields) for o in obj]
    if not isinstance(obj, dict):
        return obj
    out: dict = {}
    for f in fields:
        v = _pick(obj, f.split("."))
        if v is not None:
            _set(out, f.split("."), v)
    return out


def dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str)


def fit(result: dict, max_chars: int) -> str:
    """Serialize result; if too big, drop trailing items and say so."""
    s = dumps(result)
    if len(s) <= max_chars or not isinstance(result.get("items"), list):
        return s if len(s) <= max_chars else s[:max_chars] + '..."[truncated]'
    items = result["items"]
    lo, hi = 0, len(items)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(dumps({**result, "items": items[:mid]})) <= max_chars - 200:
            lo = mid
        else:
            hi = mid - 1
    out = {**result, "items": items[:lo], "truncated": True, "omitted": len(items) - lo,
           "hint": "Use `fields`, narrower filters, or a lower max_items to shrink output."}
    return dumps(out)
