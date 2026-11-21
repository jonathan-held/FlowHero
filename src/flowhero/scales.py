"""Axis and gate coordinate scales: ``linear``, ``log`` (base 10) and logicle.

A scale is a short text so that every gate and plot states its own coordinates:
``linear``, ``log`` or ``logicle:T:W:M:A`` with the Gating-ML 2.0 parameters
(Moore and Parks, Cytometry A 2012). The logicle arithmetic is FlowUtils', the
same code FlowKit uses when it evaluates a gate, so display and membership agree.
Statistics never use these functions: they are computed on untransformed values.
"""
import math
import numpy as np

LOGICLE_DEFAULTS = (262144.0, 0.5, 4.5, 0.0)


def _number(value):
    return repr(float(value)) if float(value) != int(float(value)) else str(int(float(value)))


def logicle_scale(t=LOGICLE_DEFAULTS[0], w=LOGICLE_DEFAULTS[1], m=LOGICLE_DEFAULTS[2], a=LOGICLE_DEFAULTS[3]):
    scale = 'logicle:' + ':'.join(_number(v) for v in (t, w, m, a))
    parse(scale)
    return scale


def parse(scale):
    """Return ``(kind, parameters)`` or raise ValueError."""
    if scale in ('linear', 'log'):
        return scale, ()
    if isinstance(scale, str) and scale.startswith('logicle:'):
        try:
            t, w, m, a = (float(part) for part in scale.split(':')[1:])
        except ValueError:
            raise ValueError('A logicle scale needs four numbers: T, W, M and A') from None
        if not all(math.isfinite(v) for v in (t, w, m, a)) or t <= 0 or m <= 0 or w < 0 or 2 * w > m or -a > w or a + w > m - 2 * w:
            raise ValueError('Logicle parameters need T > 0, M > 0, 0 <= W <= M/2 and -W <= A <= M - 3W')
        if scale != 'logicle:' + ':'.join(_number(v) for v in (t, w, m, a)):
            raise ValueError('Logicle scale text is not in its canonical form')
        return 'logicle', (t, w, m, a)
    raise ValueError('Axes must declare linear, log or logicle scales')


def valid(scale):
    try:
        parse(scale)
        return True
    except ValueError:
        return False


def kind(scale):
    return parse(scale)[0]


def requires_positive(scale):
    return scale == 'log'


def forward(values, scale):
    """Original units to scale coordinates. Log of a nonpositive value is NaN."""
    name, parameters = parse(scale)
    values = np.asarray(values, dtype=np.float64)
    if name == 'linear':
        return values.copy()
    if name == 'log':
        with np.errstate(divide='ignore', invalid='ignore'):
            result = np.log10(values)
        result[~np.isfinite(result)] = np.nan
        return result
    import flowutils
    t, w, m, a = parameters
    flat = np.ascontiguousarray(values.reshape(-1))
    finite = np.isfinite(flat)
    result = np.full(flat.shape, np.nan)
    if finite.any():
        result[finite] = flowutils.transforms.logicle(flat[finite], None, t=t, m=m, w=w, a=a)
    return result.reshape(values.shape)


def inverse(values, scale):
    """Scale coordinates back to original units."""
    name, parameters = parse(scale)
    values = np.asarray(values, dtype=np.float64)
    if name == 'linear':
        return values.copy()
    if name == 'log':
        with np.errstate(over='ignore', invalid='ignore'):
            result = np.power(10.0, values)
        result[~np.isfinite(result)] = np.nan
        return result
    import flowutils
    t, w, m, a = parameters
    flat = np.ascontiguousarray(values.reshape(-1))
    finite = np.isfinite(flat)
    result = np.full(flat.shape, np.nan)
    if finite.any():
        result[finite] = flowutils.transforms.logicle_inverse(flat[finite], None, t=t, m=m, w=w, a=a)
    return result.reshape(values.shape)


def convert(values, original, target):
    """Coordinates in one scale expressed in another; impossible values are NaN."""
    if original == target:
        return np.asarray(values, dtype=np.float64).copy()
    return forward(inverse(values, original), target)


def describe(scale):
    name, parameters = parse(scale)
    if name == 'logicle':
        t, w, m, a = parameters
        return f'logicle (T={_number(t)}, W={_number(w)}, M={_number(m)}, A={_number(a)})'
    return 'log10' if name == 'log' else 'linear'


def transform_id(scale):
    """Identifier of the matching transformation inside a FlowKit strategy."""
    name, parameters = parse(scale)
    if name == 'linear':
        return None
    if name == 'log':
        return 'log10'
    return 'logicle_' + '_'.join(_number(v).replace('.', 'p').replace('-', 'n').replace('+', '') for v in parameters)


def logicle_ticks(scale, low, high):
    """Decade ticks for a logicle axis: ``[(position, label), ...]`` major, minor."""
    name, _ = parse(scale)
    if name != 'logicle':
        raise ValueError('Ticks are defined for logicle scales')
    lowest, highest = inverse(np.array([low, high]), scale)
    major, minor = [], []
    top = max(abs(lowest), abs(highest), 10.0)
    decades = range(1, int(math.ceil(math.log10(top))) + 1)
    superscript = str.maketrans('0123456789-', '⁰¹²³⁴⁵⁶⁷⁸⁹⁻')
    candidates = [(0.0, '0')]
    for exponent in decades:
        value = 10.0 ** exponent
        text = '10' + str(exponent).translate(superscript)
        candidates += [(value, text), (-value, '-' + text)]
        minor += [sign * step * value / 10 for sign in (1, -1) for step in range(2, 10)]
    positions = forward(np.array([c[0] for c in candidates]), scale)
    kept = []
    for position, (value, text) in sorted(zip(positions, candidates)):
        if low <= position <= high:
            # Labels inside the compressed linear region would overprint each other.
            if kept and position - kept[-1][0] < (high - low) * 0.06:
                if value == 0:
                    kept[-1] = (float(position), text)
                continue
            kept.append((float(position), text))
    major = kept
    minor_positions = forward(np.array(minor), scale) if minor else np.array([])
    minor = [(float(p), '') for p in minor_positions if low <= p <= high]
    return major, minor
