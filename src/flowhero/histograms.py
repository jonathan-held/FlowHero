"""Histogram presentation derived from retained full-population analysis facts."""
import numpy as np


NORMALIZATION_LABELS = {
    'count': 'Events / bin',
    'event_fraction': 'Population fraction / bin',
    'peak': 'Fraction of peak / bin',
}


def histogram_values(data, mode):
    """Return display bin values and status without changing retained raw counts.

    Event fractions use the complete gated population, including nonfinite,
    log-excluded and outside-axis events. Peak normalization uses the largest
    retained bin. An empty population or no displayed events returns zero bins
    with ``empty`` status; it never invents a denominator.
    """
    if mode not in NORMALIZATION_LABELS:
        raise ValueError('Unknown histogram normalization: ' + str(mode))
    counts = np.asarray(data['counts'], dtype=float)
    population = data['event_count']
    if (counts.ndim != 1 or not np.isfinite(counts).all() or np.any(counts < 0)
            or not isinstance(population, (int, np.integer)) or isinstance(population, bool)
            or population < 0):
        raise ValueError('Histogram requires nonnegative finite bins and a population count')
    peak = float(counts.max()) if counts.size else 0.0
    if population == 0 or peak == 0:
        return np.zeros_like(counts), 'empty'
    denominator = population if mode == 'event_fraction' else peak if mode == 'peak' else 1
    return counts / denominator, 'ok'


def histogram_statistics_rows(spec, results, selected_source_id):
    """Select full-event statistics for exactly the histogram's displayed scope.

    These are existing analysis rows, never statistics estimated from bins.
    Missing or failed plot results are excluded just as they are from rendering.
    """
    if spec.kind != 'histogram':
        raise ValueError('Histogram statistics require a histogram plot')
    sample_ids = spec.sample_ids or ((selected_source_id,) if selected_source_id else ())
    population_id = spec.population_id or 'root'
    rows = []
    for sample_id in sample_ids:
        result = results.get(sample_id, {})
        data = result.get('sheet_plots', {}).get(spec.id)
        if not data or 'error' in data:
            continue
        rows.extend(dict(row) for row in result.get('rows', [])
                    if row['sample_id'] == sample_id and row['gate_id'] == population_id
                    and row['channel'] == spec.x_channel)
    return rows


STATISTIC_COLUMNS = (('median', 'Median'), ('arithmetic_mean', 'Arithmetic mean'), ('geometric_mean', 'Geometric mean'))
TABLE_FIELDS = ['sample_id', 'sample', 'source_path', 'source_sha256', 'gate_id', 'gate', 'channel', 'event_count',
                'median', 'arithmetic_mean', 'geometric_mean', 'status', 'domain', 'gate_scope', 'parent_id',
                'percent_parent', 'percent_total', 'analysis_status']


def histogram_statistics_table(rows):
    """One row per sample, population and channel; the three statistics become columns.

    ``rows`` are the long rows of ``histogram_statistics_rows``; values are
    copied, never recalculated. ``status`` is ``ok`` when all three numbers
    exist, the common reason when none does (``empty``, ``nonfinite``), and
    otherwise names each missing statistic with its reason, for example
    ``geometric_mean: nonpositive``. A missing number stays an empty cell.
    """
    names = [name for name, _ in STATISTIC_COLUMNS]
    table = {}
    for row in rows:
        if row['statistic'] not in names:
            raise ValueError('Unknown histogram statistic: ' + str(row['statistic']))
        key = (row['sample_id'], row['gate_id'], row['channel'])
        common = {k: v for k, v in row.items() if k not in ('statistic', 'value', 'status')}
        entry = table.setdefault(key, {**common, **{name: None for name in names}, '_status': {}})
        if row['statistic'] in entry['_status'] or {k: entry.get(k) for k in common} != common:
            raise ValueError('Inconsistent histogram statistics for one sample')
        entry[row['statistic']] = row['value']
        entry['_status'][row['statistic']] = row['status']
    result = []
    for entry in table.values():
        status = entry.pop('_status')
        problems = {name: status.get(name, 'not calculated') for name in names if status.get(name) != 'ok'}
        if not problems:
            entry['status'] = 'ok'
        elif len(problems) == len(names) and len(set(problems.values())) == 1:
            entry['status'] = next(iter(problems.values()))
        else:
            entry['status'] = '; '.join(f'{name}: {reason}' for name, reason in problems.items())
        result.append(entry)
    return result
