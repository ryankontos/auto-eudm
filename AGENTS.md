# Repository notes

AutoEUDM is a local web UI I developed to speed up device-management work in EUDM. Keep the repository focused on that web workflow.

- After completing requested work, run proportionate checks, commit the intended changes, and push to `origin/main`.
- Keep the repository on `main` unless the user explicitly requests another branch.
- Preserve unrelated user changes and mention blockers before stopping.

## Project map

- `eudm_web.py`: small web entry point.
- `start_auto_eudm.py`: cross-platform startup, environment setup, and stale-server detection.
- `src/auto_eudm/eudm_web.py`: local server startup.
- `src/auto_eudm/web_server.py`: localhost HTTP API and static-file serving.
- `src/auto_eudm/web_runtime.py`: queue, history, drafts, settings, verification cache, imports, searches, and submission jobs.
- `src/auto_eudm/web_models.py`: request and workbook data models plus validation.
- `src/auto_eudm/eudm_request.py`: authenticated EUDM operations and browser-session handoff.
- `src/auto_eudm/eudm_inventory_import.py`: shared ALM workbook parsing and row rules.
- `web/`: markup, styling, and browser-side interaction.
- `launchers/`: double-clickable web startup files for macOS, Windows, and PowerShell.
- `requirements/`: optional spreadsheet and browser dependencies installed at startup.
- `results/`: gitignored runtime state, including the queue, history, drafts, settings, and verification cache.
- `tests/`: standard-library tests for the web app and its supporting logic.

## Important behaviours

- EUDM authentication is fail-closed. Live searches and submissions require a connected session; `EUDM_SIMULATE=true` enables local simulation.
- Queue entries, request history, ALM drafts, settings, verification cache, and backlog exclusions belong in `results/`, not browser storage.
- Submission jobs are asynchronous. Preserve queue state, progress, request IDs, and failed rows if the UI is closed while a job runs.
- Workbook columns are selected by heading. ALM drafts must be saved while editing and removed when their requests enter the queue; late verification must not recreate a completed draft.
- Validation remains active even when cached verification fills a result immediately.

## Checks

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
PYTHONPATH=src .venv/bin/python -m compileall -q src tests
node --check web/app.js
git diff --check
```
