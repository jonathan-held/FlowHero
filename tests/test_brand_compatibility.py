"""Legacy documents retain their scientific schema across the FlowHero rename."""
import json
import os
from pathlib import Path
import subprocess
import sys
from flowhero.model import Project, RangeGate
from flowhero.storage import save_project,load_project,export_gates
from flowhero.gate_import import read_gate_import,prepare_gate_import
from flowhero.portable import create_package,import_package


def test_legacy_project_suffix_and_schema_seven_are_readable(tmp_path):
    p=Project(gates=[RangeGate('r','R',None,'FL1-A',0,10)])
    old=tmp_path/'old.flowpilot.json';save_project(p,old)
    doc=json.loads(old.read_text());doc['version']=7
    for field in ('annotations','compensation'):doc['project'].pop(field,None)
    old.write_text(json.dumps(doc))
    reopened=load_project(old)
    assert reopened.gates==p.gates and reopened.compensation is None
    new=tmp_path/'new.flowhero.json';save_project(reopened,new)
    assert load_project(new)==reopened
    proc=subprocess.run([sys.executable,'-m','flowhero','inspect',str(old)],capture_output=True,text=True)
    assert proc.returncode==0,proc.stderr+proc.stdout


def test_legacy_gate_json_and_portable_package_are_readable(tmp_path):
    p=Project(gates=[RangeGate('r','R',None,'FL1-A',0,10)])
    gate=tmp_path/'gates.json';export_gates(p,gate)
    assert json.loads(gate.read_text())['format']=='flowpilot-gates'
    assert prepare_gate_import(Project(),read_gate_import(gate)).gates[0].name=='R'
    package=tmp_path/'legacy.flowpilot.zip';manifest=create_package(p,package)
    assert manifest['format']=='flowpilot-portable'
    back,path=import_package(package,tmp_path/'reopened')
    assert back.gates==p.gates and load_project(path)==back


def test_public_module_and_windows_use_flowhero_name(app,bench):
    import flowhero
    assert flowhero.__version__=='0.10.2'
    assert bench.windowTitle().startswith('FlowHero 0.10.2')
    result=subprocess.run([sys.executable,'-m','flowhero','--version'],text=True,capture_output=True)
    assert result.returncode==0 and result.stdout.strip()=='0.10.2'


from test_sheet_gui import app,bench
