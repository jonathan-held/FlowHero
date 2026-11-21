"""Generate a tiny synthetic experiment; no private dataset is distributed."""
from pathlib import Path
import argparse
from dataclasses import replace
import flowio
from flowhero.model import Histogram, Polygon, Project, RangeGate
from flowhero.storage import bind_source, save_project


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', nargs='?', default='data/demo')
    parser.add_argument('--extended', action='store_true', help='Include viability, one sample adjustment and overlays')
    args = parser.parse_args()
    root = Path(args.directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    events = [[2, 2, 2, 1], [3, 3, 3, 4], [4, 4, 4, 7], [5, 5, 5, 10000],
              [7, 7, 7, 500], [20, 2, 2, -10000]]
    sources = []
    for multiplier in [1, 2]:
        path = root / f'demo-{multiplier}.fcs'
        if path.exists():
            raise SystemExit(f'Refusing to overwrite {path}; choose a new output directory.')
        with path.open('wb') as stream:
            data = [value if column < 3 else value * multiplier for row in events for column, value in enumerate(row)]
            flowio.create_fcs(stream, data, ['FSC-A', 'SSC-A', 'SSC-H', 'FL1-A'])
        sources.append(bind_source(str(path)))
    gates = [Polygon('population', 'U937', None, ('FSC-A', 'SSC-A'), ((0,0), (10,0), (10,10), (0,10))),
             Polygon('singlets', 'Single cells', 'population', ('SSC-A', 'SSC-H'), ((0,0), (6,0), (6,6), (0,6)))]
    target = root / 'demo.flowhero.json'
    project = Project(sources, gates, 'FL1-A')
    if args.extended:
        project.viability = RangeGate('viable', 'Viable cells', 'singlets', 'FL1-A', 0, 8)
        project.set_override(sources[1].id, replace(project.viability, maximum=15))
        project.histogram = Histogram('viable', 0, 20, bins=4, sample_ids=tuple(s.id for s in sources))
    save_project(project, target)
    print(f'Open project: {target}')
    print('Expected events: U937=5, Single cells=4 in each sample.')
    print('Expected medians: demo-1 U937=7 / child=5.5; demo-2 U937=14 / child=11.')
    if args.extended:
        print('Viability: demo-1 [0,8), demo-2 sample adjustment [0,15). Both count=3, medians=4 / 8.')
        print('Compare checked samples: edges 0,5,10,15,20; bin counts demo-1=2,1,0,0 / demo-2=1,1,1,0.')


if __name__ == '__main__':
    main()
