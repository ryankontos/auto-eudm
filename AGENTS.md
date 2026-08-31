# Repository workflow

- After completing requested work, run proportionate checks, commit the intended changes, and push them to `origin/main`.
- Keep the repository on `main` unless the user explicitly requests another branch.
- Preserve unrelated user changes and mention any blockers before stopping.

# Project map

AutoEUDM automates Macquarie EUDM requests. It is a dependency-light Python
localhost web app plus command-line tools; there is no JavaScript build step or
web framework.

## Where to look first

- `eudm_web.py` and `src/auto_eudm/eudm_web.py`: web entry point and startup.
- `start_auto_eudm.py`: launcher/runtime detection and stale-server restart.
- `src/auto_eudm/web_server.py`: thin localhost HTTP API and static-file server.
- `src/auto_eudm/web_runtime.py`: application state, EUDM clients, searches,
  submission jobs, request history, queue persistence, verification cache, and
  ALM draft/import persistence.
- `src/auto_eudm/web_models.py`: `RequestSpec`, `WorkbookImport`, locations,
  workbook row parsing, and queue validation.
- `src/auto_eudm/eudm_request.py`: authenticated EUDM API operations, request
  creation, device/user lookups, and browser-session handoff.
- `web/index.html`: markup; `web/app.css`: all UI styling and responsive layout;
  `web/app.js`: browser state, rendering, event handlers, and `/api/...` calls.
- `src/auto_eudm/eudm_inventory_import.py`: shared spreadsheet parsing rules.
- `launchers/`: double-clickable macOS, PowerShell, and batch launchers.
- `tests/`: standard-library `unittest` coverage for models, runtime, server,
  launchers, and CLI validation.
- `ARCHITECTURE.md` and `docs/USAGE.md`: fuller architecture and operating
  documentation.

## Main web flow

```text
launcher → eudm_web.py → web_server.py → web_runtime.py
                                      ├→ web_models.py
                                      ├→ eudm_request.py → EUDM/Chrome SSO
                                      └→ eudm_inventory_import.py → ALM workbook
browser: web/app.js → localhost /api routes → web_runtime.py
```

Keep `web_server.py` focused on HTTP parsing/status codes and keep business
rules in `web_runtime.py` or `web_models.py`. The browser must use the existing
API routes rather than reaching into Python implementation details.

## Runtime state and important behaviours

- Runtime files live under the gitignored `results/` directory: the request
  queue, request history, ALM drafts/workbook payloads, settings, verification
  cache, and backlog exclusions. Do not move these back to browser storage.
- EUDM authentication is fail-closed. Live searches/submission require a
  connected session; simulation is selected by shared environment configuration
  (`EUDM_SIMULATE=true`), not by a browser toggle.
- Submission jobs are asynchronous. The UI polls job status and must preserve
  the queue, progress, request IDs, and failed rows if a run takes time or the
  progress dialog is closed.
- Workbook columns are selected by heading, not fixed column indexes. ALM
  drafts must be saved while editing and removed when their requests are moved
  into the queue; late verification callbacks must not recreate a completed
  draft.
- Request validation prevents invalid EUDM branches, duplicate serials, and
  incomplete destinations. Preserve validation even when cached verification
  makes a result appear immediately.

## Checks and working style

After structural changes, run:

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
PYTHONPATH=src .venv/bin/python -m compileall -q src tests
node --check web/app.js
git diff --check
```

Use `rg`/`rg --files` to locate code, make edits with `apply_patch`, and avoid
writing secrets, cookies, tokens, or real user data into the repository.
