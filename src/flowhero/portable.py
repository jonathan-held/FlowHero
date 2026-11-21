"""Inspectable ZIP projects with streamed, verified FCS payloads.

Packages carry integrity/provenance, not a signature or an authenticity claim.
Only a completely validated new local directory is published during import.
"""
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import zipfile

from . import __version__
from . import storage

FORMAT = 'flowpilot-portable'
VERSION = 1
CHUNK_SIZE = 1024 * 1024


def size_estimate(project):
    """Input bytes before compression; stat only, without reading FCS events."""
    project.validate()
    return {'file_count': len(project.sources),
            'total_input_bytes': sum(Path(source.path).stat().st_size for source in project.sources)}


def _copy(stream, target, *, expected_size=None, expected_sha256=None):
    digest = hashlib.sha256()
    count = 0
    while True:
        chunk = stream.read(CHUNK_SIZE)
        if not chunk:
            break
        count += len(chunk)
        if expected_size is not None and count > expected_size:
            raise ValueError('Payload exceeds its declared uncompressed size')
        digest.update(chunk)
        target.write(chunk)
    if expected_size is not None and count != expected_size:
        raise ValueError('Payload is shorter than its declared uncompressed size')
    hexdigest = digest.hexdigest()
    if expected_sha256 is not None and hexdigest != expected_sha256:
        raise ValueError('Payload SHA-256 mismatch')
    return {'sha256': hexdigest, 'bytes': count}


def create_package(project, destination):
    """Atomically write a ZIP; failures preserve any existing destination."""
    project.validate()
    destination = Path(destination).expanduser()
    resolved = destination.resolve()
    if any(resolved == Path(source.path).resolve() for source in project.sources):
        raise ValueError('A portable package cannot overwrite an original FCS source')
    for source in project.sources:
        storage.verify_source(source)
    bindings = [replace(source, path=f'data/{i:06d}.fcs')
                for i, source in enumerate(project.sources, 1)]
    manifest = {
        'format': FORMAT, 'version': VERSION, 'app_version': __version__,
        'project_schema': storage.VERSION, 'domain': project.domain,
        'provenance': {'created_utc': datetime.now(timezone.utc).isoformat(),
                       'original_sources': [asdict(source) for source in project.sources]},
        'sources': [asdict(source) for source in bindings], 'payloads': [],
    }
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.' + destination.name + '.',
                                         suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
        with tempfile.TemporaryDirectory(dir=destination.parent, prefix='.flowhero-package-') as scratch:
            project_file = Path(scratch) / 'project.json'
            storage.save_project(replace(project, sources=bindings), project_file)
            with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED,
                                 allowZip64=True) as archive:
                with project_file.open('rb') as stream, archive.open('project.json', 'w', force_zip64=True) as target:
                    manifest['payloads'].append({'path': 'project.json', **_copy(stream, target)})
                for original, binding in zip(project.sources, bindings):
                    with open(original.path, 'rb') as stream, archive.open(binding.path, 'w', force_zip64=True) as target:
                        record = _copy(stream, target, expected_sha256=original.sha256)
                    manifest['payloads'].append({'path': binding.path, **record})
                archive.writestr('manifest.json', json.dumps(manifest, indent=2, allow_nan=False))
        # Windows only flushes handles opened for writing.
        with temporary.open('r+b') as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        temporary = None
        return manifest
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _safe_path(name):
    if not isinstance(name, str) or not name or '\\' in name or ':' in name or '\x00' in name:
        raise ValueError('Unsafe package path')
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or path.as_posix() != name or name.endswith('/'):
        raise ValueError('Unsafe package path: ' + name)
    return name


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON field: ' + key)
        result[key] = value
    return result


def _read_json(path):
    def reject_constant(value):
        raise ValueError('Invalid JSON number: ' + value)
    with path.open(encoding='utf-8') as stream:
        return json.load(stream, object_pairs_hook=_unique_object, parse_constant=reject_constant)


def _sha(value):
    return (isinstance(value, str) and len(value) == 64
            and all(character in '0123456789abcdef' for character in value))


def _inventory(archive):
    inventory = {}
    for info in archive.infolist():
        name = _safe_path(info.orig_filename)
        if name != info.filename:
            raise ValueError('ZIP entry name changed during decoding')
        mode = stat.S_IFMT(info.external_attr >> 16)
        if name in inventory:
            raise ValueError('Duplicate ZIP entry: ' + name)
        if info.is_dir() or mode not in (0, stat.S_IFREG) or info.flag_bits & 1:
            raise ValueError('Package entries must be ordinary unencrypted files: ' + name)
        inventory[name] = info
    if 'manifest.json' not in inventory or 'project.json' not in inventory:
        raise ValueError('Package is missing its manifest or project')
    return inventory


def _manifest(path, inventory):
    manifest = _read_json(path)
    expected = {'format', 'version', 'app_version', 'project_schema', 'domain',
                'provenance', 'sources', 'payloads'}
    if not isinstance(manifest, dict) or set(manifest) != expected:
        raise ValueError('Invalid portable package manifest')
    if (manifest['format'] != FORMAT or type(manifest['version']) is not int
            or manifest['version'] != VERSION):
        raise ValueError('Unknown portable package format/version')
    if (not isinstance(manifest['app_version'], str) or not manifest['app_version']
            or type(manifest['project_schema']) is not int
            or manifest['project_schema'] < 1 or manifest['project_schema'] > storage.VERSION):
        raise ValueError('Unsupported portable project schema or app metadata')
    if not isinstance(manifest['sources'], list) or not isinstance(manifest['payloads'], list):
        raise ValueError('Invalid portable payload inventory')
    payloads = {}
    for payload in manifest['payloads']:
        if not isinstance(payload, dict) or set(payload) != {'path', 'sha256', 'bytes'}:
            raise ValueError('Invalid payload record')
        name = _safe_path(payload['path'])
        if name in payloads or not _sha(payload['sha256']) or type(payload['bytes']) is not int or payload['bytes'] < 0:
            raise ValueError('Duplicate payload or invalid payload hash/size')
        if name not in inventory or inventory[name].file_size != payload['bytes']:
            raise ValueError('Missing payload or manifest size mismatch: ' + name)
        payloads[name] = payload
    ids, source_paths = set(), set()
    for source in manifest['sources']:
        if (not isinstance(source, dict) or set(source) != {'id', 'path', 'sha256'}
                or not isinstance(source['id'], str) or not source['id'] or source['id'] in ids
                or not _sha(source['sha256'])):
            raise ValueError('Invalid or duplicate package source identity')
        name = _safe_path(source['path'])
        if not name.startswith('data/') or name in source_paths or name not in payloads:
            raise ValueError('Invalid or missing package source payload')
        if source['sha256'] != payloads[name]['sha256']:
            raise ValueError('Source and payload digest mismatch')
        ids.add(source['id']); source_paths.add(name)
    if set(payloads) != {'project.json'} | source_paths or set(inventory) != {'manifest.json'} | set(payloads):
        raise ValueError('Package inventory is incomplete or contains unexpected entries')
    provenance = manifest['provenance']
    if (not isinstance(provenance, dict) or set(provenance) != {'created_utc', 'original_sources'}
            or not isinstance(provenance['created_utc'], str) or not isinstance(provenance['original_sources'], list)):
        raise ValueError('Invalid package provenance')
    originals = provenance['original_sources']
    if len(originals) != len(ids):
        raise ValueError('Package provenance source inventory mismatch')
    by_id = {source['id']: source for source in manifest['sources']}
    seen = set()
    for original in originals:
        if (not isinstance(original, dict) or set(original) != {'id', 'path', 'sha256'}
                or not all(isinstance(value, str) for value in original.values())
                or original['id'] in seen or original['id'] not in by_id
                or original['sha256'] != by_id[original['id']]['sha256']):
            raise ValueError('Package provenance source identity mismatch')
        seen.add(original['id'])
    return manifest, payloads


def import_package(archive, destination):
    """Validate and publish a package in a NEW directory; return Project, Path.

    Uncompressed sizes are preflighted against available destination space and
    enforced while streaming. Data sizes are not restricted by a small file cap.
    Original relative project bindings and manifest provenance remain inspectable.
    """
    destination = Path(destination).expanduser()
    if os.path.lexists(destination):
        raise ValueError('Package destination already exists; choose a new local directory')
    destination = destination.resolve()
    try:
        with zipfile.ZipFile(archive) as package:
            inventory = _inventory(package)
            # Extra project bytes allow the local absolute-path document alongside
            # its archived relative-path provenance. FCS bytes are counted once.
            required = sum(info.file_size for info in inventory.values()) + inventory['project.json'].file_size
            if required > shutil.disk_usage(destination.parent).free:
                raise ValueError('Insufficient free space for declared package expansion')
            with tempfile.TemporaryDirectory(dir=destination.parent,
                                              prefix='.' + destination.name + '.partial-') as scratch:
                stage = Path(scratch)
                with package.open(inventory['manifest.json']) as stream, (stage / 'manifest.json').open('wb') as target:
                    _copy(stream, target, expected_size=inventory['manifest.json'].file_size)
                manifest, payloads = _manifest(stage / 'manifest.json', inventory)
                for name, record in payloads.items():
                    path = stage.joinpath(*PurePosixPath(name).parts)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with package.open(inventory[name]) as stream, path.open('xb') as target:
                        _copy(stream, target, expected_size=record['bytes'], expected_sha256=record['sha256'])
                archived_path = stage / 'project.json'
                project = storage.load_project(archived_path)
                if (_read_json(archived_path)['version'] != manifest['project_schema']
                        or project.domain != manifest['domain']
                        or [asdict(source) for source in project.sources] != manifest['sources']):
                    raise ValueError('Archived project and manifest source/schema/domain mismatch')
                project = replace(project, sources=[replace(source, path=str(destination / source.path))
                                                     for source in project.sources])
                (stage / 'provenance').mkdir()
                archived_path.rename(stage / 'provenance' / 'project.json')
                storage.save_project(project, stage / 'project.json')
                if os.path.lexists(destination):
                    raise ValueError('Package destination already exists; choose a new local directory')
                stage.rename(destination)
                return project, destination / 'project.json'
    except (zipfile.BadZipFile, json.JSONDecodeError, UnicodeError, RuntimeError) as exc:
        raise ValueError(f'Invalid portable package: {exc}') from exc
