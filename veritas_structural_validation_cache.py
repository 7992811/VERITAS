"""Memoize static proof verification, never quote freshness or entry admission.

The full supplied event, source and defaults are fingerprinted on EVERY call.
Changing a nested price, contract, policy or digest causes a miss. We retain
only a SHA256 and a successful two-scalar result, never an event/price graph.
Pickle is used as an in-memory fingerprint only and is never deserialized.
"""
from collections import OrderedDict
import hashlib
import pickle
from threading import RLock

MAX_ENTRIES = 256
MAX_FINGERPRINT_BYTES = 262144
_lock = RLock()
_cache = OrderedDict()
_hits = _misses = 0


def clear():
    global _hits, _misses
    with _lock:
        _cache.clear()
        _hits = _misses = 0


def snapshot():
    with _lock:
        return {'entries':len(_cache), 'max_entries':MAX_ENTRIES,
                'hits':_hits, 'misses':_misses}


def verify(event, source, *, version, defaults, validator):
    global _hits, _misses
    # Non-dict/opaque inputs keep the original validator's exact failure path.
    key = None
    if type(event) is dict and (source is None or type(source) is dict):
        try:
            data = pickle.dumps((version, defaults, event, source), protocol=5)
            if len(data) <= MAX_FINGERPRINT_BYTES:
                key = hashlib.sha256(data).digest()
            del data
        except (TypeError, ValueError, OverflowError, AttributeError, RecursionError, pickle.PickleError):
            pass
    if key is not None:
        with _lock:
            result = _cache.get(key)
            if result is not None:
                _cache.move_to_end(key)
                _hits += 1
                return dict(result)
    result = validator(event, source)
    with _lock:
        _misses += 1
        if (key is not None and result == {'eligible':True, 'reason':'STRUCTURAL_EVENT_PROOF_VALID'}):
            _cache[key] = dict(result)
            _cache.move_to_end(key)
            while len(_cache) > MAX_ENTRIES:
                _cache.popitem(last=False)
    return result
