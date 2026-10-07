"""Bounded exact-content memo for STATIC proof verification, never admission.

Every lookup fingerprints the full event/source/defaults. A nested mutation
misses. Only a SHA256 plus two scalar result fields are retained. No quote age,
entry expiry, risk allowance, or unverified success is cached. Pickle is used
as an in-memory fingerprint and is never deserialized.
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


def _key(event, source, version, defaults):
    if type(event) is not dict or (source is not None and type(source) is not dict):
        return None
    try:
        data = pickle.dumps((version, defaults, event, source), protocol=5)
        return hashlib.sha256(data).digest() if len(data) <= MAX_FINGERPRINT_BYTES else None
    except (TypeError, ValueError, OverflowError, AttributeError, RecursionError, pickle.PickleError):
        return None


def verify(event, source, *, version, defaults, validator):
    global _hits, _misses
    key = _key(event, source, version, defaults)
    if key is not None:
        with _lock:
            result = _cache.get(key)
            if result is not None:
                _cache.move_to_end(key)
                _hits += 1
                return dict(result)
    result = validator(event, source)
    success = result == {'eligible':True, 'reason':'STRUCTURAL_EVENT_PROOF_VALID'}
    # A shared mutable input changed while its proof was being checked: do not
    # associate the returned result with the earlier fingerprint.
    stable = success and key is not None and key == _key(event, source, version, defaults)
    with _lock:
        _misses += 1
        if stable:
            _cache[key] = dict(result)
            _cache.move_to_end(key)
            while len(_cache) > MAX_ENTRIES:
                _cache.popitem(last=False)
    return result
