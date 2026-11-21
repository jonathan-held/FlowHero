# Contributing

Bug reports and suggestions are welcome as GitHub issues.

For a bug report, please include: the FlowHero version (Help > About), operating system, the exact steps, the complete error text from the message area, and if possible a small project that shows the problem (File > Save portable package…). Do not attach confidential or patient data. A portable package contains the FCS files of the project and the folder paths they came from.

## Development setup

```bash
python -m venv .venv
.venv/bin/python -m pip install -e . pytest        # Windows: .venv\Scripts\python
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
```

Python 3.10 to 3.12. The suite needs no display and no data files; it writes synthetic FCS files.

## Rules for changes

- Numbers come first. A change to gating, compensation, scales or statistics needs a test that compares the result with an independent calculation (NumPy on the raw events).
- Statistics use every event of a population and untransformed values. Display options must not change them.
- Saved projects must keep opening. A change to the project file raises `storage.VERSION` and keeps reading all earlier versions.
- New functions should not add controls to the main window for users who do not need them; prefer the right-click menus.
- State in the pull request what was run and on which system, and what was not checked.
