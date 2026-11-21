# Validation status

FlowHero is a development version. This page states what has been checked and what has not, so that you can judge how far to rely on it. Compare results with an established program before using them in a publication.

## What the automated tests check

The suite (`tests/`, 265 tests) runs without a display and without data files; it writes synthetic FCS files.

- Event counts of gated populations equal an independent NumPy calculation on the raw events: rectangle, polygon, range, quadrant, ellipse and combined gates; linear, log and logicle gate coordinates; no compensation, compensation from the file and from one common matrix; shared gates and per-sample adjustments; files of 6,000 and 300,000 events.
- Median, arithmetic mean and geometric mean equal NumPy on the gated, untransformed (and, where switched on, compensated) values. Undefined cases report a reason instead of a number.
- Overlaid histograms with 9, 25 and 100 files: every sample present in curves, legend, exports and statistics.
- Gates read from FlowJo workspaces written by FlowKit reproduce the counts exactly. A polygon converted from a biexponential axis to logicle differed by less than 0.2 % of events.
- Saving and reopening, all earlier project formats, templates, portable packages, Undo/Redo, cancelled and superseded calculations, export files and their provenance.

## Where it has run

| System | What | Result |
|---|---|---|
| Linux x86-64, Python 3.10, packages as in `requirements.lock`, Qt offscreen | full suite, version 0.10.2 (21 Nov 2026) | 264 passed, 1 skipped (needs PyInstaller) |
| Linux x86-64, Python 3.12, fresh install by `start_flowhero.command` (newest indirect packages, among them pandas 3.0) | full suite, version 0.10.1 (not repeated for 0.10.2) | 263 passed, 1 skipped |
| macOS 26 arm64, Python 3.12, Qt offscreen | full suite of version 0.10.0 plus independent checks of compensated statistics, empty populations and cancelled calculations | passed |
| macOS, desktop | used by hand by the author | no systematic protocol |
| Windows | - | not run |

The test workflow in `.github/workflows/tests.yml` runs the suite on Linux, macOS and Windows; see the repository's Actions tab for its current state.

## Not validated

- Windows.
- Systematic comparison with other analysis programs on real acquisitions. One real file (63,634 events) was checked for root and scatter-gate statistics against NumPy.
- A workspace saved by FlowJo itself (only FlowKit-written workspaces were tested).
- The singlet gate proposal, the time-course flag and the adaptive gate proposals on real data. They are aids with fixed formulas; review every proposal.
- Mouse interaction is not covered by automated tests (the tests send the same events Qt sends).

## Known limits

No compensation matrix from single-stain controls, no spectral unmixing, no FlowJo biex axis (converted to logicle on import), no clustering or dimensionality reduction, no plate layouts.
