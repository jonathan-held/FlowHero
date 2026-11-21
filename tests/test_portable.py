from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import stat
import zipfile

import pytest

from flowhero.analysis import evaluate
from flowhero.model import PlotSpec, Project
from flowhero.portable import create_package, import_package, size_estimate
from flowhero.storage import load_project
from test_analysis import source_file, polygons


def project_input(tmp_path):
    original = tmp_path / 'originals'
    original.mkdir()
    source = source_file(original, [[2, 2, 2, 1], [3, 3, 3, 4], [7, 7, 7, 20], [20, 2, 2, 500]])
    return Project([source], polygons(), 'FL1-A', plots=[PlotSpec('hist', 'histogram', 'FL1-A')])


def read_members(path):
    with zipfile.ZipFile(path) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}


def rewrite(path, members):
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)


def assert_cleanup(tmp_path, destination):
    assert not destination.exists()
    assert not list(tmp_path.glob('.' + destination.name + '.partial-*'))


def test_portable_roundtrip_after_originals_moved_reproduces_analysis(tmp_path):
    project = project_input(tmp_path)
    project.analysis = {
        'format': 'flowpilot-adaptive-analysis', 'version': 1,
        'created_utc': '2026-11-12T00:00:00Z', 'method': {'name': 'retained provenance'},
        'template': {'path': '/original/template.json', 'sha256': '1' * 64},
        'reference': {'path': '/original/reference.fcs', 'sha256': '2' * 64},
        'inputs': [asdict(source) for source in project.sources],
        'failures': [], 'proposals': {},
    }
    before = evaluate(project.sources[0], project.gates, project.channel, plot_specs=project.plots)
    estimate = size_estimate(project)
    assert estimate == {'file_count': 1, 'total_input_bytes': Path(project.sources[0].path).stat().st_size}
    package = tmp_path / 'experiment.flowhero.zip'
    manifest = create_package(project, package)
    assert manifest['provenance']['original_sources'] == [asdict(project.sources[0])]
    members = read_members(package)
    archived = json.loads(members['project.json'])
    assert archived['project']['sources'][0]['path'] == 'data/000001.fcs'
    assert manifest['sources'][0]['path'] == 'data/000001.fcs'
    for record in manifest['payloads']:
        assert record['bytes'] == len(members[record['path']])
        assert record['sha256'] == hashlib.sha256(members[record['path']]).hexdigest()
    Path(project.sources[0].path).rename(tmp_path / 'moved-original.fcs')
    destination = tmp_path / 'imported'
    imported, local_project = import_package(package, destination)
    assert local_project == destination / 'project.json'
    assert load_project(local_project) == imported
    assert imported.sources[0].id == project.sources[0].id
    assert imported.sources[0].sha256 == project.sources[0].sha256
    assert Path(imported.sources[0].path) == destination / 'data/000001.fcs'
    assert (destination / 'manifest.json').read_bytes() == members['manifest.json']
    assert (destination / 'provenance/project.json').read_bytes() == members['project.json']
    after = evaluate(imported.sources[0], imported.gates, imported.channel, plot_specs=imported.plots)
    assert after['event_count'] == before['event_count']
    keys = ('gate_id', 'channel', 'statistic', 'event_count', 'value', 'status')
    assert [[row[key] for key in keys] for row in after['rows']] == [[row[key] for key in keys] for row in before['rows']]
    assert after['sheet_plots'] == before['sheet_plots']
    # All semantic fields survive, including newer analysis provenance fields.
    assert replace(imported, sources=project.sources) == project
    assert imported.analysis == project.analysis


def test_duplicate_basenames_and_untrusted_ids_do_not_become_paths(tmp_path):
    project = project_input(tmp_path)
    other = tmp_path / 'other'
    other.mkdir()
    second = source_file(other, [[2, 2, 2, 100]])
    first = replace(project.sources[0], id='../outside')
    second = replace(second, id='/absolute/second')
    assert Path(first.path).name == Path(second.path).name
    project.sources = [first, second]
    archive = tmp_path / 'duplicates.flowhero.zip'
    manifest = create_package(project, archive)
    assert [source['path'] for source in manifest['sources']] == ['data/000001.fcs', 'data/000002.fcs']
    imported, _ = import_package(archive, tmp_path / 'imported')
    assert [source.id for source in imported.sources] == ['../outside', '/absolute/second']
    assert Path(imported.sources[0].path).read_bytes() != Path(imported.sources[1].path).read_bytes()
    assert not (tmp_path / 'outside').exists()


def test_changed_source_failure_preserves_old_output_and_cleans_staging(tmp_path):
    project = project_input(tmp_path)
    archive = tmp_path / 'existing.flowhero.zip'
    archive.write_bytes(b'old output')
    Path(project.sources[0].path).write_bytes(b'changed source')
    with pytest.raises(ValueError, match='Source content changed'):
        create_package(project, archive)
    assert archive.read_bytes() == b'old output'
    assert not list(tmp_path.glob('.existing.flowhero.zip.*'))
    assert not list(tmp_path.glob('.flowhero-package-*'))


def test_mid_copy_digest_failure_is_atomic(tmp_path, monkeypatch):
    from flowhero import portable
    project = project_input(tmp_path)
    archive = tmp_path / 'existing.flowhero.zip'
    archive.write_bytes(b'old output')
    # Simulate changed bytes between initial verification and the streamed copy.
    monkeypatch.setattr(portable.storage, 'verify_source', lambda source: None)
    Path(project.sources[0].path).write_bytes(b'changed source')
    with pytest.raises(ValueError, match='SHA-256 mismatch'):
        create_package(project, archive)
    assert archive.read_bytes() == b'old output'
    assert not list(tmp_path.glob('.existing.flowhero.zip.*'))
    assert not list(tmp_path.glob('.flowhero-package-*'))


@pytest.mark.parametrize('corruption', ['data', 'missing', 'size', 'format', 'schema', 'domain', 'duplicate-id', 'extra'])
def test_bad_packages_never_publish_and_cleanup(tmp_path, corruption):
    project = project_input(tmp_path)
    archive = tmp_path / 'bad.flowhero.zip'
    create_package(project, archive)
    members = read_members(archive)
    manifest = json.loads(members['manifest.json'])
    if corruption == 'data':
        members['data/000001.fcs'] = b'x' * len(members['data/000001.fcs'])
    elif corruption == 'missing':
        del members['data/000001.fcs']
    elif corruption == 'size':
        manifest['payloads'][-1]['bytes'] += 1
    elif corruption == 'format':
        manifest['format'] = 'unknown'
    elif corruption == 'schema':
        manifest['project_schema'] = 999
    elif corruption == 'domain':
        manifest['domain'] = 'unknown'
    elif corruption == 'duplicate-id':
        manifest['sources'].append(manifest['sources'][0])
    elif corruption == 'extra':
        members['surprise.txt'] = b'not declared'
    members['manifest.json'] = json.dumps(manifest).encode()
    rewrite(archive, members)
    destination = tmp_path / 'imported'
    with pytest.raises(ValueError):
        import_package(archive, destination)
    assert_cleanup(tmp_path, destination)


@pytest.mark.parametrize('name', ['../escaped.fcs', '/absolute.fcs', 'data/../escaped.fcs', 'data\\escaped.fcs', 'C:/escaped.fcs'])
def test_traversal_is_rejected_before_extraction(tmp_path, name):
    project = project_input(tmp_path)
    archive = tmp_path / 'traversal.zip'
    create_package(project, archive)
    with zipfile.ZipFile(archive, 'a') as package:
        package.writestr(name, b'escape')
    destination = tmp_path / 'imported'
    with pytest.raises(ValueError, match='Unsafe package path'):
        import_package(archive, destination)
    assert_cleanup(tmp_path, destination)
    assert not (tmp_path / 'escaped.fcs').exists()


@pytest.mark.parametrize('name', ['data/a' + chr(92) + 'b.fcs', 'data/a' + chr(0) + 'b.fcs'])
def test_actual_single_backslash_and_nul_are_rejected(name):
    from flowhero.portable import _safe_path
    with pytest.raises(ValueError, match='Unsafe package path'):
        _safe_path(name)


def test_duplicate_zip_names_symlinks_and_existing_destination(tmp_path):
    project = project_input(tmp_path)
    archive = tmp_path / 'duplicate.zip'
    create_package(project, archive)
    with pytest.warns(UserWarning, match='Duplicate name'):
        with zipfile.ZipFile(archive, 'a') as package:
            package.writestr('project.json', b'duplicate')
    with pytest.raises(ValueError, match='Duplicate ZIP entry'):
        import_package(archive, tmp_path / 'duplicate-output')
    create_package(project, archive)
    with zipfile.ZipFile(archive, 'a') as package:
        info = zipfile.ZipInfo('data/link.fcs')
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        package.writestr(info, '../../originals/vectors.fcs')
    with pytest.raises(ValueError, match='ordinary unencrypted files'):
        import_package(archive, tmp_path / 'symlink-output')
    create_package(project, archive)
    existing = tmp_path / 'exists'
    existing.mkdir()
    marker = existing / 'untouched'
    marker.write_bytes(b'preserved')
    with pytest.raises(ValueError, match='already exists'):
        import_package(archive, existing)
    assert marker.read_bytes() == b'preserved'
    link = tmp_path / 'existing-link'
    link.symlink_to(tmp_path / 'not-created', target_is_directory=True)
    with pytest.raises(ValueError, match='already exists'):
        import_package(archive, link)
    assert not (tmp_path / 'not-created').exists()


def test_declared_expansion_preflight_and_original_source_output_protection(tmp_path, monkeypatch):
    from flowhero import portable
    project = project_input(tmp_path)
    source_path = Path(project.sources[0].path)
    before = source_path.read_bytes()
    with pytest.raises(ValueError, match='original FCS source'):
        create_package(project, source_path)
    assert source_path.read_bytes() == before
    archive = tmp_path / 'package.zip'
    create_package(project, archive)
    class Space:
        free = 1
    monkeypatch.setattr(portable.shutil, 'disk_usage', lambda path: Space())
    with pytest.raises(ValueError, match='Insufficient free space'):
        import_package(archive, tmp_path / 'imported')
    assert_cleanup(tmp_path, tmp_path / 'imported')
