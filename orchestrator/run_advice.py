"""The facts both attendee-facing reporters read from a run record, decided once.

``watch_run.py`` and ``progress.py`` both turn a saved run into "what to do now",
and an independent review of their first versions found they disagreed with each
other and with the engine in exactly the places a stuck attendee follows literally:

- a coordinator recycled while Kiro was writing its check leaves a record whose only
  pull request is in ``work_items[*].pr``, because ``role_prs`` is filled later, so
  both reporters said "no pull request opened, resubmit" while a PR was open;
- every stopped run with a PR got the red-gate advice ("merge it yourself, or rerun
  with a simpler direction"), including a review outage, where the engine says not
  to rebuild, and a quota stop, where it says not to resubmit now;
- a Lab 3 Chat build got Lab 2's "rerun Run a Build step 1", which would start a new
  game on top of the merged one;
- the suggested checkout folder ignored which lab the run belonged to.

So the reading lives here, once. Reporting only: stdlib, no model, no dispatch, no
network, and nothing is written. The worst a bug here can do is describe a run badly.
"""
from __future__ import annotations

import os

# A new GitHub repository's own starter files. A folder holding only these is the
# repository's initial commit, not a merged game, so replacing it loses nothing.
TEMPLATE_NAMES = frozenset({
    ".git", ".gitignore", ".gitattributes", ".claude", ".ds_store", ".workshop-checkout",
    "readme", "readme.md", "readme.txt", "readme.rst",
    "license", "license.md", "license.txt", "copying",
})

# The only reasons for which "merge it yourself, or rerun with a simpler direction"
# is the right advice: the executable check stayed red, or the review still had
# findings, after the one bounded repair (engine.py `_finalize`).
_RED_AFTER_REPAIR = frozenset({"ITERATION_CAP", "GATE_RED"})


def pr_urls(rec: dict) -> list[str]:
    """Every pull request URL the record names, oldest source first, no duplicates."""
    found: list[str] = []

    def add(url) -> None:
        if isinstance(url, str) and url.strip() and url.strip() not in found:
            found.append(url.strip())

    for row in rec.get("role_prs") or []:
        if isinstance(row, dict):
            add(row.get("pr_url") or row.get("url"))
    items = rec.get("work_items") or {}
    for item in (items.values() if isinstance(items, dict) else items):
        if isinstance(item, dict) and isinstance(item.get("pr"), dict):
            add(item["pr"].get("pr_url"))
    if isinstance(rec.get("pr"), dict):
        add(rec["pr"].get("pr_url"))
    add(rec.get("pr_url"))
    return found


def pr_rows(rec: dict) -> list[dict]:
    """``pr_urls`` with each URL's recorded ``role_prs`` state and error, when known."""
    known = {}
    for row in rec.get("role_prs") or []:
        if isinstance(row, dict) and (row.get("pr_url") or row.get("url")):
            known.setdefault((row.get("pr_url") or row.get("url")).strip(), row)
    return [{**known.get(url, {}), "url": url} for url in pr_urls(rec)]


def is_chat_build(rec: dict) -> bool:
    """Agent Studio Chat records its signed-in submitter; the Lab 2 CLI path does not."""
    return bool(str(rec.get("submitted_by") or "").strip())


def red_after_repair(rec: dict) -> bool:
    """True only when every blocked pull request stayed red after its one repair."""
    if not str(rec.get("fail_reason") or "").startswith("ROLE_PR_BLOCKED"):
        return False
    blocked = [row for row in rec.get("role_prs") or []
               if isinstance(row, dict) and row.get("state") == "blocked"]
    return bool(blocked) and all(row.get("error") in _RED_AFTER_REPAIR for row in blocked)


def holds_app(directory: str) -> tuple[bool | None, int]:
    """(None, 0) when absent; (False, 0) for only starter files; else (True, file count)."""
    try:
        names = [n for n in os.listdir(directory) if n.lower() not in TEMPLATE_NAMES]
    except (FileNotFoundError, NotADirectoryError):
        return None, 0
    return bool(names), len(names)


def checkout_target(rec: dict, home: str) -> tuple[str, bool]:
    """(``~/<dir>``, already) for this run's checkout.

    The pages name the folder by lab: ``~/game`` for the Lab 2 build and
    ``~/game-lab3`` for the Lab 3 fix. ``github.py checkout`` REPLACES its
    destination, so a folder that already holds an app is never suggested: the
    caller says it is already checked out and offers a separate, unused folder.
    A folder holding only the repository's starter files is safe to replace.
    """
    natural = "game-lab3" if is_chat_build(rec) else "game"
    has, _ = holds_app(os.path.join(home, natural))
    if not has:
        return f"~/{natural}", False
    suffix = (str(rec.get("run_id") or "").rsplit("_", 1)[-1] or "new")[:6]
    for name in [f"game-{suffix}"] + [f"game-{suffix}-{n}" for n in range(2, 100)]:
        if not os.path.exists(os.path.join(home, name)):
            return f"~/{name}", True
    return f"~/game-{suffix}-new", True


def port_for(target: str) -> int | None:
    """The port the pages start each folder on; None for any other folder."""
    return {"~/game": 8000, "~/game-lab3": 8001}.get(target)
