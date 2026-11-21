"""Portable operations run outside Qt; the window owns the staging directory."""
def portable_worker(queue, project, tasks, include_plot):
    from .portable import create_package, import_package
    task=tasks[0]
    try:
        if task['operation']=='create':
            manifest=create_package(project,task['staged'])
            queue.put({'kind':'package_ready','revision':project.revision,'manifest':manifest})
        elif task['operation']=='import':
            imported,path=import_package(task['archive'],task['staged'])
            queue.put({'kind':'import_ready','revision':project.revision,'project':imported})
        else:
            raise ValueError('Unknown portable operation')
    except Exception as exc:
        queue.put({'kind':'error','revision':project.revision,'message':f'{type(exc).__name__}: {exc}'})
    queue.put({'kind':'done','revision':project.revision})
