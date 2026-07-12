"""Provenance helpers — every derived artifact names its sources and parameters."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path | str) -> str:
    return sha256_bytes(Path(path).read_bytes())


def sha256_array(arr) -> str:
    """Stable content hash for a numpy array (dtype + shape + bytes)."""
    import numpy as np

    a = np.ascontiguousarray(arr)
    payload = f"{a.dtype.str}|{a.shape}|".encode() + a.tobytes()
    return sha256_bytes(payload)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def provenance(
    *,
    kind: str,
    source_hashes: list[str],
    parameters: dict[str, Any],
    notes: str | None = None,
) -> dict[str, Any]:
    """Build a serializable provenance record for a derived product."""
    record: dict[str, Any] = {
        "kind": kind,
        "experimental": True,
        "disclaimer": (
            "Experimental reflectivity-only analysis. Not a severe-weather forecast. "
            "Reflectivity imagery alone cannot support rotation or tornado inference."
        ),
        "created_at": utc_now_iso(),
        "source_hashes": list(source_hashes),
        "parameters": dict(parameters),
    }
    if notes:
        record["notes"] = notes
    return record


def to_jsonable(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    try:
        import numpy as np

        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, np.generic):
            return obj.item()
    except ImportError:
        pass
    return obj


def dumps_json(obj: Any, *, indent: int = 2) -> str:
    return json.dumps(to_jsonable(obj), indent=indent, sort_keys=True)
