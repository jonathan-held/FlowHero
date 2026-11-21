"""One spawned worker at a time. Cancellation owns and reaps that worker."""
import multiprocessing as mp
from queue import Empty
from .model import Project


def _run(queue, project, sources, include_plot):
    # Import large scientific dependencies only in the worker.
    from .analysis import evaluate
    for source in sources:
        try:
            project.validate()
            gates = project.gates_for(source.id)
            result = evaluate(source, gates[:len(project.gates)], project.channel, include_plot,
                              statistic=project.statistic,
                              viability=gates[-1] if project.viability else None,
                              histogram=project.histogram)
            overridden = {g.id for g in project.overrides.get(source.id, [])}
            for row in result['rows']:
                row['gate_scope'] = 'sample' if row['gate_id'] in overridden else 'shared'
            queue.put({'kind': 'result', 'revision': project.revision, 'source_id': source.id,
                       'data': result})
        except Exception as exc:
            queue.put({'kind': 'error', 'revision': project.revision, 'source_id': source.id,
                       'message': f'{type(exc).__name__}: {exc}'})
    queue.put({'kind': 'done', 'revision': project.revision})


def _bind(queue, project, paths, include_plot):
    from .storage import bind_source
    from .analysis import evaluate
    for path in paths:
        try:
            source = bind_source(path)
            metadata = evaluate(source, [], '', False)
            queue.put({'kind': 'bound', 'revision': project.revision, 'source': source,
                       'channels': metadata['channels'], 'channel_labels': metadata['channel_labels']})
        except Exception as exc:
            queue.put({'kind': 'error', 'revision': project.revision, 'source_id': path,
                       'message': f'{type(exc).__name__}: {exc}'})
    queue.put({'kind': 'done', 'revision': project.revision})


class Jobs:
    def __init__(self):
        self._context = mp.get_context('spawn')
        self._process = None
        self._queue = None
        self._done = False

    @property
    def busy(self):
        return self._process is not None

    def start(self, project: Project, sources, include_plot=False, *, target=_run):
        if self.busy:
            raise RuntimeError('A job is already running')
        self._queue = self._context.Queue()
        self._done = False
        self._process = self._context.Process(target=target, args=(self._queue, project, sources, include_plot))
        try:
            self._process.start()
        except BaseException:
            self._queue.close()
            self._process = self._queue = None
            raise

    def poll(self):
        if not self.busy:
            return []
        messages = []
        while True:
            try:
                message = self._queue.get_nowait()
                messages.append(message)
                self._done |= message['kind'] == 'done'
            except Empty:
                break
        # A normal child flushes its queue before exit. Drain again next poll if
        # the child is still flushing; never join a live producer before draining.
        if not self._process.is_alive():
            self._process.join()
            while True:
                try:
                    message = self._queue.get_nowait()
                    messages.append(message)
                    self._done |= message['kind'] == 'done'
                except Empty:
                    break
            if not self._done:
                messages.append({'kind': 'crash', 'message': f'Worker exited without completion (code {self._process.exitcode})'})
            self._release()
        return messages

    def _release(self):
        self._process.close()
        self._queue.close()
        self._queue.join_thread()
        self._process = self._queue = None

    def cancel(self):
        if not self.busy:
            return
        if self._process.is_alive():
            self._process.terminate()
        self._process.join(timeout=2)
        if self._process.is_alive():
            self._process.kill()
            self._process.join(timeout=2)
        if self._process.is_alive():
            raise RuntimeError('Worker did not exit; its ownership is retained. Retry cancellation.')
        self._release()


def sheet_worker(queue, project, sources, include_plot):
    from .analysis import evaluate
    project.validate()
    for source in sources:
        try:
            result = evaluate(source, project.gates_for(source.id), project.channel,
                              statistic=project.statistic, plot_specs=project.plots,
                              compensation=project.compensation,
                              highlight_id=getattr(project, '_highlight', None))
            overridden = {g.id for g in project.overrides.get(source.id, [])}
            for row in result['rows']:
                row['gate_scope'] = 'sample' if row['gate_id'] in overridden else 'shared'
            queue.put({'kind': 'result', 'revision': project.revision, 'source_id': source.id, 'data': result})
        except Exception as exc:
            message = {'kind': 'error', 'revision': project.revision, 'source_id': source.id,
                       'message': f'{type(exc).__name__}: {exc}'}
            # A valid sample with an unavailable configured channel must still
            # expose its actual channels so the UI can recover via axis controls.
            try:
                metadata = evaluate(source, [], '', False)
                message.update(channels=metadata['channels'], channel_labels=metadata['channel_labels'])
            except Exception:
                pass
            queue.put(message)
    queue.put({'kind': 'done', 'revision': project.revision})


class Superseded(Exception):
    """A newer request owns presentation; keep valid completed cache entries."""


def session_worker(output, commands, generation):
    from .cache import AnalysisCache
    from .analysis import evaluate
    from .dependencies import relabel
    cache = AnalysisCache()
    while True:
        ticket, project, sources, binding = commands.get()
        def checkpoint():
            if generation.value != ticket:
                raise Superseded()
        class TaggedOutput:
            def put(self, message):
                checkpoint()
                output.put({**message, 'ticket': ticket})
        tagged = TaggedOutput()
        try:
            checkpoint()
            if binding:
                _bind(tagged, project, sources, False)
                continue
            project.validate()
            for source in sources:
                checkpoint()
                try:
                    result = evaluate(source, project.gates_for(source.id), project.channel,
                                      statistic=project.statistic, plot_specs=project.plots,
                                      cache=cache, checkpoint=checkpoint,
                                      compensation=project.compensation,
                                      highlight_id=getattr(project, '_highlight', None))
                    checkpoint()
                    relabel(result, project, source)
                    tagged.put({'kind': 'result', 'revision': project.revision,
                                'source_id': source.id, 'data': result})
                except Superseded:
                    raise
                except Exception as exc:
                    message = {'kind': 'error', 'revision': project.revision, 'source_id': source.id,
                               'message': f'{type(exc).__name__}: {exc}'}
                    try:
                        metadata = evaluate(source, [], '', cache=cache, checkpoint=checkpoint)
                        message.update(channels=metadata['channels'], channel_labels=metadata['channel_labels'])
                    except Superseded:
                        raise
                    except Exception:
                        pass
                    tagged.put(message)
            tagged.put({'kind': 'done', 'revision': project.revision})
        except Superseded:
            continue
        except Exception as exc:
            try:
                tagged.put({'kind': 'crash', 'message': str(exc)})
            except Superseded:
                # A newer request arrived while reporting; keep the worker alive.
                continue


class SessionJobs(Jobs):
    """One window-owned worker with a 256MiB LRU, retained until cancel/close.

    Normal completion keeps the process/cache. Superseding checks between gates,
    statistics and plots; explicit Cancel/Close still terminates and reaps it.
    """
    def __init__(self):
        super().__init__()
        self._commands = None
        self._generation = self._context.Value('q', 0)
        self._ticket = 0
        self._active = False

    @property
    def busy(self):
        return self._active

    def supersede(self):
        self._ticket += 1
        self._generation.value = self._ticket
        self._active = False

    def start(self, project, sources, include_plot=False, *, target=sheet_worker):
        self.supersede()
        if self._process is not None and not self._process.is_alive():
            self._process.join()
            self._release()
        if self._process is None:
            self._queue = self._context.Queue()
            self._commands = self._context.Queue()
            self._process = self._context.Process(target=session_worker,
                    args=(self._queue, self._commands, self._generation))
            self._process.start()
        self._active = True
        self._commands.put((self._ticket, project, sources, target == _bind))

    def poll(self):
        if self._process is None:
            return []
        messages = []
        while True:
            try:
                message = self._queue.get_nowait()
            except Empty:
                break
            if message['ticket'] != self._ticket:
                continue
            messages.append(message)
            if message['kind'] in ('done', 'crash'):
                self._active = False
        if not self._process.is_alive():
            self._process.join()
            if self._active:
                messages.append({'kind': 'crash', 'message': f'Analysis worker exited (code {self._process.exitcode})'})
            self._active = False
            self._release()
        return messages

    def _release(self):
        super()._release()
        # Pending superseded command snapshots are discarded with this session.
        self._commands.cancel_join_thread()
        self._commands.close()
        self._commands = None

    def cancel(self):
        if self._process is None:
            return
        self.supersede()
        # Base implementation uses busy to establish ownership. The session may
        # be idle but still owns a live process that must be reaped.
        self._active = True
        try:
            super().cancel()
        finally:
            self._active = self._process is not None
