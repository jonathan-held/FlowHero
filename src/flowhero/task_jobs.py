"""Exports and file look-ups that need event data run outside the Qt process."""


def task_worker(queue, project, tasks, include_plot):
    task = tasks[0]
    revision = project.revision
    try:
        from . import export
        kind = task['kind']
        if kind == 'statistics':
            rows, failures = export.statistics_rows(project, task['channels'], task['statistics'], task.get('ancestor_id'))
            if not rows:
                raise ValueError('No sample could be calculated: ' + '; '.join(f['reason'] for f in failures))
            table = export.wide_table(rows) if task['layout'] == 'wide' else export.long_table(rows)
            export.write_csv(task['path'], *table)
            record = export.provenance(project, project_path=task.get('project_path'), channels=task['channels'],
                                       statistics=task['statistics'], failures=failures)
            export.write_provenance(task['path'] + '.provenance.json', record)
            result = {'rows': len(table[2]), 'failures': failures, 'path': task['path']}
        elif kind == 'events':
            sources = [s for s in project.sources if s.id in task['sample_ids']]
            count = export.write_events(task['path'], project, sources, task['population_id'], task['channels'])
            result = {'rows': count, 'failures': [], 'path': task['path']}
        elif kind == 'threshold':
            source = next(s for s in project.sources if s.id == task['sample_id'])
            value, count = export.control_threshold(project, source, task['population_id'], task['channel'],
                                                    task['percentile'], task['scale'])
            result = {'value': value, 'events': count}
        elif kind == 'information':
            source = next(s for s in project.sources if s.id == task['sample_id'])
            result = export.fcs_information(source)
        elif kind == 'spillover':
            from .analysis import analysis_state, file_spillover
            source = next(s for s in project.sources if s.id == task['sample_id'])
            result = {'spillover': file_spillover(analysis_state(source, [])[0]), 'sample': source.path}
        elif kind == 'singlets':
            from .qc import singlet_polygon
            source = next(s for s in project.sources if s.id == task['sample_id'])
            values = export.population_events(project, source, task['population_id'], [task['area'], task['height']])
            vertices, diagnostics = singlet_polygon(values[:, 0], values[:, 1])
            result = {'vertices': vertices, 'diagnostics': diagnostics}
        elif kind == 'time_qc':
            from .analysis import analysis_state
            from .qc import time_course
            source = next(s for s in project.sources if s.id == task['sample_id'])
            sample, events, labels = analysis_state(source, [], compensation=project.compensation)[:3]
            if sample.time_index is None:
                raise ValueError('This file has no time channel')
            channel = task.get('channel') if task.get('channel') in labels else None
            result = time_course(events[:, sample.time_index],
                                 events[:, labels.index(channel)] if channel else None)
            result.update(sample=source.path, channel=channel, time_channel=labels[sample.time_index])
        else:
            raise ValueError('Unknown task')
        queue.put({'kind': 'task_ready', 'revision': revision, 'task': task, 'result': result})
    except Exception as exc:
        queue.put({'kind': 'error', 'revision': revision, 'message': f'{type(exc).__name__}: {exc}'})
    queue.put({'kind': 'done', 'revision': revision})
