"""Result tables, raw plot data and provenance, independent of the open plots.

Everything here recomputes from verified sources and full populations. The
window calls it in a worker; ``flowhero export-statistics`` calls it directly.
"""
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
import csv
import json
import math
import re

import numpy as np

from . import __version__
from .cache import AnalysisCache
from .model import RangeGate, ordered_gates
from .storage import atomic_write

BASIC_STATISTICS = ('median', 'arithmetic_mean', 'geometric_mean')
SPREAD_STATISTICS = ('sd', 'cv', 'robust_cv')
STATISTIC_DEFINITIONS = {
    'median': 'Median of the untransformed values.',
    'arithmetic_mean': 'Arithmetic mean of the untransformed values.',
    'geometric_mean': 'exp(mean(ln x)); undefined (status nonpositive) if any value is <= 0.',
    'sd': 'Sample standard deviation (n - 1 denominator); needs at least two events.',
    'cv': '100 x sample standard deviation / arithmetic mean; undefined if the mean is 0.',
    'robust_cv': '100 x 0.5 x (P84.13 - P15.87) / median; undefined if the median is 0.',
    'percentile': 'pN is the N-th percentile by linear interpolation between order statistics (NumPy default).',
}
# A one-sided threshold is stored as an ordinary range gate. Its outer bound is
# this constant, far beyond any value a cytometer file can hold (32-bit ADC
# counts are < 4.3e9); it is written into every export that uses such a gate.
OPEN_BOUND = 1e12
PERCENTILE = re.compile(r'p(\d{1,2}(?:\.\d{1,2})?)$')


def valid_statistic(name):
    return name in BASIC_STATISTICS or name in SPREAD_STATISTICS or bool(PERCENTILE.match(str(name)))


def extended_statistic(values, name):
    """Return ``(value, status)`` with the same status vocabulary as the app.

    Empty populations and populations holding nonfinite values never yield a
    number. A statistic that is mathematically undefined reports why.
    """
    if not valid_statistic(name):
        raise ValueError(f'Unknown statistic: {name}')
    values = np.asarray(values, dtype=np.float64)
    if not len(values):
        return None, 'empty'
    if not np.isfinite(values).all():
        return None, 'nonfinite'
    with np.errstate(over='ignore', invalid='ignore', divide='ignore'):
        if name == 'median':
            result = np.median(values)
        elif name == 'arithmetic_mean':
            result = np.mean(values)
        elif name == 'geometric_mean':
            if np.any(values <= 0):
                return None, 'nonpositive'
            result = np.exp(np.mean(np.log(values)))
        elif name in ('sd', 'cv'):
            if len(values) < 2:
                return None, 'insufficient'
            result = np.std(values, ddof=1)
            if name == 'cv':
                mean = np.mean(values)
                if mean == 0:
                    return None, 'undefined'
                result = 100 * result / mean
        elif name == 'robust_cv':
            median = np.median(values)
            if median == 0:
                return None, 'undefined'
            low, high = np.percentile(values, [15.87, 84.13])
            result = 100 * .5 * (high - low) / median
        else:
            result = np.percentile(values, float(PERCENTILE.match(name).group(1)))
    result = float(result)
    return (result, 'ok') if math.isfinite(result) else (None, 'nonfinite')


def population_paths(gates):
    by_id = {g.id: g for g in gates}
    paths = {}
    used = set()
    for gate in ordered_gates(gates):
        path = (paths[gate.parent_id] + '/' if gate.parent_id else '') + gate.name
        candidate, number = path, 2
        while candidate in used:
            # Display names may repeat; exported columns must stay distinct.
            candidate, number = f'{path} [{number}]', number + 1
        used.add(candidate)
        paths[gate.id] = candidate
    return paths, by_id


def acquired_volume_ul(sample):
    """FCS ``$VOL`` is the acquired volume in nanolitres; absent on most instruments."""
    try:
        volume = float(sample.get_metadata().get('vol'))
    except (TypeError, ValueError):
        return None
    return volume / 1000 if math.isfinite(volume) and volume > 0 else None


def sample_statistics(project, source, channels, statistics, ancestor_id=None, cache=None,
                      checkpoint=lambda: None):
    """Long-format rows for one sample: every population x channel x statistic."""
    from .analysis import analysis_state
    from .review import population_status
    gates = project.gates_for(source.id)
    sample, events, labels, gates, memberships, _, _, _ = analysis_state(source, gates, cache, checkpoint,
                                                                         project.compensation)
    missing = [c for c in channels if c not in labels]
    if missing:
        raise ValueError('Channel(s) not in this sample: ' + ', '.join(missing))
    paths, by_id = population_paths(gates)
    if ancestor_id is not None and ancestor_id not in by_id:
        raise ValueError('The chosen ancestor population no longer exists')
    volume = acquired_volume_ul(sample)
    overridden = {g.id for g in project.overrides.get(source.id, [])}
    annotations = project.annotations.get(source.id, {})
    total = len(events)
    populations = [(None, 'All events', 'All events', None, np.ones(total, dtype=bool))] + [
        (g.id, g.name, paths[g.id], g.parent_id, memberships[g.id]) for g in gates]
    rows = []
    for gate_id, name, path, parent_id, mask in populations:
        checkpoint()
        count = int(mask.sum())
        parent_count = int(memberships[parent_id].sum()) if parent_id else total
        base = {'sample_id': source.id, 'sample': Path(source.path).name,
                **{'annotation:' + key: value for key, value in annotations.items()},
                'source_path': source.path, 'source_sha256': source.sha256,
                'gate_id': gate_id or 'root', 'gate': name, 'population_path': path,
                'parent_id': parent_id or 'root', 'event_count': count,
                'percent_parent': 100 * count / parent_count if parent_count else None,
                'percent_total': 100 * count / total if total else None,
                'gate_scope': 'sample' if gate_id in overridden else 'shared',
                'analysis_status': population_status(project, source.id, gate_id),
                'domain': project.domain}
        if ancestor_id is not None:
            lineage, cursor = set(), gate_id
            while cursor is not None:
                lineage.add(cursor)
                cursor = by_id[cursor].parent_id
            ancestor_count = int(memberships[ancestor_id].sum())
            base['percent_of_ancestor'] = (100 * count / ancestor_count
                                           if ancestor_id in lineage and ancestor_count else None)
            base['ancestor'] = paths[ancestor_id]
        if volume is not None:
            base['events_per_ul'] = count / volume
            base['acquired_volume_ul'] = volume
        if not channels:
            rows.append({**base, 'channel': '', 'statistic': '', 'value': None, 'status': ''})
        for channel in channels:
            values = events[mask, labels.index(channel)].astype(np.float64)
            for statistic in statistics:
                value, status = extended_statistic(values, statistic)
                rows.append({**base, 'channel': channel, 'statistic': statistic,
                             'value': value, 'status': status})
    return rows


def statistics_rows(project, channels, statistics, ancestor_id=None, cache=None, checkpoint=lambda: None):
    """Rows for every sample; a failed sample is reported, never written as zeros."""
    project.validate()
    for name in statistics:
        if not valid_statistic(name):
            raise ValueError(f'Unknown statistic: {name}')
    cache = cache if cache is not None else AnalysisCache()
    rows, failures = [], []
    for source in project.sources:
        try:
            rows.extend(sample_statistics(project, source, channels, statistics, ancestor_id, cache, checkpoint))
        except (ValueError, OSError, KeyError) as exc:
            failures.append({'sample_id': source.id, 'sample': Path(source.path).name, 'reason': str(exc)})
    return rows, failures


LONG_FIELDS = ['sample', 'population_path', 'gate', 'channel', 'statistic', 'value', 'status',
               'event_count', 'percent_parent', 'percent_total', 'percent_of_ancestor', 'ancestor',
               'events_per_ul', 'acquired_volume_ul', 'gate_scope', 'analysis_status', 'sample_id',
               'gate_id', 'parent_id', 'source_path', 'source_sha256', 'domain']
COUNT_COLUMNS = [('event_count', 'events'), ('percent_parent', '% of parent'), ('percent_total', '% of all events'),
                 ('percent_of_ancestor', '% of ancestor'), ('events_per_ul', 'events/µL')]


def annotation_fields(rows):
    return list(dict.fromkeys(key for row in rows for key in row if key.startswith('annotation:')))


def long_table(rows):
    notes = annotation_fields(rows)
    present = {key for row in rows for key in row}
    fields = ['sample'] + notes + [f for f in LONG_FIELDS[1:] if f in present]
    header = {field: field.removeprefix('annotation:') for field in fields}
    return fields, header, rows


def wide_table(rows):
    """One row per sample; one column per population and measure (for Prism/Excel)."""
    notes = annotation_fields(rows)
    present = {key for row in rows for key in row}
    samples, columns = {}, {}
    for row in rows:
        record = samples.setdefault(row['sample_id'], {'sample': row['sample'],
                                    **{key: row.get(key, '') for key in notes}})
        for key, label in COUNT_COLUMNS:
            if key in present:
                column = f"{row['population_path']} | {label}"
                columns[column] = None
                record[column] = row.get(key)
        if row['statistic']:
            column = f"{row['population_path']} | {row['channel']} {row['statistic']}"
            columns[column] = None
            record[column] = row['value']
    fields = ['sample'] + notes + list(columns)
    header = {field: field.removeprefix('annotation:') for field in fields}
    return fields, header, list(samples.values())


def write_csv(path, fields, header, rows):
    def write(stream):
        writer = csv.writer(stream)
        writer.writerow([header[field] for field in fields])
        for row in rows:
            writer.writerow(['' if row.get(field) is None else row.get(field) for field in fields])
    atomic_write(path, write)


def package_version(name):
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return 'unknown'


def gate_definition(gate, path):
    record = {'id': gate.id, 'population_path': path, 'type': type(gate).__name__,
              'channels': list(gate.channels), 'scales': list(gate.scales),
              'coordinates': 'log10(value) on log axes, logicle(value) on logicle axes, value on linear axes'}
    if hasattr(gate, 'operands'):
        record.update(operation=gate.operation, operands=list(gate.operands))
        record.pop('coordinates')
    elif hasattr(gate, 'center'):
        record.update(center=list(gate.center), half_axes=[list(a) for a in gate.axes])
    elif hasattr(gate, 'vertices'):
        record['vertices'] = [list(v) for v in gate.vertices]
    else:
        record['minimum'] = list(gate.minimum) if isinstance(gate.minimum, tuple) else gate.minimum
        record['maximum'] = list(gate.maximum) if isinstance(gate.maximum, tuple) else gate.maximum
        from . import scales as scale_rules
        if isinstance(gate, RangeGate) and math.isclose(gate.maximum, float(scale_rules.forward([OPEN_BOUND], gate.scales[0])[0])):
            record['note'] = f'One-sided threshold; the bound {OPEN_BOUND:g} stands for "open".'
    return record


def domain_meaning(project):
    if project.compensation is None:
        return ('Acquisition-preprocessed, uncompensated, untransformed values as read by FlowKit. '
                'No compensation and no display transform enter any statistic.')
    if project.compensation['source'] == 'file':
        how = 'the spillover matrix stored in each FCS file ($SPILLOVER, $SPILL or $COMP)'
    else:
        how = 'one spillover matrix applied to every sample (listed in this record)'
    return ('Acquisition-preprocessed values as read by FlowKit, compensated with ' + how +
            ' (events × inverse spillover, FlowKit/FlowUtils). Gates and statistics use the compensated, '
            'otherwise untransformed values; no display transform enters any statistic.')


def provenance(project, *, project_path=None, channels=(), statistics=(), failures=()):
    """Everything needed to state how exported numbers were produced."""
    paths, _ = population_paths(project.default_gates())
    samples = []
    for source in project.sources:
        own = {g.id for g in project.overrides.get(source.id, [])}
        samples.append({'id': source.id, 'file': Path(source.path).name, 'path': source.path,
                        'sha256': source.sha256, 'annotations': project.annotations.get(source.id, {}),
                        'sample_specific_gates': [gate_definition(g, paths[g.id])
                                                  for g in project.gates_for(source.id) if g.id in own]})
    return {
        'format': 'flowhero-provenance', 'version': 1,
        'created_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'software': {'FlowHero': __version__, 'FlowKit': package_version('flowkit'),
                     'NumPy': package_version('numpy'), 'Python': '.'.join(map(str, __import__('sys').version_info[:3]))},
        'project': {'path': str(project_path) if project_path else None, 'revision': project.revision},
        'data_domain': {'id': project.domain, 'meaning': domain_meaning(project),
                        'compensation': project.compensation},
        'gating': {'engine': 'FlowKit GatingStrategy; hierarchy applied as parent AND child membership',
                   'validity': 'Events with nonfinite gate coordinates are excluded; log-scale gate dimensions require positive values.',
                   'shared_gates': [gate_definition(g, paths[g.id]) for g in ordered_gates(project.default_gates())]},
        'statistics': {'channels': list(channels), 'requested': list(statistics),
                       'definitions': {name: text for name, text in STATISTIC_DEFINITIONS.items()
                                       if name in statistics or (name == 'percentile' and any(PERCENTILE.match(s) for s in statistics))},
                       'percentages': 'percent_parent = events / parent events; percent_total = events / all events in the file.',
                       'events_per_ul': 'events / ($VOL / 1000), only for files that record $VOL (nL).'},
        'samples': samples, 'failures': list(failures),
        'gate_review': None if project.analysis is None else {
            'method': project.analysis['method'],
            'note': 'Gate positions for some samples were proposed automatically; see analysis_status per row.'},
    }


def write_provenance(path, record):
    atomic_write(path, lambda stream: json.dump(record, stream, indent=2, allow_nan=False))


def methods_report(record):
    """Markdown supplement: software, files, gates and statistic definitions."""
    software = record['software']
    lines = ['# Flow cytometry analysis record', '',
             f"Generated {record['created_utc']} by FlowHero {software['FlowHero']}.", '',
             '## Draft methods text', '',
             (f"Flow cytometry data were analysed with FlowHero {software['FlowHero']} "
              f"(FlowKit {software['FlowKit']}, NumPy {software['NumPy']}, Python {software['Python']}). "
              f"Gates were applied hierarchically to {'compensated' if record['data_domain'].get('compensation') else 'uncompensated'}, untransformed acquisition values; "
              'statistics were calculated on all events of each gated population. '
              + ('Statistics reported: ' + ', '.join(record['statistics']['requested']) + '. '
                 if record['statistics']['requested'] else '')
              + 'Gate coordinates, file checksums and statistic definitions are given below.'), '',
             '_Edit this paragraph to describe staining, acquisition and any compensation done on the instrument._', '',
             '## Software', '', '| Component | Version |', '|---|---|']
    lines += [f'| {name} | {version} |' for name, version in software.items()]
    lines += ['', '## Data domain', '', record['data_domain']['meaning'], '', '## Samples', '']
    notes = list(dict.fromkeys(key for s in record['samples'] for key in s['annotations']))
    lines += ['| File | ' + ''.join(f'{n} | ' for n in notes) + 'SHA-256 | Own gate positions |',
              '|---|' + '---|' * (len(notes) + 2)]
    for sample in record['samples']:
        lines.append(f"| {sample['file']} | " + ''.join(f"{sample['annotations'].get(n, '')} | " for n in notes)
                     + f"`{sample['sha256']}` | {len(sample['sample_specific_gates']) or 'none'} |")
    lines += ['', '## Gates (shared strategy)', '', record['gating']['engine'] + '. ' + record['gating']['validity'], '']
    def describe(gate):
        text = f"- **{gate['population_path']}** — {gate['type']} on " + ' × '.join(
            f'{c} ({s})' for c, s in zip(gate['channels'], gate['scales']))
        if 'operands' in gate:
            names = {g['id']: g['population_path'].rsplit('/', 1)[-1] for g in record['gating']['shared_gates']}
            joiner = {'and': ' AND ', 'or': ' OR '}.get(gate['operation'], '')
            inner = joiner.join(names.get(o, o) for o in gate['operands'])
            return f"- **{gate['population_path']}** — combined gate: " + ('NOT ' + inner if gate['operation'] == 'not' else inner)
        if 'center' in gate:
            text += ('; centre (' + ', '.join(f'{v:.6g}' for v in gate['center']) + '), half-axes '
                     + ' and '.join('(' + ', '.join(f'{v:.6g}' for v in a) + ')' for a in gate['half_axes']))
        elif 'vertices' in gate:
            text += '; vertices ' + ', '.join('(' + ', '.join(f'{v:.6g}' for v in p) + ')' for p in gate['vertices'])
        else:
            text += f"; minimum {gate['minimum']}, maximum {gate['maximum']}"
        return text + ('. ' + gate['note'] if 'note' in gate else '')
    lines += [describe(g) for g in record['gating']['shared_gates']] or ['No gates.']
    lines += ['', 'Coordinates are log10(value) on log axes, logicle(value; T, W, M, A) on logicle axes '
              '(Moore and Parks 2012, as implemented in FlowUtils) and the value itself on linear axes.']
    compensation = record['data_domain'].get('compensation')
    if compensation and compensation['source'] == 'matrix':
        names = compensation['channels']
        lines += ['', '## Spillover matrix', '', 'Rows: fluorochrome; columns: detector.', '',
                  '| | ' + ' | '.join(names) + ' |', '|---|' + '---|' * len(names)]
        lines += [f'| {name} | ' + ' | '.join(f'{v:.6g}' for v in row) + ' |'
                  for name, row in zip(names, compensation['values'])]
    custom = [(s['file'], g) for s in record['samples'] for g in s['sample_specific_gates']]
    if custom:
        lines += ['', '### Sample-specific gate positions', ''] + [f'- {name}: ' + describe(g)[2:] for name, g in custom]
    lines += ['', '## Statistic definitions', '']
    lines += [f'- **{name}**: {text}' for name, text in record['statistics']['definitions'].items()]
    lines += [f"- **Percentages**: {record['statistics']['percentages']}",
              f"- **Events per µL**: {record['statistics']['events_per_ul']}"]
    if record['gate_review']:
        lines += ['', '## Automatically proposed gate positions', '', record['gate_review']['note']]
    if record['failures']:
        lines += ['', '## Samples without results', ''] + [f"- {f['sample']}: {f['reason']}" for f in record['failures']]
    return '\n'.join(lines) + '\n'


def population_events(project, source, population_id, channels, cache=None):
    """Untransformed values of every event in a population, for external plotting."""
    from .analysis import analysis_state
    _, events, labels, _, memberships, _, _, _ = analysis_state(source, project.gates_for(source.id), cache,
                                                                compensation=project.compensation)
    missing = [c for c in channels if c not in labels]
    if missing:
        raise ValueError('Channel(s) not in this sample: ' + ', '.join(missing))
    mask = memberships[population_id] if population_id else np.ones(len(events), dtype=bool)
    return events[mask][:, [labels.index(c) for c in channels]]


def write_events(path, project, sources, population_id, channels, cache=None):
    """One row per event: sample, then one column per channel (raw values)."""
    cache = cache if cache is not None else AnalysisCache()
    blocks = [(source, population_events(project, source, population_id, channels, cache)) for source in sources]
    def write(stream):
        writer = csv.writer(stream)
        writer.writerow(['sample'] + list(channels))
        for source, values in blocks:
            name = Path(source.path).name
            writer.writerows([name, *(repr(float(v)) for v in row)] for row in values)
    atomic_write(path, write)
    return sum(len(values) for _, values in blocks)


def control_threshold(project, source, population_id, channel, percentile, scale='linear', cache=None):
    """Percentile of a control sample's population, in gate coordinates.

    On a log axis only positive values can be gated, so the percentile is taken
    over the positive values and returned as log10.
    """
    if not 0 < percentile < 100:
        raise ValueError('Choose a percentile between 0 and 100')
    values = population_events(project, source, population_id, [channel], cache)[:, 0].astype(np.float64)
    values = values[np.isfinite(values)]
    if scale == 'log':
        values = values[values > 0]
    if scale != 'linear':
        from . import scales as scale_rules
        values = scale_rules.forward(values, scale)
    if len(values) < 2:
        raise ValueError('The control population has too few usable events for a threshold')
    return float(np.percentile(values, percentile)), int(len(values))


def fcs_information(source, cache=None):
    """Keywords, channel table and embedded spillover text of one verified file."""
    from .analysis import analysis_state
    sample = analysis_state(source, [], cache)[0]
    keywords = {str(k): str(v) for k, v in sample.get_metadata().items()}
    channels = []
    for index, (name, marker) in enumerate(zip(sample.pnn_labels, sample.pns_labels), start=1):
        channels.append({'index': index, 'name': name, 'marker': marker,
                         'voltage': keywords.get(f'p{index}v', ''), 'gain': keywords.get(f'p{index}g', ''),
                         'range': keywords.get(f'p{index}r', ''), 'amplification': keywords.get(f'p{index}e', '')})
    from .analysis import file_spillover
    spill = file_spillover(sample)
    matrix = None if spill is None else {'channels': spill['channels'],
                                         'values': [[format(v, '.6g') for v in row] for row in spill['values']]}
    return {'file': Path(source.path).name, 'path': source.path, 'sha256': source.sha256,
            'event_count': int(sample.event_count), 'keywords': keywords, 'channels': channels,
            'spillover': matrix}


def read_spillover_csv(path):
    """Spillover table: header row of channel names, then one row per channel.

    Rows may start with the channel name. Row = fluorochrome, column = detector,
    as in the FCS ``$SPILLOVER`` keyword.
    """
    with open(path, newline='', encoding='utf-8-sig') as stream:
        text = stream.read()
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=',;\t')
    except csv.Error:
        dialect = csv.excel
    table = [[cell.strip() for cell in row] for row in csv.reader(text.splitlines(), dialect)
             if any(cell.strip() for cell in row)]
    if len(table) < 3:
        raise ValueError('A spillover table needs a header row and at least two channel rows')
    labelled = len(table[1]) == len(table[0]) and not _is_number(table[1][0])
    names = table[0][1:] if labelled else table[0]
    body = [row[1:] if labelled else row for row in table[1:]]
    if labelled and [row[0] for row in table[1:]] != names:
        raise ValueError('Row names must repeat the column names in the same order')
    try:
        values = [[float(cell) for cell in row] for row in body]
    except ValueError:
        raise ValueError('The spillover table contains a cell that is not a number') from None
    from .model import validate_compensation
    compensation = {'source': 'matrix', 'channels': names, 'values': values}
    validate_compensation(compensation)
    return compensation


def _is_number(text):
    try:
        float(text)
        return True
    except ValueError:
        return False


def write_spillover_csv(path, compensation):
    def write(stream):
        writer = csv.writer(stream)
        writer.writerow([''] + list(compensation['channels']))
        for name, row in zip(compensation['channels'], compensation['values']):
            writer.writerow([name] + [repr(float(v)) for v in row])
    atomic_write(path, write)


def read_annotations(path, sources):
    """Match a CSV's first column to sample file names; other columns become annotations."""
    with open(path, newline='', encoding='utf-8-sig') as stream:
        sample_text = stream.read()
    try:
        dialect = csv.Sniffer().sniff(sample_text[:4096], delimiters=',;\t')
    except csv.Error:
        dialect = csv.excel
    table = [row for row in csv.reader(sample_text.splitlines(), dialect) if any(cell.strip() for cell in row)]
    if len(table) < 2 or len(table[0]) < 2:
        raise ValueError('The annotation file needs a header row, a sample column and at least one more column')
    names = [name.strip() for name in table[0][1:]]
    if not all(names) or len(set(names)) != len(names):
        raise ValueError('Annotation columns need distinct, nonempty names')
    by_name = {}
    for source in sources:
        for key in (Path(source.path).name, Path(source.path).stem):
            by_name.setdefault(key.lower(), []).append(source)
    annotations, unmatched, seen = {}, [], set()
    for row in table[1:]:
        key = row[0].strip()
        matches = {s.id: s for s in by_name.get(key.lower(), [])}
        if len(matches) != 1:
            unmatched.append(key)
            continue
        source = next(iter(matches.values()))
        if source.id in seen:
            raise ValueError(f'Sample listed twice in the annotation file: {key}')
        seen.add(source.id)
        cells = [cell.strip() for cell in row[1:]] + [''] * len(names)
        annotations[source.id] = dict(zip(names, cells))
    if not annotations:
        raise ValueError('No row matched a sample file name in this project')
    return annotations, unmatched
