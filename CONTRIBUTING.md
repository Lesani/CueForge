# Contributing to CueForge

Thanks for your interest in improving CueForge! This guide covers how to get a
development environment running, how to test your changes, and what to expect
when opening a pull request.

## Development setup

CueForge is developed on Windows with Python 3.13, but the core logic is
cross-platform.

```sh
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

`ffmpeg` is downloaded automatically on first use, so no manual install is
required. To run the app straight from source:

```sh
.venv/Scripts/python.exe -m cueforge
```

Then open the printed URL (default http://localhost:7070/). The web UI has **no
build step** — it is plain ES-module JavaScript served directly from
`cueforge/web/`, so edits are picked up on reload.

## Running the tests

Python suite (mixer/fades, project model + storage, import/dedup, cue traversal,
server reducer):

```sh
.venv/Scripts/python.exe -m pytest
```

Web unit tests (run with Node's built-in test runner):

```sh
node --test "tests/web/**/*.test.mjs"
```

Please make sure both suites pass before submitting a change, and add tests for
new behavior where it is reasonable to do so.

## Code style

- **Python:** follow the style of the surrounding code (PEP 8, type hints,
  focused modules with clear docstrings). Keep functions small and testable.
- **JavaScript:** vanilla ES modules, no framework, no bundler. Match the
  existing structure under `cueforge/web/js/`.
- Keep changes focused. Unrelated refactoring is best kept in a separate PR.

## Pull requests

1. Fork the repo and create a feature branch from `main`.
2. Make your change, with tests and a clear commit message.
3. Ensure `pytest` and `node --test "tests/web/**/*.test.mjs"` both pass.
4. Open a pull request describing **what** changed and **why**. Link any related
   issue.

Small, well-scoped PRs are easier to review and get merged faster. If you are
planning a larger change, please open an issue first to discuss the approach.

## License

By contributing, you agree that your contributions will be licensed under the
project's [AGPL-3.0-or-later](LICENSE) license.
