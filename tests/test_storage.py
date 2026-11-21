import csv
import json
from pathlib import Path
import pytest
from flowhero.model import Polygon, Project
from flowhero.storage import bind_source, digest, export_csv, load_project, relink, save_project


def test_roundtrip_relocation_and_identity(tmp_path):
    original = tmp_path / 'file.fcs'
    original.write_bytes(b'original data')
    source = bind_source(str(original))
    gates = [Polygon('pop', 'Display name', None, ('x', 'y'), ((0, 0), (3, 0), (3, 3))),
             Polygon('child', 'Display name', 'pop', ('y', 'z'), ((0, 0), (2, 0), (2, 2)))]
    project = Project([source], gates, 'fl', revision=4)
    path = tmp_path / 'project.json'
    save_project(project, path)
    assert load_project(path) == project
    moved = tmp_path / 'moved.fcs'
    moved.write_bytes(original.read_bytes())
    original.unlink()
    replacement = relink(source, str(moved))
    assert replacement.id == source.id and replacement.sha256 == source.sha256
    moved.write_bytes(b'new data')
    with pytest.raises(ValueError, match='changed'):
        relink(source, str(moved))


@pytest.mark.parametrize('mutation', [
    lambda d: d.update(version=99),
    lambda d: d['project'].update(domain='unknown-compensation'),
    lambda d: d['project'].update(revision=True),
    lambda d: d['project'].update(sources=[{'id': 'a', 'path': 'x', 'sha256': 'bad'}]),
    lambda d: d['project'].update(gates=[{'id': 'a', 'name': 'bad', 'parent_id': None,
                                       'channels': ['x', 'y'], 'vertices': [[0, 0], [1, 1], [2, 2]]}]),
    lambda d: d['project'].update(channel=[]),
    lambda d: d['project'].update(statistic='unknown'),
    lambda d: d['project'].update(statistic=None),
])
def test_malformed_projects_fail_closed(tmp_path, mutation):
    path = tmp_path / 'bad.json'
    save_project(Project(), path)
    data = json.loads(path.read_text())
    mutation(data)
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_project(path)


def test_failed_atomic_save_preserves_existing_file(tmp_path, monkeypatch):
    import flowhero.storage as storage
    path = tmp_path / 'project.json'
    save_project(Project(channel='before'), path)
    before = digest(path)
    def fail(*args):
        raise OSError('disk unavailable')
    monkeypatch.setattr(storage.os, 'replace', fail)
    with pytest.raises(OSError):
        save_project(Project(channel='after'), path)
    assert digest(path) == before
    assert not list(tmp_path.glob('*.tmp'))


def test_csv_preserves_quotes_paths_and_empty_statistics(tmp_path):
    path = tmp_path / 'out.csv'
    row = dict(sample_id='id', sample='comma, "sample"', source_path='/local/a b.fcs', source_sha256='a'*64,
               gate_id='g', gate='U937', channel='YL1-A', event_count=0, statistic='median',
               value=None, status='empty', domain='flowkit-preprocessed-uncompensated')
    export_csv([row], path)
    with path.open(newline='') as f:
        result = list(csv.DictReader(f))
    assert result[0]['sample'] == row['sample']
    assert result[0]['value'] == '' and result[0]['status'] == 'empty'


def test_statistic_roundtrip_and_version_one_migration(tmp_path):
    path = tmp_path / 'project.json'
    project = Project(channel='FL1-A', statistic='geometric_mean')
    save_project(project, path)
    assert load_project(path) == project
    document = json.loads(path.read_text())
    document['version'] = 1
    del document['project']['statistic']
    for name in ['viability', 'overrides', 'histogram', 'plots', 'analysis', 'gate_imports', 'annotations', 'compensation']:
        del document['project'][name]
    path.write_text(json.dumps(document))
    assert load_project(path).statistic == 'median'
    save_project(load_project(path), path)
    upgraded = json.loads(path.read_text())
    assert upgraded['version'] == 10
    assert upgraded['project']['statistic'] == 'median'
