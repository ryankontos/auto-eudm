# AutoEUDM architecture map

Read this file before changing the project. It is the shortest route to the
right owner and avoids scanning the whole repository.

## Start here

| Need | Primary file |
| --- | --- |
| Launch the local web app | `eudm_web.py` → `src/auto_eudm/eudm_web.py` |
| Shared environment/configuration | `src/auto_eudm/eudm_config.py` |
| EUDM API workflow and request submission | `src/auto_eudm/eudm_request.py` |
| Browser SSO/cookie setup | `src/auto_eudm/eudm_request.py` and `src/auto_eudm/bootstrap.py` |
| Web UI state, EUDM searches, jobs, history, preferences, workbook imports | `src/auto_eudm/web_runtime.py` |
| Local HTTP API and static files | `src/auto_eudm/web_server.py` |
| Request data shapes and browser-facing validation | `src/auto_eudm/web_models.py` |
| Spreadsheet parsing and row rules | `src/auto_eudm/eudm_inventory_import.py` |
| Web markup, styles and interaction code | `web/index.html`, `web/app.css`, `web/app.js` |
| Result files and logging | `src/auto_eudm/run_reporting.py`, `results/`, `logs/` |

## Web application flow

```text
launcher / eudm_web.py
  → web_server.py          HTTP routes + static assets
  → web_runtime.py         application state + EUDM work
      → eudm_request.py    authenticated BMC/EUDM API calls
      → eudm_inventory_import.py  workbook parsing
  → web_models.py          queue/request validation
```

`web/app.js` calls only `/api/...` routes. `web_server.py` should stay thin:
validate HTTP input, delegate to `Application`, and return JSON. Put business
rules in `web_runtime.py` and request-shape rules in `web_models.py`.

## Important constraints

- Simulation is controlled by shared configuration; do not add client-side
  simulation switches.
- EUDM session failures must fail closed and prompt reconnection.
- Location requests may include a returning user. The return details must be
  searched, shown and confirmed before submitting because EUDM emails them.
- Workbook columns are selected by heading, never by hard-coded indexes.
- Do not reintroduce legacy project names or compatibility wrappers.

## Working safely

1. Make the smallest change in the owning module above.
2. Keep the HTTP route stable unless the browser code changes in the same task.
3. Run `python3 -m py_compile src/auto_eudm/*.py`, `node --check web/app.js`,
   and `git diff --check` after structural changes.
4. Network and SSO workflows cannot be fully verified on a personal machine;
   preserve detailed logs in `logs/` for a work-machine capture.
