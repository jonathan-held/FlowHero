from pathlib import Path
import runpy
import pytest


def test_python3120_build_workaround_preserves_code_objects(monkeypatch):
    pytest.importorskip('PyInstaller')
    from PyInstaller.building import utils
    from PyInstaller.archive import writers
    apply = runpy.run_path(str(Path(__file__).parents[1]/'packaging/python3120_compat.py'))['apply']
    original_utils = utils.replace_filename_in_code_object
    original_writers = writers.replace_filename_in_code_object
    monkeypatch.setattr(utils, 'replace_filename_in_code_object', original_utils)
    monkeypatch.setattr(writers, 'replace_filename_in_code_object', original_writers)
    assert not apply((3, 12, 1))
    assert utils.replace_filename_in_code_object is original_utils
    assert apply((3, 12, 0))
    code = compile('values = [1, 2]; answer = [x for x in values]', 'original.py', 'exec')
    assert utils.replace_filename_in_code_object(code, 'other.py') is code
    assert writers.replace_filename_in_code_object(code, 'other.py') is code
