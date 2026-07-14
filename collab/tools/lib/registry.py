"""registry — the ``collabs.json`` name→root registry (collab-kit slice 3, §A2).

Maps collab names to roots so ``handoff <name> …`` and cross-collab ``status`` work. Writes are
atomic (tmp + ``os.replace`` via ``collab_common.safe_write``) under a coarse ``collab_lock``, so a
concurrent ``register`` can never corrupt the registry ([C18]). stdlib only.

Schema::

    {"version": 1, "collabs": {"<name>": {root, repo, reviewer, created, guardrails}}}
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collab_common as cc
import handoff_core as hc

REGISTRY_VERSION = 1


def _home(home=None) -> Path:
    return (Path(home) if home else cc.resolve_collab_home()).expanduser().resolve()


def _registry_path(home=None) -> Path:
    return _home(home) / "collabs.json"


def _lockdir(home=None) -> Path:
    d = _home(home) / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / ".registrylock"


def load(home=None) -> dict:
    """Load the registry.

    Distinguishes the three cases so a bad byte can't destroy the registry:
      * **absent** (``FileNotFoundError``) → a fresh empty registry (normal first use);
      * **transiently locked** (``PermissionError`` — a concurrent writer's ``os.replace`` on
        Windows) → bounded retry, then raise;
      * **corrupt** (invalid JSON) → **raise** ``CollabError`` rather than silently returning empty,
        because ``register`` would otherwise overwrite every prior entry (silent data loss).
    """
    p = _registry_path(home)
    text = None
    for attempt in range(5):
        try:
            text = p.read_text("utf-8")
            break
        except FileNotFoundError:
            return {"version": REGISTRY_VERSION, "collabs": {}}
        except PermissionError as exc:
            if attempt == 4:
                raise cc.CollabError(
                    f"could not read {p} after retries (locked by a concurrent writer?)"
                ) from exc
            time.sleep(0.02 * (2**attempt))
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise cc.CollabError(
            f"registry {p} is corrupt (invalid JSON) — refusing to proceed and overwrite it; "
            f"inspect/repair or remove it"
        ) from exc
    data.setdefault("version", REGISTRY_VERSION)
    data.setdefault("collabs", {})
    return data


def register(name, root, *, repo=None, reviewer=None, guardrails=None, created=None, home=None) -> dict:
    """Add/update a collab entry. Atomic read-modify-write under the registry lock ([C18])."""
    slug = cc.slugify(name)
    entry = {
        "root": str(Path(root).expanduser().resolve()),
        "repo": repo,
        "reviewer": reviewer,
        "created": created or time.strftime("%Y-%m-%d", time.gmtime()),
        "guardrails": list(guardrails or []),
    }
    with cc.collab_lock(_lockdir(home), ttl=10.0, acquire_timeout=30.0) as h:
        data = load(home)  # read under lock
        data["collabs"][slug] = entry
        h.assert_current()  # fence before the atomic commit
        cc.safe_write(_registry_path(home), json.dumps(data, indent=2, sort_keys=True))
    return {"name": slug, **entry}


def resolve(name, home=None):
    """Return the root Path for a registered collab name, or None."""
    ent = load(home)["collabs"].get(cc.slugify(name))
    return Path(ent["root"]) if ent else None


def status(home=None) -> list[dict]:
    """Cross-collab overview: per collab, handoff counts by state + oldest-pending age (§A5)."""
    out = []
    for name, ent in sorted(load(home)["collabs"].items()):
        root = Path(ent["root"])
        counts: dict[str, int] = {}
        oldest_pending_age = None
        try:
            for h in hc.list_handoffs(root):
                counts[h["state"]] = counts.get(h["state"], 0) + 1
            pend = hc.list_handoffs(root, "pending")
            if pend:
                oldest = min(os.path.getmtime(h["path"]) for h in pend)
                oldest_pending_age = round(time.time() - oldest, 1)
        except cc.CollabError:
            pass  # unreadable/absent collab root — report it with empty counts
        out.append(
            {
                "name": name,
                "root": str(root),
                "counts": counts,
                "oldest_pending_age_s": oldest_pending_age,
                "reviewer": ent.get("reviewer"),
                "guardrails": ent.get("guardrails"),
            }
        )
    return out
