"""Serializable scientific definitions; GUI and engine share these facts."""
from dataclasses import dataclass, field
import math
from uuid import uuid4

from . import scales as scale_rules

DOMAIN = "flowkit-preprocessed-uncompensated"
COMPENSATED_DOMAIN = "flowkit-preprocessed-compensated"
DOMAINS = (DOMAIN, COMPENSATED_DOMAIN)
STATISTICS = ("median", "arithmetic_mean", "geometric_mean")


def valid_sha256(value):
    return isinstance(value,str) and len(value)==64 and all(c in '0123456789abcdef' for c in value)


def validate_interval(minimum, maximum):
    if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
               for x in (minimum, maximum)) or minimum >= maximum:
        raise ValueError("Range limits must be finite numbers with minimum < maximum")


def validate_scales(scales, count):
    if len(scales) != count or not all(scale_rules.valid(s) for s in scales):
        raise ValueError('Axes must declare linear, log or logicle scales')


def validate_compensation(compensation):
    """None, the spillover stored in each file, or one spillover matrix for all samples."""
    if compensation is None:
        return
    if not isinstance(compensation, dict) or compensation.get('source') not in ('file', 'matrix'):
        raise ValueError('Compensation must name its source: file or matrix')
    if compensation['source'] == 'file':
        if set(compensation) != {'source'}:
            raise ValueError('File compensation takes no further settings')
        return
    if set(compensation) != {'source', 'channels', 'values'}:
        raise ValueError('A compensation matrix needs channels and values')
    channels, values = compensation['channels'], compensation['values']
    if (not isinstance(channels, list) or len(channels) < 2 or len(set(channels)) != len(channels)
            or not all(isinstance(c, str) and c.strip() for c in channels)):
        raise ValueError('A compensation matrix needs at least two distinct channel names')
    if (not isinstance(values, list) or len(values) != len(channels)
            or not all(isinstance(row, list) and len(row) == len(channels) for row in values)
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                       for row in values for v in row)):
        raise ValueError('The spillover matrix must be square with finite numbers')
    # Gaussian elimination with partial pivoting: reject matrices that cannot be inverted.
    rows = [[float(v) for v in row] for row in values]
    size = len(rows)
    for column in range(size):
        pivot = max(range(column, size), key=lambda r: abs(rows[r][column]))
        if abs(rows[pivot][column]) < 1e-12:
            raise ValueError('The spillover matrix cannot be inverted')
        rows[column], rows[pivot] = rows[pivot], rows[column]
        for r in range(column + 1, size):
            factor = rows[r][column] / rows[column][column]
            rows[r] = [a - factor * b for a, b in zip(rows[r], rows[column])]


def operands_of(gate):
    return getattr(gate, 'operands', ())


def ordered_gates(gates):
    by_id = {g.id: g for g in gates}
    if len(by_id) != len(gates) or 'root' in by_id or '' in by_id:
        raise ValueError('Gate identities must be unique and cannot be root')
    for g in gates:
        if g.parent_id is not None and g.parent_id not in by_id:
            raise ValueError('Gate references a missing parent')
        for operand in operands_of(g):
            if operand not in by_id or by_id[operand].parent_id != g.parent_id:
                raise ValueError('A combined gate must combine existing gates of its own parent population')
    remaining = list(gates)
    ordered, seen = [], set()
    while remaining:
        ready = [g for g in remaining if (g.parent_id is None or g.parent_id in seen)
                 and all(operand in seen for operand in operands_of(g))]
        if not ready:
            raise ValueError('Gate hierarchy contains a cycle')
        ordered.extend(ready)
        seen.update(g.id for g in ready)
        remaining = [g for g in remaining if g.id not in seen]
    return ordered


def validate_quadrant_groups(gates):
    identities = {gate.id for gate in gates}
    groups = {}
    for gate in gates:
        if not isinstance(gate, Rectangle) or not gate.group_id:
            continue
        if gate.group_id in identities:
            raise ValueError('Quadrant group identity must differ from gate identities')
        if any((lo is None) == (hi is None) for lo, hi in zip(gate.minimum, gate.maximum)):
            raise ValueError('Quadrant group members need exactly one finite bound per dimension')
        contract = (gate.parent_id, gate.channels, gate.scales,
                    tuple(lo if lo is not None else hi for lo, hi in zip(gate.minimum, gate.maximum)))
        if gate.group_id in groups and groups[gate.group_id] != contract:
            raise ValueError('Quadrant group members must share parent, channels, scales and crossing')
        groups[gate.group_id] = contract


@dataclass(frozen=True)
class Rectangle:
    id: str
    name: str
    parent_id: str | None
    channels: tuple[str, str]
    minimum: tuple[float | None, float | None]
    maximum: tuple[float | None, float | None]
    scales: tuple[str, str] = ('linear', 'linear')
    group_id: str | None = None

    def __post_init__(self):
        validate_scales(self.scales, 2)
        if (not isinstance(self.id, str) or not self.id or not isinstance(self.name, str) or not self.name.strip()
                or len(self.channels) != 2 or len(set(self.channels)) != 2
                or not all(isinstance(c, str) and c for c in self.channels)):
            raise ValueError('Rectangle needs identity, name and two distinct channels')
        if self.group_id is not None and (not isinstance(self.group_id, str) or not self.group_id):
            raise ValueError('Invalid quadrant group identity')
        if len(self.minimum) != 2 or len(self.maximum) != 2:
            raise ValueError('Rectangle requires two bounds per dimension')
        for lo, hi in zip(self.minimum, self.maximum):
            for value in (lo, hi):
                if value is not None and (isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value)):
                    raise ValueError('Rectangle bounds must be finite or open')
            if lo is not None and hi is not None:
                validate_interval(lo, hi)


@dataclass(frozen=True)
class PlotSpec:
    id: str
    kind: str
    x_channel: str
    y_channel: str = ''
    population_id: str | None = None
    x_scale: str = 'linear'
    y_scale: str = 'linear'
    x_limits: tuple[float, float] | None = None
    y_limits: tuple[float, float] | None = None
    bins: int = 128
    geometry: tuple[int, int, int, int] = (0, 0, 540, 400)
    sample_ids: tuple[str, ...] = ()
    normalization: str = 'count'
    contours: bool = False
    stagger: bool = False
    overlay: bool = False
    pinned: bool = False
    legend: bool = True

    def __post_init__(self):
        if not self.id or self.kind not in ('histogram', 'scatter', 'density') or not self.x_channel:
            raise ValueError('Plot needs an identity, kind and channel')
        if self.normalization not in ('count', 'event_fraction', 'peak'):
            raise ValueError('Histogram normalization must be count, event_fraction or peak')
        if type(self.contours) is not bool:
            raise ValueError('Density contours must be true or false')
        if type(self.stagger) is not bool or type(self.overlay) is not bool:
            raise ValueError('Histogram offset and sample overlay must be true or false')
        if type(self.pinned) is not bool or type(self.legend) is not bool:
            raise ValueError('Fixed sample list and legend settings must be true or false')
        if self.pinned and not (self.kind == 'histogram' and self.sample_ids):
            raise ValueError('Only a histogram with at least one sample can keep a fixed sample list')
        if self.kind == 'histogram' and self.y_scale not in ('linear', 'log'):
            raise ValueError('The count axis of a histogram is linear or log')
        if self.sample_ids and not (self.kind == 'histogram' or (self.kind == 'scatter' and self.overlay)):
            raise ValueError('Sample overlays need a histogram or a scatter plot with overlay switched on')
        if self.kind != 'histogram' and (not self.y_channel or self.y_channel == self.x_channel):
            raise ValueError('Choose two distinct channels for a 2D plot')
        validate_scales((self.x_scale, self.y_scale), 2)
        for limits, scale in [(self.x_limits, self.x_scale), (self.y_limits, self.y_scale)]:
            if limits is not None:
                if len(limits) != 2:
                    raise ValueError('Axis needs two limits')
                validate_interval(*limits)
                if scale_rules.requires_positive(scale) and limits[0] <= 0:
                    raise ValueError('Logarithmic limits must be positive')
        if type(self.bins) is not int or not 8 <= self.bins <= 256:
            raise ValueError('Plots require 8–256 bins')
        if len(self.geometry) != 4 or not all(type(v) is int for v in self.geometry) or not (240 <= self.geometry[2] <= 10000 and 180 <= self.geometry[3] <= 10000):
            raise ValueError('Invalid plot geometry')
        if (not all(isinstance(s, str) and s for s in self.sample_ids)
                or len(set(self.sample_ids)) != len(self.sample_ids)):
            raise ValueError('The samples of a plot must be distinct')


@dataclass(frozen=True)
class Source:
    id: str
    path: str
    sha256: str


@dataclass(frozen=True)
class Polygon:
    id: str
    name: str
    parent_id: str | None
    channels: tuple[str, str]
    vertices: tuple[tuple[float, float], ...]
    scales: tuple[str, str] = ("linear", "linear")

    def __post_init__(self):
        validate_scales(self.scales, 2)
        if not self.id or not self.name.strip():
            raise ValueError("Gate identity and name must be nonempty")
        if len(self.channels) != 2 or len(set(self.channels)) != 2 or not all(self.channels):
            raise ValueError("Polygon needs two distinct named channels")
        if len(self.vertices) < 3 or any(len(v) != 2 for v in self.vertices):
            raise ValueError("Polygon needs at least three two-dimensional vertices")
        if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
                   for v in self.vertices for x in v):
            raise ValueError("Polygon coordinates must be finite numbers")
        if len(set(self.vertices)) < 3:
            raise ValueError("Polygon needs three distinct vertices")
        # Reject zero-area geometry rather than reporting a misleading empty gate.
        area = sum(a[0]*b[1] - b[0]*a[1]
                   for a, b in zip(self.vertices, self.vertices[1:] + self.vertices[:1]))
        if area == 0:
            raise ValueError("Polygon has zero signed area")


@dataclass(frozen=True)
class Ellipse:
    """Centre plus two conjugate half-axes in gate coordinates.

    The boundary is ``center + axes[0]*cos(t) + axes[1]*sin(t)``. Any two
    independent half-axes describe an ellipse, so tilting needs no extra rule.
    """
    id: str
    name: str
    parent_id: str | None
    channels: tuple[str, str]
    center: tuple[float, float]
    axes: tuple[tuple[float, float], tuple[float, float]]
    scales: tuple[str, str] = ("linear", "linear")

    def __post_init__(self):
        validate_scales(self.scales, 2)
        if not isinstance(self.id, str) or not self.id or not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Gate identity and name must be nonempty")
        if len(self.channels) != 2 or len(set(self.channels)) != 2 or not all(isinstance(c, str) and c for c in self.channels):
            raise ValueError("Ellipse needs two distinct named channels")
        if len(self.center) != 2 or len(self.axes) != 2 or any(len(a) != 2 for a in self.axes):
            raise ValueError("Ellipse needs a centre and two half-axes")
        numbers = list(self.center) + [v for a in self.axes for v in a]
        if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in numbers):
            raise ValueError("Ellipse coordinates must be finite numbers")
        (ux, uy), (vx, vy) = self.axes
        size = max(abs(ux), abs(uy), abs(vx), abs(vy))
        if size == 0 or abs(ux * vy - uy * vx) <= 1e-12 * size * size:
            raise ValueError("Ellipse has no area")

    def covariance(self):
        (ux, uy), (vx, vy) = self.axes
        return ((ux * ux + vx * vx, ux * uy + vx * vy), (ux * uy + vx * vy, uy * uy + vy * vy))


@dataclass(frozen=True)
class BooleanGate:
    """Events of the parent that are in all / any / none of the named sibling gates."""
    id: str
    name: str
    parent_id: str | None
    operation: str
    operands: tuple[str, ...]

    def __post_init__(self):
        if not isinstance(self.id, str) or not self.id or not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Gate identity and name must be nonempty")
        if self.operation not in ('and', 'or', 'not'):
            raise ValueError("A combined gate uses and, or, or not")
        if (not isinstance(self.operands, tuple) or not all(isinstance(o, str) and o for o in self.operands)
                or len(set(self.operands)) != len(self.operands) or self.id in self.operands):
            raise ValueError("A combined gate needs distinct other gates")
        if (self.operation == 'not' and len(self.operands) != 1) or (self.operation != 'not' and len(self.operands) < 2):
            raise ValueError("NOT takes one gate; AND and OR take two or more")

    @property
    def channels(self):
        return ()

    @property
    def scales(self):
        return ()


@dataclass(frozen=True)
class RangeGate:
    id: str
    name: str
    parent_id: str | None
    channel: str
    minimum: float
    maximum: float
    scales: tuple[str, ...] = ("linear",)

    def __post_init__(self):
        validate_scales(self.scales, 1)
        if self.parent_id is not None and not isinstance(self.parent_id, str):
            raise ValueError("Invalid range parent")
        if not all(isinstance(x, str) and x.strip()
                   for x in (self.id, self.name, self.channel)):
            raise ValueError("Range gate needs identity, name, parent and channel")
        validate_interval(self.minimum, self.maximum)

    @property
    def channels(self):
        return (self.channel,)


@dataclass(frozen=True)
class Histogram:
    gate_id: str
    minimum: float
    maximum: float
    bins: int = 128
    normalized: bool = False
    sample_ids: tuple[str, ...] = ()

    def __post_init__(self):
        validate_interval(self.minimum, self.maximum)
        if type(self.bins) is not int or not 2 <= self.bins <= 512:
            raise ValueError("Histogram needs 2–512 bins")
        if type(self.normalized) is not bool or not isinstance(self.gate_id, str):
            raise ValueError("Invalid histogram settings")
        if (not isinstance(self.sample_ids, tuple)
                or not all(isinstance(s, str) for s in self.sample_ids)
                or len(set(self.sample_ids)) != len(self.sample_ids)):
            raise ValueError("Choose distinct samples for overlays")


@dataclass
class Project:
    sources: list[Source] = field(default_factory=list)
    gates: list[Polygon] = field(default_factory=list)
    channel: str = ""
    domain: str = DOMAIN
    revision: int = 0
    statistic: str = "median"
    viability: RangeGate | None = None
    overrides: dict[str, list[Polygon | RangeGate]] = field(default_factory=dict)
    histogram: Histogram | None = None
    plots: list[PlotSpec] = field(default_factory=list)
    analysis: dict | None = None
    gate_imports: list[dict] = field(default_factory=list)
    annotations: dict[str, dict[str, str]] = field(default_factory=dict)
    compensation: dict | None = None

    def default_gates(self):
        return list(self.gates) + ([self.viability] if self.viability else [])

    def gates_for(self, source_id):
        replacements = {g.id: g for g in self.overrides.get(source_id, [])}
        return [replacements.get(g.id, g) for g in self.default_gates()]

    def set_override(self, source_id, gate):
        gates = [g for g in self.overrides.get(source_id, []) if g.id != gate.id]
        self.overrides[source_id] = gates + [gate]

    def validate(self):
        validate_compensation(self.compensation)
        if self.domain != (COMPENSATED_DOMAIN if self.compensation else DOMAIN):
            raise ValueError(f"Analysis domain does not match the compensation setting: {self.domain}")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("Revision must be a nonnegative integer")
        if not isinstance(self.channel, str):
            raise ValueError("Statistic channel must be a string")
        if self.statistic not in STATISTICS:
            raise ValueError(f"Unsupported statistic: {self.statistic}")
        ids = set()
        for source in self.sources:
            if not source.id or source.id in ids or not source.path:
                raise ValueError("Sample identities must be unique, with a source path")
            if not valid_sha256(source.sha256):
                raise ValueError("Invalid source SHA-256")
            ids.add(source.id)
        ordered_gates(self.default_gates())
        validate_quadrant_groups(self.default_gates())
        if self.viability is not None:
            if not isinstance(self.viability, RangeGate) or not self.gates or self.viability.parent_id not in {g.id for g in self.gates}:
                raise ValueError('Viability must reference a polygon parent')
        defaults = {g.id: g for g in self.default_gates()}
        source_ids = {s.id for s in self.sources}
        if not isinstance(self.overrides, dict):
            raise ValueError("Invalid sample overrides")
        for source_id, gates in self.overrides.items():
            if source_id not in source_ids or not isinstance(gates, list):
                raise ValueError("Overrides must reference an existing sample")
            seen = set()
            for gate in gates:
                default = defaults.get(gate.id)
                if (default is None or type(default) is not type(gate) or gate.id in seen or isinstance(gate, BooleanGate)
                        or gate.name != default.name or gate.parent_id != default.parent_id
                        or gate.channels != default.channels or gate.scales != default.scales):
                    raise ValueError("Overrides may change gate geometry only, with unique existing identities")
                seen.add(gate.id)
            validate_quadrant_groups(self.gates_for(source_id))
        if self.compensation and (self.viability is not None or self.histogram is not None):
            raise ValueError('Compensation is not available for projects of the first FlowHero window')
        if self.histogram is not None:
            if (not isinstance(self.histogram, Histogram) or self.histogram.gate_id not in defaults
                    or not set(self.histogram.sample_ids) <= source_ids or not self.channel):
                raise ValueError("Histogram must reference an existing gate, channel and samples")

        if len({p.id for p in self.plots}) != len(self.plots):
            raise ValueError('Plot identities must be unique')
        for plot in self.plots:
            if plot.population_id is not None and plot.population_id not in defaults:
                raise ValueError('Plot references a missing population')
            if not set(plot.sample_ids) <= source_ids:
                raise ValueError('Plot references missing samples')

        if not isinstance(self.annotations, dict):
            raise ValueError('Invalid sample annotations')
        for source_id, values in self.annotations.items():
            if source_id not in source_ids or not isinstance(values, dict) or not all(
                    isinstance(k, str) and k.strip() and isinstance(v, str) for k, v in values.items()):
                raise ValueError('Annotations must map existing samples to named text values')

        from .gate_import import validate_import_history
        validate_import_history(self.gate_imports)

        if self.analysis is not None:
            from .review import validate_analysis
            validate_analysis(self)

    def edited(self):
        self.revision += 1


def new_id() -> str:
    return uuid4().hex
