# Contributing

## Setup

Use Python 3.10 or newer and run commands from the repository root.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Do not commit credentials, generated benchmark output, logs, virtual environments,
or provider responses. Generated benchmark files belong under `artifacts/`.

## Changes

- Keep runtime modules at the root unless a package migration is intentional.
- Put evaluation harnesses in `benchmarks/` and user-facing documentation in `docs/`.
- Update README links and commands when moving files.
- Keep write operations behind the existing approval boundary.
- Add direct dependencies to `requirements.txt`; do not rely on transitive installs.
- Use short imperative or Conventional Commit subjects when committing changes.

## Checks

Run the offline checks before opening a pull request:

```powershell
python -m compileall -q agent.py spotify_mcp.py psych_mcp.py streamlit_app.py run_tool.py get_token.py benchmarks tests
python benchmarks/bakeoff.py --selfcheck
python benchmarks/bakeoff_jev.py --selfcheck
python benchmarks/bakeoff_jev_40.py --selfcheck
python tests/test_ui.py
```

Live benchmark runs use external APIs, may cost money, and can modify local artifact
files. State clearly when they were run and summarize curated findings under
`docs/benchmarks/`.
