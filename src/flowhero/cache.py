"""Session-local, byte-bounded LRU for parsed samples and derived facts."""
from collections import OrderedDict
import sys
import numpy as np


def retained_size(value, seen=None):
    seen = set() if seen is None else seen
    if id(value) in seen:
        return 0
    seen.add(id(value))
    size = sys.getsizeof(value)
    if isinstance(value, np.ndarray):
        return size + (retained_size(value.base, seen) if value.base is not None else 0)
    if isinstance(value, dict):
        return size + sum(retained_size(k, seen) + retained_size(v, seen) for k, v in value.items())
    if isinstance(value, (tuple, list)):
        return size + sum(retained_size(v, seen) for v in value)
    if hasattr(value, '__dict__'):
        return size + retained_size(vars(value), seen)
    return size


class AnalysisCache:
    def __init__(self, max_bytes=256 * 1024 * 1024):
        self.max_bytes = max_bytes
        self.entries = OrderedDict()
        self.bytes = 0
        self.counts = {name: {'hit': 0, 'miss': 0} for name in ('sample', 'gate', 'statistic', 'plot')}

    def get(self, layer, key, produce):
        key = (layer, key)
        if key in self.entries:
            self.counts[layer]['hit'] += 1
            self.entries.move_to_end(key)
            return self.entries[key][0]
        self.counts[layer]['miss'] += 1
        value = produce()
        size = retained_size((key, value))
        if size <= self.max_bytes:
            while self.entries and self.bytes + size > self.max_bytes:
                _, (_, removed) = self.entries.popitem(last=False)
                self.bytes -= removed
            self.entries[key] = (value, size)
            self.bytes += size
        return value
