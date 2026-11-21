"""Real FlowIO-produced FCS files through the production FlowKit adapter."""
import hashlib
from pathlib import Path

import flowio
import numpy as np
import pytest

from flowhero.analysis import PLOT_LIMIT, evaluate
from flowhero.model import DOMAIN, Polygon, Source

CHANNELS = ["FSC-A", "SSC-A", "SSC-H", "FL1-A"]


def source_file(tmp_path, events, metadata=None):
    path = tmp_path / "vectors.fcs"
    with path.open("wb") as stream:
        flowio.create_fcs(stream, np.asarray(events).flatten().tolist(), CHANNELS,
                          metadata_dict=metadata)
    return Source("sample-vector", str(path), hashlib.sha256(path.read_bytes()).hexdigest())


def polygons():
    return [
        Polygon("population-id", "Repeated display name", None,
                ("FSC-A", "SSC-A"), ((0, 0), (10, 0), (10, 10), (0, 10))),
        Polygon("singlets-id", "Repeated display name", "population-id",
                ("SSC-A", "SSC-H"), ((0, 0), (6, 0), (6, 6), (0, 6))),
    ]


def test_hierarchy_median_and_uncompensated_domain(tmp_path):
    # Final event is inside the child polygon but outside the parent polygon.
    # Distinct stable IDs permit repeated display names.
    events = [[2, 2, 2, 1], [3, 3, 3, 4], [4, 4, 4, 7],
              [5, 5, 5, 10000], [7, 7, 7, 500], [20, 2, 2, -10000]]
    source = source_file(tmp_path, events)
    result = evaluate(source, polygons(), "FL1-A", include_plot=True)
    parent, child = result["rows"]
    assert result["channels"] == CHANNELS
    assert result["event_count"] == 6
    assert (parent["event_count"], parent["value"]) == (5, 7)
    assert (child["event_count"], child["value"]) == (4, 5.5)
    assert child["status"] == "ok"
    assert child["domain"] == DOMAIN
    assert child["source_sha256"] == source.sha256
    assert child["sample_id"] == source.id
    assert len(result["plots"]["population"]["x"]) == 6
    assert len(result["plots"]["singlets"]["x"]) == 5


def test_acquisition_gain_preprocessing_is_shared_by_gates_and_statistics(tmp_path):
    source = source_file(tmp_path, [[2, 2, 2, 10], [3, 3, 3, 30]],
                         metadata={"p4g": "2"})
    result = evaluate(source, polygons(), "FL1-A")
    assert result["rows"][0]["value"] == 10


def test_empty_and_nonfinite_are_explicit(tmp_path):
    source = source_file(tmp_path, [[2, 2, 2, float("nan")], [8, 8, 8, 10]])
    result = evaluate(source, polygons(), "FL1-A")
    assert all(row["status"] == "nonfinite" and row["value"] is None
               for row in result["rows"])
    empty = Polygon("empty", "empty", None, ("FSC-A", "SSC-A"),
                    ((30, 30), (40, 30), (40, 40), (30, 40)))
    row = evaluate(source, [empty], "FL1-A")["rows"][0]
    assert (row["event_count"], row["status"], row["value"]) == (0, "empty", None)


def test_missing_channel_and_changed_source_are_actionable(tmp_path):
    source = source_file(tmp_path, [[2, 2, 2, 10]])
    with pytest.raises(ValueError, match="missing channel.*DOES-NOT-EXIST"):
        evaluate(source, polygons(), "DOES-NOT-EXIST")
    path = Path(source.path)
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="Source content changed"):
        evaluate(source, polygons(), "FL1-A")


def test_plot_sampling_does_not_change_scientific_population(tmp_path):
    size = PLOT_LIMIT + 137
    events = np.full((size, 4), 2.0)
    events[:, 3] = np.arange(size)
    source = source_file(tmp_path, events)
    result = evaluate(source, polygons(), "FL1-A", include_plot=True)
    assert result["rows"][1]["event_count"] == size
    assert result["rows"][1]["value"] == (size - 1) / 2
    assert len(result["plots"]["population"]["x"]) == PLOT_LIMIT
    assert len(result["plots"]["singlets"]["x"]) == PLOT_LIMIT


def test_initial_load_needs_no_gate_or_statistic(tmp_path):
    source = source_file(tmp_path, [[2, 2, 2, 10]])
    result = evaluate(source, [], "", include_plot=True)
    assert result["rows"] == []
    assert result["plots"]["singlets"] == result["plots"]["population"]


@pytest.mark.parametrize('statistic, expected', [
    ('median', 2.5), ('arithmetic_mean', 4.5), ('geometric_mean', 48 ** .25),
])
def test_known_statistics_use_full_child_membership(tmp_path, statistic, expected):
    source = source_file(tmp_path, [[2, 2, 2, 1], [3, 3, 3, 1], [4, 4, 4, 4],
                                    [5, 5, 5, 12], [7, 7, 7, 500], [20, 2, 2, 9000]])
    row = evaluate(source, polygons(), 'FL1-A', statistic=statistic)['rows'][1]
    assert row['statistic'] == statistic
    assert row['event_count'] == 4
    assert row['value'] == pytest.approx(expected)
    assert row['status'] == 'ok'


@pytest.mark.parametrize('values, status', [
    ([0, 4], 'nonpositive'), ([-2, 4], 'nonpositive'),
    ([float('nan'), 4], 'nonfinite'), ([float('inf'), 0], 'nonfinite'),
])
def test_geometric_mean_invalid_values_are_not_discarded(tmp_path, values, status):
    source = source_file(tmp_path, [[2, 2, 2, values[0]], [3, 3, 3, values[1]]])
    row = evaluate(source, polygons(), 'FL1-A', statistic='geometric_mean')['rows'][1]
    assert (row['event_count'], row['status'], row['value']) == (2, status, None)


def test_means_preserve_negative_values_empty_counts_and_large_products(tmp_path):
    source = source_file(tmp_path, [[2, 2, 2, -4], [3, 3, 3, 2]])
    row = evaluate(source, polygons(), 'FL1-A', statistic='arithmetic_mean')['rows'][1]
    assert (row['value'], row['status']) == (-1, 'ok')
    empty = Polygon('empty', 'empty', None, ('FSC-A', 'SSC-A'),
                    ((30, 30), (40, 30), (40, 40), (30, 40)))
    for statistic in ['arithmetic_mean', 'geometric_mean']:
        row = evaluate(source, [empty], 'FL1-A', statistic=statistic)['rows'][0]
        assert (row['event_count'], row['status'], row['value']) == (0, 'empty', None)
    # Multiplying these 100 values overflows float64; the log-space mean is finite.
    events = np.full((100, 4), 2.0)
    events[:, 3] = 1e30
    source = source_file(tmp_path, events)
    result = evaluate(source, polygons(), 'FL1-A', statistic='geometric_mean', include_plot=True)
    assert result['rows'][1]['value'] == pytest.approx(1e30)
    assert result['rows'][1]['event_count'] == 100


def test_unknown_statistic_is_rejected_before_analysis(tmp_path):
    source = source_file(tmp_path, [[2, 2, 2, 10]])
    with pytest.raises(ValueError, match='Unsupported statistic'):
        evaluate(source, polygons(), 'FL1-A', statistic='unknown')

