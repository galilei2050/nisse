"""Small text helpers shared across domains — no domain logic of its own."""


def match_unique(items: list[str], term: str) -> list[str]:
    """Items a removal term hits: the exact (case-insensitive) match if any, else substring matches.

    The caller removes only when this returns exactly one (unique); >1 is ambiguous, 0 is missing.
    Used by both lists (`list_edit`) and core memory (`update_core_memory`) for remove-by-fragment.
    """
    low = term.lower()
    exact = [i for i in items if i.lower() == low]
    return exact or [i for i in items if low in i.lower()]
