"""Sample display points while retaining densities from the complete histogram."""
import numpy as np


def density_points(data_xy, x_edges, y_edges, counts, limit=12_000):
    """Return transformed scatter coordinates and their full-population bin count.

    Sampling first retains one actual event per occupied bin. If there are more
    occupied bins than display slots, sparsest bins take priority. Remaining
    slots are spread deterministically across other events in input order.
    No memberships, coordinates, histogram bins or event counts are modified.
    """
    points = np.asarray(data_xy, dtype=float)
    if not points.size:
        points = points.reshape(0, 2)
    edges = [np.asarray(x_edges, dtype=float), np.asarray(y_edges, dtype=float)]
    bins = np.asarray(counts)
    if (points.ndim != 2 or points.shape[1] != 2 or type(limit) is not int or limit < 1
            or any(e.ndim != 1 or len(e) < 2 or not np.isfinite(e).all()
                   or np.any(np.diff(e) <= 0) for e in edges)
            or bins.shape != (len(edges[0]) - 1, len(edges[1]) - 1)
            or not np.isfinite(bins).all() or np.any(bins < 0)):
        raise ValueError('Density display needs 2D points, increasing bin edges, matching counts and a positive limit')
    visible = np.isfinite(points).all(axis=1)
    for axis, edge in enumerate(edges):
        visible &= (points[:, axis] >= edge[0]) & (points[:, axis] <= edge[-1])
    points = points[visible]
    indices = [np.minimum(np.searchsorted(edge, points[:, axis], side='right') - 1, len(edge) - 2)
               for axis, edge in enumerate(edges)]
    densities = bins[indices[0], indices[1]]
    flat = indices[0] * bins.shape[1] + indices[1]
    occupied, first = np.unique(flat, return_index=True)
    chosen = np.arange(len(points))
    sampling = 'all'
    if len(points) > limit:
        # Stable tie-break by bin identity; rare populations remain visible even
        # when the acquisition order groups them after a large dense population.
        priority = np.lexsort((occupied, bins.reshape(-1)[occupied]))
        chosen = first[priority[:limit]]
        remaining_slots = limit - len(chosen)
        if remaining_slots:
            available = np.ones(len(points), dtype=bool)
            available[chosen] = False
            remaining = np.flatnonzero(available)
            fill = remaining[np.linspace(0, len(remaining) - 1, remaining_slots, dtype=int)]
            chosen = np.concatenate((chosen, fill))
        chosen.sort()
        sampling = 'sparse-bin-representatives-then-even'
    return {
        'x': points[chosen, 0].tolist(), 'y': points[chosen, 1].tolist(),
        'density': densities[chosen].tolist(),
        'eligible_count': len(points), 'rendered_count': len(chosen),
        'occupied_bins': len(occupied), 'represented_bins': len(np.unique(flat[chosen])),
        'sampling': sampling, 'density_estimator': 'full-population histogram2d',
    }
