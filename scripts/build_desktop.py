"""Build on the target OS. On macOS this creates a local arm64 .app for testing."""
import argparse
from datetime import datetime, timezone
from importlib import metadata
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def notices():
    output = ROOT / 'build' / 'third-party-notices'
    output.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT/'packaging/THIRD_PARTY_NOTICES.txt', output/'README.txt')
    shutil.copytree(ROOT/'packaging/licenses', output/'licenses', dirs_exist_ok=True)
    inventory = []
    for dist in sorted(metadata.distributions(), key=lambda d: d.metadata.get('Name', '').lower()):
        name = dist.metadata.get('Name', 'unknown')
        folder = output / name.replace('/', '_')
        folder.mkdir(exist_ok=True)
        (folder/'METADATA.txt').write_text(dist.read_text('METADATA') or dist.read_text('PKG-INFO') or name)
        copied = []
        for file in dist.files or []:
            # Preserve wheel-supplied notices, including numpy/scipy vendored libs.
            if any(part in file.name.lower() for part in ('license', 'copying', 'copyright', 'notice')):
                source = Path(dist.locate_file(file))
                if source.is_file():
                    target = folder/str(file).replace('..', '_').lstrip('/')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
                    copied.append(str(file))
        inventory.append({'name': name, 'version': dist.version, 'copied_notice_files': copied})
    (output/'inventory.json').write_text(json.dumps(inventory, indent=2)+'\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Inspect prerequisites without building')
    parser.add_argument('--output', type=Path, help='New output directory; must not already exist')
    args = parser.parse_args()
    if sys.platform not in ('linux', 'darwin'):
        parser.error('This recipe supports Linux smoke builds and native macOS arm64 builds')
    if sys.platform == 'darwin' and platform.machine() != 'arm64':
        parser.error('Use native arm64 Python on the target Apple Silicon Mac')
    if not (3, 10) <= sys.version_info[:2] <= (3, 12):
        parser.error('Use the tested Python 3.10–3.12 runtime pins')
    for package, version in [('pyinstaller', '6.22.3'), ('pyinstaller-hooks-contrib', '2026.10')]:
        try:
            actual = metadata.version(package)
        except metadata.PackageNotFoundError:
            parser.error('Install packaging/requirements.lock in the project environment first')
        if actual != version:
            parser.error(f'{package}: expected {version}, found {actual}')
    output = (args.output or ROOT/'dist'/('desktop-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))).resolve()
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
               '--distpath', str(output), '--workpath', str(ROOT/'build/frozen'),
               str(ROOT/'packaging/FlowHero.spec')]
    print(json.dumps({'platform': sys.platform, 'architecture': platform.machine(),
                      'python': sys.version, 'output': str(output), 'command': command}, indent=2), flush=True)
    if args.check:
        return
    if output.exists():
        parser.error('Refusing to replace an existing output directory')
    notices()
    output.mkdir(parents=True)
    subprocess.run(command, cwd=ROOT, check=True)
    binary = output/'FlowHero.app/Contents/MacOS/FlowHero' if sys.platform == 'darwin' else output/'FlowHero/FlowHero'
    if not binary.exists():
        raise RuntimeError('Expected packaged executable was not produced')
    print('Built:', binary)
    print('Run its --smoke-test with a NEW local output directory, then test native dialogs and Finder launch.')


if __name__ == '__main__':
    main()
