"""Copy native data graphs without dispatching a function for every scalar.

Only exact builtin dictionaries and lists take the fast path. Immutable scalar
objects retain their identity; subclasses, tuples, datetimes and custom copy
protocols are delegated to Python's deepcopy with the SAME memo. Cycles and
shared mutable children remain preserved. There is no serialization, input
pruning, shared mutable snapshot cache, or change to any trading value.
"""
from copy import deepcopy as _standard_deepcopy

_ATOMIC = frozenset((type(None), bool, int, float, complex, str, bytes))
_MISSING = object()


def deepcopy(value, memo=None):
    """The ordinary two-argument deepcopy interface for internal snapshots."""
    kind = type(value)
    if kind in _ATOMIC:
        return value
    if memo is None:
        memo = {}
    existing = memo.get(id(value), _MISSING)
    if existing is not _MISSING:
        return existing
    if kind is dict:
        result = {}
        memo[id(value)] = result
        for key, child in value.items():
            # Keep Python's assignment evaluation order: value before key.
            result[key if type(key) in _ATOMIC else deepcopy(key, memo)] = (
                child if type(child) in _ATOMIC else deepcopy(child, memo))
    elif kind is list:
        result = []
        memo[id(value)] = result
        for child in value:
            result.append(child if type(child) in _ATOMIC else deepcopy(child, memo))
    else:
        return _standard_deepcopy(value, memo)
    # Retain original objects for the lifetime of the memo, so object IDs
    # cannot be recycled into false aliases while a larger graph is copied.
    try:
        memo[id(memo)].append(value)
    except KeyError:
        memo[id(memo)] = [value]
    return result
