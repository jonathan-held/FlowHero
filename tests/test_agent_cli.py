import json,os,subprocess,sys
from pathlib import Path
from test_adaptive import experiment
from flowhero.automation import evaluate_project,histogram_rows
from flowhero.storage import load_project,digest


def call(*args):
    return subprocess.run([sys.executable,'-m','flowhero',*map(str,args)],text=True,capture_output=True,
                           env={**os.environ,'QT_QPA_PLATFORM':'invalid-headless-check'})


def test_public_agent_and_portable_cli_are_headless_reproducible_and_non_overwriting(tmp_path):
    template,reference,new,_=experiment(tmp_path)
    output=tmp_path/'run'
    result=call('analyze',template,'--fcs',new.path,'--output',output)
    assert result.returncode==0,result.stderr+result.stdout
    outcome=json.loads(result.stdout)
    assert outcome['outcome']=='review_required' and outcome['reopen_recompute_verified']
    project=output/'project.flowhero.json'
    inspected=call('inspect',project)
    info=json.loads(inspected.stdout)
    assert info['analysis']['pending_review']==3 and 'proposals' not in info['analysis']
    package=tmp_path/'portable.flowhero.zip'
    packed=call('package',project,package)
    assert packed.returncode==0,packed.stdout+packed.stderr
    assert json.loads(packed.stdout)['outcome']=='ok'
    archive_hash=digest(package)
    duplicate=call('package',project,package)
    assert duplicate.returncode==1 and digest(package)==archive_hash
    refused=call('package',project,project)
    assert refused.returncode==1
    Path(new.path).rename(tmp_path/'moved-input.fcs')
    unpacked=call('unpack',package,tmp_path/'imported')
    assert unpacked.returncode==0,unpacked.stdout+unpacked.stderr
    imported=load_project(json.loads(unpacked.stdout)['project'])
    results,failures=evaluate_project(imported)
    assert not failures and len(histogram_rows(imported,results))==3
    # Existing run directories and invalid commands fail without starting Qt.
    duplicate=call('analyze',template,'--fcs',imported.sources[0].path,'--output',output)
    assert duplicate.returncode==1 and json.loads(duplicate.stdout)['outcome']=='error'
    unknown=call('unknown-command')
    assert unknown.returncode==2 and 'invalid choice' in unknown.stderr
    help_result=call('--help')
    assert help_result.returncode==0 and 'analyze' in help_result.stdout
