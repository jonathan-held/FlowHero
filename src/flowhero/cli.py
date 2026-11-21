"""Freeze support runs before either headless commands or the Qt workbench."""
from multiprocessing import freeze_support
import json
import sys


def main():
    freeze_support()
    if len(sys.argv)>1 and sys.argv[1] in ('--smoke-test','--agent-smoke-test'):
        if len(sys.argv)!=3:
            print(json.dumps({'outcome':'error','error':'--smoke-test requires one new output directory'}))
            raise SystemExit(2)
        if sys.argv[1]=='--agent-smoke-test':
            from .agent_smoke import run
        else:
            from .sheet_smoke import run
        run(sys.argv[2])
    elif len(sys.argv)>1:
        from .commands import run
        raise SystemExit(run(sys.argv[1:]))
    else:
        from .workbench import main as launch
        launch()
