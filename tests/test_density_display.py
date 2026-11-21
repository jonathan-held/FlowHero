from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from flowhero.density import density_points
from flowhero.model import PlotSpec, Project, Source
from flowhero.storage import load_project, save_project


def density_data(points, x_edges, y_edges, limit=12_000):
    counts, _, _ = np.histogram2d(np.asarray(points)[:, 0], np.asarray(points)[:, 1],
                                 bins=(x_edges, y_edges))
    return counts, density_points(points, x_edges, y_edges, counts, limit)


def test_sparse_bins_retained_deterministically_and_full_density_never_reestimated():
    # A single rare event in the middle is missed by a simple even event sample.
    points = np.array([[.25, .25]] * 100 + [[1.25, .25]] + [[.25, .25]] * 100)
    before = points.copy()
    counts, shown = density_data(points, [0, 1, 2], [0, 1, 2], limit=4)
    original = counts.copy()
    assert shown == density_points(points, [0, 1, 2], [0, 1, 2], counts, 4)
    assert shown['rendered_count'] == 4 and shown['eligible_count'] == 201
    assert shown['occupied_bins'] == shown['represented_bins'] == 2
    assert 1.25 in shown['x']
    assert sorted(shown['density']) == [1, 200, 200, 200]
    assert shown['sampling'] == 'sparse-bin-representatives-then-even'
    assert shown['density_estimator'] == 'full-population histogram2d'
    assert np.array_equal(points, before) and np.array_equal(counts, original)


def test_sparse_bin_priority_when_capacity_cannot_cover_every_bin():
    points = [[.25, .25]] * 50 + [[1.25, .25]] * 2 + [[2.25, .25]]
    _, shown = density_data(points, [0, 1, 2, 3], [0, 1], limit=2)
    assert shown['x'] == [1.25, 2.25]
    assert shown['density'] == [2, 1]
    assert (shown['represented_bins'], shown['occupied_bins']) == (2, 3)


def test_transformed_coordinates_edges_exclusions_and_no_membership_mutation():
    # Inputs are already in their declared view domain (including log10 axes).
    points = np.array([[-2, -1], [-1, 0], [0, 1], [1, 2], [10, 10], [np.nan, 0]])
    x_edges, y_edges = [-2, -1, 0, 1], [-1, 0, 1, 2]
    counts, shown = density_data(points, x_edges, y_edges)
    assert shown['x'] == [-2, -1, 0, 1]
    assert shown['y'] == [-1, 0, 1, 2]
    assert shown['density'] == [1, 1, 2, 2]
    assert shown['sampling'] == 'all'
    assert shown['eligible_count'] == shown['rendered_count'] == 4
    assert counts.sum() == 4
    empty = density_points([], x_edges, y_edges, np.zeros((3, 3)))
    assert empty['x'] == empty['y'] == empty['density'] == []
    assert empty['rendered_count'] == empty['represented_bins'] == 0


def test_contours_persist_and_prior_v5_defaults_to_false(tmp_path):
    spec = PlotSpec('d', 'density', 'FSC-A', 'SSC-A', contours=True)
    project = Project(plots=[spec])
    path = tmp_path / 'density.flowhero.json'
    save_project(project, path)
    assert load_project(path) == project
    import json
    data = json.loads(path.read_text())
    del data['project']['plots'][0]['contours']
    path.write_text(json.dumps(data))
    assert load_project(path).plots == [replace(spec, contours=False)]
    with pytest.raises(ValueError, match='contours'):
        replace(spec, contours=1)


def test_real_density_scatter_color_scale_contours_and_context_toggle():
    from PySide6 import QtCore, QtWidgets
    import pyqtgraph as pg
    from flowhero.plot_sheet import PlotDocument
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    spec = PlotSpec('d', 'density', 'FSC-A', 'SSC-A', contours=True)
    source = Source('a', '/local/sample.fcs', '0' * 64)
    points = [[10.25, 20.5]] * 9 + [[10.25, 22.5]] + [[11.25, 22.5]] * 4
    x_edges, y_edges = [10, 11, 12], [20, 22, 24]
    counts, shown = density_data(points, x_edges, y_edges)
    raw_counts = counts.copy()
    data = {**shown, 'counts': counts.tolist(), 'x_edges': x_edges, 'y_edges': y_edges,
            'x_bounds': [10, 12], 'y_bounds': [20, 24], 'event_count': len(points),
            'nonfinite': 0, 'nonpositive': 0, 'outside': 0}
    owner = SimpleNamespace(project=Project([source], plots=[spec]), rebuilding=True,
                            closing=True, calls=[], results={'a': {'sheet_plots': {'d': data}}})
    owner.plot_spec = lambda pid: owner.project.plots[0]
    owner.selected_source = lambda: source
    owner.edit_plot = lambda pid, **changes: owner.calls.append((pid, changes))
    document = PlotDocument(owner, spec)
    document.render()
    scatter = next(item for item in document.graph.items() if isinstance(item, pg.ScatterPlotItem))
    assert len(scatter.points()) == len(points)
    assert not any(isinstance(item, pg.ImageItem) for item in document.graph.items())
    assert document.density_scale.getAxis('left').labelText == 'log1p(events/bin)'
    assert document.density_scale.values == (0, np.log1p(9))
    assert len(document.density_contours) == 3
    for contour in document.density_contours:
        assert np.array_equal(contour.data, raw_counts)
        assert contour.axisOrder == 'col-major'
        assert contour.mapToParent(QtCore.QPointF(0, 0)) == QtCore.QPointF(10, 20)
        assert contour.mapToParent(QtCore.QPointF(2, 2)) == QtCore.QPointF(12, 24)
    assert len(document.graph.getPlotItem().legend.items) == 1
    assert 'rendered 14/14' in document.summary.text()
    assert data['counts'] == raw_counts.tolist()
    failures = []
    def toggle():
        menu = document._context_menu
        try:
            action = next(action for action in menu.actions() if action.text() == 'Density contours')
            assert action.isChecked()
            action.trigger()
            assert not any('Compare checked' in action.text() for action in menu.actions())
        except Exception as exc:
            failures.append(exc)
        finally:
            menu.close()
    QtCore.QTimer.singleShot(0, toggle)
    document.context_menu(QtCore.QPoint(0, 0))
    assert not failures
    assert owner.calls == [('d', {'contours': False})]
    # A single layout-owned scale survives rerenders and hides while pending.
    old_scale = document.density_scale
    document.render()
    assert document.density_scale is old_scale
    document.clear_pending()
    assert not document.density_scale.isVisible() and document.density_contours == []
    document.close()
    app.processEvents()
