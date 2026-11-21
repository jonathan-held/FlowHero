"""Headless commands share the same model, analysis and portable contracts."""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
from .storage import load_project


def run(argv):
    parser=argparse.ArgumentParser(prog='flowhero')
    from . import __version__
    parser.add_argument('--version',action='version',version=__version__)
    commands=parser.add_subparsers(dest='command',required=True)
    inspect=commands.add_parser('inspect');inspect.add_argument('project');inspect.add_argument('--details',action='store_true')
    analyze=commands.add_parser('analyze')
    analyze.add_argument('template');analyze.add_argument('--fcs',action='append',required=True)
    analyze.add_argument('--output',required=True);analyze.add_argument('--reference-sample')
    analyze.add_argument('--adapt-channel',action='append',default=[])
    analyze.add_argument('--bins',type=int,default=64)
    analyze.add_argument('--max-shift-fraction',type=float,default=.5)
    analyze.add_argument('--min-events',type=int,default=30)
    package=commands.add_parser('package');package.add_argument('project');package.add_argument('output')
    unpack=commands.add_parser('unpack');unpack.add_argument('archive');unpack.add_argument('output')
    gate_import=commands.add_parser('import-gates', help='Append exported JSON/Gating-ML gates to a project')
    gate_import.add_argument('project');gate_import.add_argument('gates');gate_import.add_argument('--output',required=True)
    gate_import.add_argument('--variant',help='Exported sample identity whose geometry becomes shared gates')
    gate_import.add_argument('--channel',nargs=2,action='append',default=[],metavar=('EXPORTED','ACQUISITION'))
    statistics=commands.add_parser('export-statistics', help='Recalculate every sample and write a statistics table')
    statistics.add_argument('project');statistics.add_argument('--output',required=True)
    statistics.add_argument('--layout',choices=('long','wide'),default='long')
    statistics.add_argument('--channel',action='append',default=[],help='Repeat per channel; default: statistics channel and plot channels')
    statistics.add_argument('--statistic',action='append',default=[],help='median, arithmetic_mean, geometric_mean, sd, cv, robust_cv or pN; default: the first three')
    statistics.add_argument('--ancestor',help='Gate identity for an extra percent-of-ancestor column')
    report=commands.add_parser('methods-report', help='Write the Markdown analysis record of a project')
    report.add_argument('project');report.add_argument('--output',required=True)
    args=parser.parse_args(argv)
    try:
        if args.command=='inspect':
            project=load_project(args.project)
            result={'sources':[asdict(s) for s in project.sources],
                    'gates':[{'id':g.id,'name':g.name,'parent_id':g.parent_id,'channels':g.channels,'scales':g.scales} for g in project.default_gates()],
                    'plots':[asdict(p) for p in project.plots],'analysis':project.analysis}
            from .gate_import import import_summary
            result['gate_imports']=project.gate_imports if args.details else [import_summary(record) for record in project.gate_imports]
            if project.analysis is not None and not args.details:
                from .review import review_rows
                records=review_rows(project)
                result['analysis']={'method':project.analysis['method'],'proposal_count':len(records),
                                    'pending_review':sum(r['status'] not in ('accepted','reset','removed') for _,_,r in records),
                                    'failure_count':len(project.analysis['failures']),'full_provenance':str(Path(args.project).absolute())}
        elif args.command=='import-gates':
            from .gate_import import read_gate_import, prepare_gate_import, import_summary
            from .storage import save_project
            if os.path.lexists(args.output):
                raise ValueError('Import output exists; choose a new project file')
            project=load_project(args.project)
            document=read_gate_import(args.gates)
            if args.variant not in document.variants:
                raise ValueError('Choose an exported sample identity from the gate JSON sample_overrides')
            mapping={channel:channel for gate in document.variants[args.variant] for channel in gate.channels}
            seen=set()
            for original,target in args.channel:
                if original not in mapping or original in seen:
                    raise ValueError('Channel mappings must name distinct exported channels')
                seen.add(original);mapping[original]=target
            candidate=prepare_gate_import(project,document,variant=args.variant,channel_map=mapping)
            candidate.revision=project.revision+1
            save_project(candidate,args.output)
            result={'outcome':'ok','project':str(Path(args.output).absolute()),'gate_import':import_summary(candidate.gate_imports[-1]),
                    'counts':'not_computed; open the project or analyze with matching FCS inputs'}
        elif args.command in ('export-statistics','methods-report'):
            from . import export
            from .storage import atomic_write
            if os.path.lexists(args.output):
                raise ValueError('Output exists; choose a new file')
            project=load_project(args.project)
            defaults=[c for c in dict.fromkeys([project.channel]+[c for p in project.plots for c in (p.x_channel,p.y_channel)]) if c]
            if args.command=='methods-report':
                record=export.provenance(project,project_path=Path(args.project).absolute(),channels=defaults,statistics=export.BASIC_STATISTICS)
                atomic_write(args.output,lambda stream:stream.write(export.methods_report(record)))
                result={'outcome':'ok','report':str(Path(args.output).absolute())}
            else:
                channels=args.channel or defaults
                names=args.statistic or list(export.BASIC_STATISTICS)
                rows,failures=export.statistics_rows(project,channels,names,args.ancestor)
                if not rows:
                    raise ValueError('No sample could be calculated: '+'; '.join(f['reason'] for f in failures))
                table=export.wide_table(rows) if args.layout=='wide' else export.long_table(rows)
                export.write_csv(args.output,*table)
                record=export.provenance(project,project_path=Path(args.project).absolute(),channels=channels,statistics=names,failures=failures)
                export.write_provenance(args.output+'.provenance.json',record)
                result={'outcome':'partial' if failures else 'ok','table':str(Path(args.output).absolute()),
                        'provenance':str(Path(args.output+'.provenance.json').absolute()),'rows':len(table[2]),'failures':failures}
        elif args.command=='analyze':
            from .automation import analyze
            result=analyze(args.template,args.fcs,args.output,reference_id=args.reference_sample,
                    bins=args.bins,max_shift_fraction=args.max_shift_fraction,min_events=args.min_events,
                    adapt_channels=args.adapt_channel)
        elif args.command=='package':
            from .portable import create_package
            if os.path.lexists(args.output):
                raise ValueError('Package output exists; choose a new file')
            manifest=create_package(load_project(args.project),args.output)
            result={'outcome':'ok','package':str(Path(args.output).absolute()),'manifest':manifest}
        else:
            from .portable import import_package
            project,path=import_package(args.archive,args.output)
            result={'outcome':'ok','project':str(path),'source_count':len(project.sources)}
        print(json.dumps(result,allow_nan=False))
        return 2 if result.get('outcome')=='partial' else 0
    except Exception as exc:
        print(json.dumps({'outcome':'error','error':f'{type(exc).__name__}: {exc}'}))
        return 1
