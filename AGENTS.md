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
- `src/auto_eudm/pc_toolkit.py`: optional read-only PC Toolkit enrichment, ranking, authentication handoff, and filesystem cache.
- `web/`: markup, styling, and browser-side interaction.
- `launchers/`: double-clickable web startup files for macOS, Windows, and PowerShell.
- `requirements/`: optional spreadsheet and browser dependencies installed at startup.
- `results/`: gitignored runtime state, including the queue, history, drafts, settings, and verification cache.
- `tests/`: standard-library tests for the web app and its supporting logic.

## Important behaviours

- EUDM authentication is fail-closed. Live searches and submissions require a connected session; `EUDM_SIMULATE=true` enables local simulation.
- Helix's `sessionstatus` response is not proof of an authenticated API session. Let the opened Helix app establish its own web-client session, then verify the EUDM catalogue and carts APIs before reporting success. Helix API requests use the site-root origin/referrer and the authenticating Chrome user agent.
- The in-memory diagnostics capture runs for the current server session. It records compact API request/response bodies and safe headers, and exports only the most recent five minutes as a gzip file from Settings or the authentication sheet. Credential-bearing fields remain redacted; request serials, usernames, form answers, and error responses are retained.
- Queue entries, request history, ALM drafts, settings, verification cache, and backlog exclusions belong in `results/`, not browser storage.
- ALM inventory imports accept `.xlsx`, `.xlsm`, and `.csv`/CSV UTF-8. CSV uses the same heading mapping but has one value-only table, so sheet selection, date-fill sections, and font-colour status hints do not apply.
- Every ALM inventory upload and mapped parse attempt writes a detailed, content-safe diagnostic to `results/alm-workbook-load-logs/`; the import status exposes its path on failure.
- PC Toolkit requests, connection probes, browser navigation, SSO/API responses, cache events, and exception chains are saved as credential-safe, schema-versioned JSON-lines in `results/pc-toolkit-logs/`; Settings can download the current session log. Response/request bodies and correlation IDs are retained for troubleshooting, while credential-bearing fields are redacted and oversized bodies carry a hash plus an explicit truncation marker.
- PC Toolkit device lookups must retain the production Chrome fetch headers captured from its portal. Portal role discovery alone is not a successful connection: verify the device API after browser authentication before reporting PC Toolkit as ready, and wait for the shared Chrome profile to become free rather than repeatedly opening blank windows.
- Submission jobs are asynchronous. Preserve queue state, progress, request IDs, and failed rows if the UI is closed while a job runs.
- Workbook columns are selected by heading. ALM drafts must be saved while editing and removed when their requests enter the queue; late verification must not recreate a completed draft.
- Validation remains active even when cached verification fills a result immediately.
- PC Toolkit enriches Helix data but never replaces Helix validation or blocks submission. Its compact cache and automatically discovered model catalogue live in `results/pc-toolkit-cache.json`; model-to-status mappings live in settings.

## Checks

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
PYTHONPATH=src .venv/bin/python -m compileall -q src tests
node --check web/app.js
git diff --check
```
