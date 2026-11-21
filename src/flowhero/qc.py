"""Descriptive helpers computed from every event: singlet band and time course.

Both are aids for a person who then decides. The singlet gate is an ordinary
polygon that can be edited or deleted; the time course flags bins but never
removes events. Neither has been validated as an automatic quality filter.
"""
import numpy as np

MAD_TO_SD = 1.4826
SINGLET_WIDTH = 3.0
TIME_BINS = 100
RATE_LIMIT = 4.0


def singlet_polygon(area, height, width=SINGLET_WIDTH):
    """Band around the median height/area ratio, as polygon vertices (linear axes).

    Events whose ratio lies within ``width`` robust standard deviations
    (1.4826 x median absolute deviation) of the median ratio are kept; the band
    spans the 0.1st to 99.9th percentile of area, widened by 5 % of that span.
    """
    area, height = np.asarray(area, dtype=np.float64), np.asarray(height, dtype=np.float64)
    usable = np.isfinite(area) & np.isfinite(height) & (area > 0) & (height > 0)
    area, height = area[usable], height[usable]
    if len(area) < 100:
        raise ValueError('A singlet gate needs at least 100 events with positive area and height')
    ratio = height / area
    centre = float(np.median(ratio))
    spread = MAD_TO_SD * float(np.median(np.abs(ratio - centre)))
    spread = max(spread, 0.005 * centre)
    low, high = max(centre - width * spread, centre * 0.05), centre + width * spread
    a_low, a_high = (float(v) for v in np.percentile(area, [0.1, 99.9]))
    margin = 0.05 * (a_high - a_low)
    a_low, a_high = max(a_low - margin, a_low * 0.5), a_high + margin
    if not a_high > a_low:
        raise ValueError('The area values of this population do not span a range')
    vertices = ((a_low, a_low * low), (a_high, a_high * low), (a_high, a_high * high), (a_low, a_low * high))
    inside = (ratio >= low) & (ratio <= high) & (area >= a_low) & (area <= a_high)
    return vertices, {'method': 'median height/area ratio +/- 3 robust SD', 'events': int(len(area)),
                      'median_ratio': centre, 'robust_sd': spread, 'ratio_low': low, 'ratio_high': high,
                      'fraction_inside': float(inside.mean())}


def time_course(time, signal=None, bins=TIME_BINS):
    """Events per time bin and the median of one channel per bin.

    A bin is flagged when its event count differs from the median count by more
    than ``RATE_LIMIT`` robust standard deviations, where the robust SD is the
    larger of 1.4826 x MAD and the Poisson expectation sqrt(median).
    """
    time = np.asarray(time, dtype=np.float64)
    keep = np.isfinite(time)
    if signal is not None:
        signal = np.asarray(signal, dtype=np.float64)[keep]
    time = time[keep]
    if len(time) < bins or not time.max() > time.min():
        raise ValueError('Too few events or no spread in the time channel for a time course')
    edges = np.linspace(time.min(), time.max(), bins + 1)
    index = np.minimum(np.searchsorted(edges, time, side='right') - 1, bins - 1)
    counts = np.bincount(index, minlength=bins)
    typical = float(np.median(counts))
    spread = max(MAD_TO_SD * float(np.median(np.abs(counts - typical))), np.sqrt(max(typical, 1.0)))
    flagged = np.abs(counts - typical) > RATE_LIMIT * spread
    medians = None
    if signal is not None:
        medians = [float(np.median(values[np.isfinite(values)])) if np.isfinite(values).any() else None
                   for values in (signal[index == i] for i in range(bins))]
    return {'edges': edges.tolist(), 'counts': counts.tolist(), 'flagged': flagged.tolist(),
            'median_count': typical, 'robust_sd': spread, 'limit': RATE_LIMIT,
            'events': int(len(time)), 'flagged_events': int(counts[flagged].sum()),
            'signal_medians': medians}
