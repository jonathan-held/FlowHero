import time
from flowhero.jobs import Jobs
from flowhero.model import Project


def slow(queue, project, sources, include_plot):
    time.sleep(30)


def finish(queue, project, sources, include_plot):
    queue.put({'kind': 'result', 'revision': project.revision, 'data': 'x' * 200_000})
    queue.put({'kind': 'error', 'revision': project.revision, 'message': 'sample failure'})
    queue.put({'kind': 'done', 'revision': project.revision})


def crash(queue, project, sources, include_plot):
    raise RuntimeError('child failed')


def collect(jobs):
    messages = []
    until = time.monotonic() + 12
    while jobs.busy and time.monotonic() < until:
        messages.extend(jobs.poll())
        time.sleep(.02)
    assert not jobs.busy
    return messages


def test_cancel_reaps_worker_and_allows_retry():
    jobs = Jobs()
    jobs.start(Project(), [], target=slow)
    process = jobs._process
    pid = process.pid
    jobs.cancel()
    assert not jobs.busy
    import multiprocessing
    assert pid not in [p.pid for p in multiprocessing.active_children()]
    jobs.start(Project(revision=2), [], target=finish)
    messages = collect(jobs)
    assert [m['kind'] for m in messages] == ['result', 'error', 'done']
    assert all(m['revision'] == 2 for m in messages)
    jobs.cancel()


def test_completion_flush_and_crash_report():
    jobs = Jobs()
    jobs.start(Project(), [], target=finish)
    messages = collect(jobs)
    assert len(messages[0]['data']) == 200_000
    assert messages[-1]['kind'] == 'done'
    jobs.start(Project(), [], target=crash)
    messages = collect(jobs)
    assert messages[-1]['kind'] == 'crash'


def test_real_batch_preserves_success_when_sibling_source_fails(tmp_path):
    from test_analysis import source_file, polygons
    from flowhero.model import Source
    source = source_file(tmp_path, [[2, 2, 2, 10], [3, 3, 3, 20]])
    missing = Source('missing', str(tmp_path / 'missing.fcs'), 'a' * 64)
    project = Project([source, missing], polygons(), 'FL1-A', revision=7)
    jobs = Jobs()
    jobs.start(project, project.sources)
    messages = collect(jobs)
    assert [m['kind'] for m in messages] == ['result', 'error', 'done']
    assert messages[0]['source_id'] == source.id
    assert messages[0]['data']['rows'][1]['value'] == 15
    assert messages[1]['source_id'] == missing.id
    assert 'Relink' in messages[1]['message']
