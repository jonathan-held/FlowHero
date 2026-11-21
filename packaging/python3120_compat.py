"""Build-only workaround for CPython 3.12.0 code.replace corruption.

Upstream PyInstaller #7992 / CPython #111866: changing code filenames in 3.12.0
can corrupt inlined comprehensions (observed Mac SciPy 'del obj' failure).
Keep original code objects/paths on that interpreter only. No scientific code
or installed package is patched; newer interpreters use normal anonymization.
"""
import sys


def apply(version=None):
    if tuple(version or sys.version_info[:3]) != (3, 12, 0):
        return False
    from PyInstaller.building import utils
    from PyInstaller.archive import writers

    def preserve(code_object, filename):
        return code_object

    utils.replace_filename_in_code_object = preserve
    writers.replace_filename_in_code_object = preserve
    print('CPython 3.12.0 compatibility: preserving code objects/filenames; build paths remain in bytecode.')
    return True
