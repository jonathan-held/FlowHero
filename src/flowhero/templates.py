"""Named shared strategies never transfer overrides onto unrelated samples."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from .storage import load_project, save_project


def shared_template(project):
    template = deepcopy(project)
    template.sources = []
    template.annotations = {}
    template.overrides = {}
    template.analysis = None
    template.plots = [replace(p, sample_ids=(), pinned=False) for p in template.plots]
    if template.histogram:
        template.histogram = replace(template.histogram, sample_ids=())
    template.revision = 0
    template.validate()
    return template


def save_template(project, directory, name):
    name = name.strip()
    if not name or name in ('.', '..') or any(c in name for c in '/\\'):
        raise ValueError('Use a template name without path separators')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (name + '.flowhero.json')
    if path.exists():
        raise ValueError('A template with that name exists. Choose a new name.')
    save_project(shared_template(project), path)
    return path


def apply_template(path, sources):
    template = load_project(path)
    if template.overrides:
        raise ValueError('Template has sample-specific adjustments; save a shared template first or explicitly map them in the original project.')
    template.sources = list(sources)
    template.analysis = None
    template.plots = [replace(p,sample_ids=(),pinned=False) for p in template.plots]
    if template.histogram:
        template.histogram = replace(template.histogram,sample_ids=())
    template.validate()
    return template
