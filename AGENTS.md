# Repository notes

AutoEUDM is a local web UI I developed to speed up device-management work in EUDM. Keep the repository focused on that web workflow.

- After completing requested work, run proportionate checks, commit the intended changes, and push to `origin/main`.
- Keep the repository on `main` unless the user explicitly requests another branch.
- Preserve unrelated user changes and mention blockers before stopping.

## Update notes

- Include one concise Markdown note in `update-notes/` with every commit. Use a unique `YYYY-MM-DD-short-description.md` filename, one clear heading, and one to three plain-language bullets. Keep it brief; skip implementation detail and release-note ceremony.
- Write notes for the person using AutoEUDM. For internal-only commits, say briefly what maintenance changed.
- The app shows notes added between the installed commit and the selected update channel before applying an update.
- Push normal development work to `main`. Keep `stable` as the release channel and move it forward only when intentionally publishing a release.

## Project map

- `eudm_web.py`: small web entry point.
- `start_auto_eudm.py`: launcher startup, background supervision, environment setup, and stale-server detection.
- `src/auto_eudm/eudm_web.py`: local server startup.
- `src/auto_eudm/web_server.py`: localhost HTTP API and static-file serving.
- `src/auto_eudm/local_service.py`: background-service controls, macOS login launch agent, Git branch monitoring, and update/restart handling.
- `src/auto_eudm/web_runtime.py`: queue, history, drafts, settings, verification cache, imports, searches, and submission jobs.
- `src/auto_eudm/state_database.py`: per-instance SQLite schema, relational repositories, and one-time legacy migration.
- `src/auto_eudm/data_paths.py`: operating-system application-support paths and instance separation.
- `src/auto_eudm/web_models.py`: request and workbook data models plus validation.
- `src/auto_eudm/eudm_request.py`: authenticated EUDM operations and browser-session handoff.
- `src/auto_eudm/eudm_inventory_import.py`: shared ALM workbook parsing and row rules.
- `src/auto_eudm/pc_toolkit.py`: optional read-only PC Toolkit enrichment, ranking, authentication handoff, and filesystem cache.
- `web/`: markup, styling, and browser-side interaction.
- `web/components.js` / `components.css`: adapters for locally bundled Idiomorph, Tom Select and Tippy/Popper. State-driven queue/ALM rendering preserves nodes and focus; use `DeploymentComponents.listen` when rebinding retained nodes, and `selectOptions` when changing searchable picker options.
- `scripts/vendor_web.mjs`: refresh tracked browser bundles/licenses with `npm ci` then `npm run vendor:web` on the development machine. The deployed browser UI needs no CDN or Node runtime.
- `scripts/check_alm_ui.py`: optional Chrome/Playwright ALM workflow checks. Run only against an isolated simulator: it resets that instance's queue, drafts and backlog ignores. Covers mapping, Excel/CSV, corrections, resume, enrichment retries and queue-save recovery.
- `launchers/`: double-clickable web startup files for macOS, Windows, and PowerShell.
- `requirements/`: optional spreadsheet and browser dependencies installed at startup.
- `results/`: legacy runtime-state source used for a one-time migration; do not add new durable app state here.
- `tests/`: standard-library tests for the web app and its supporting logic.

## Important behaviours

- EUDM authentication is fail-closed. Live searches and submissions require a connected session; `EUDM_SIMULATE=true` enables local simulation.
- Helix's `sessionstatus` response is not proof of an authenticated API session. Let the opened Helix app establish its own web-client session, then verify the EUDM catalogue and carts APIs before reporting success. Helix API requests use the site-root origin/referrer and the authenticating Chrome user agent.
- The in-memory diagnostics capture runs for the current server session. It records compact API request/response bodies and safe headers, and exports only the most recent five minutes as a gzip file from Settings. Credential-bearing fields remain redacted; request serials, usernames, form answers, and error responses are retained.
- Authentication status and visible retry actions live in the top bar; do not block the workspace with an authentication dialog. Optional headless authentication is monitored by the server while no web page is open. After three consecutive failures it pauses until the user clicks the top-bar status for a visible Chrome retry. Helix has priority over PC Toolkit's shared Chrome profile.
- Durable state belongs in a per-instance SQLite database under the operating system's Deployments application-support directory, not in the checkout or browser storage. The default instance imports existing `results/` JSON/workbook files once, transactionally, while keeping every source file as a recovery copy. Other instance IDs use isolated databases and never import the default instance's files.
- SQLite is the data model, not a container for the former JSON files. Keep settings, requests, serials, people, validation, history, ALM workbook metadata/drafts, verification aliases, and PC Toolkit device/person/hardware records in their domain tables with foreign keys, checks, unique constraints, indexes, and atomic transactions. Workbook bytes are the one intentional BLOB; do not introduce generic JSON/KV/blob state tables or persist arbitrary request documents. Browser-facing dictionaries are reconstructed at the API boundary.
- Add a numbered schema migration whenever tables or columns change; keep upgrades safe for an already-created database. Use one consistent read snapshot for related rows, and transactionally mutate shared queue state so windows/processes cannot overwrite one another.
- Windows connected to one server share queue, preferences, and submission progress. Preserve queue three-way merging, SQL serial uniqueness, and request-ID duplicate protection so concurrent windows cannot overwrite edits or submit the same request twice.
- ALM inventory imports accept `.xlsx`, `.xlsm`, and `.csv`/CSV UTF-8. CSV uses the same heading mapping but has one value-only table, so sheet selection, date-fill sections, and font-colour status hints do not apply.
- Every ALM inventory upload and mapped parse attempt writes a detailed, content-safe diagnostic under that instance's application-support directory; the import status exposes its path on failure.
- PC Toolkit requests, browser navigation, SSO/API responses, cache events, and exception chains are saved as credential-safe, schema-versioned JSON-lines under that instance's application-support directory; Settings can download the current session log. Response/request bodies and correlation IDs are retained for troubleshooting, while credential-bearing fields are redacted and oversized bodies carry a hash plus an explicit truncation marker.
- PC Toolkit device lookups default to a real Chrome page using the same dedicated profile as sign-in. Puppeteer is an optional real-Chrome transport for machines where it is more reliable; Direct API remains an explicit fallback in Settings. Portal role/heartbeat authentication does not use a fabricated serial probe; the first real lookup validates the device API. Helix always authenticates first and explicitly pauses PC Toolkit before reauthentication so the optional service never holds the shared profile while Helix needs it. Browser-backed PC Toolkit bulk lookups run inside Chrome in concurrent batches of up to 60, retry only the failed subset, refresh the bearer token in-page, and may fall back to cached enrichment. The Puppeteer transport uses the tracked `puppeteer-core` dependency and `src/auto_eudm/pc_toolkit_puppeteer.cjs` JSON-lines bridge.
- Submission jobs are asynchronous. Preserve queue state, progress, request IDs, and failed rows if the UI is closed while a job runs.
- Failed deployments stay in the shared queue after results are dismissed. Retry failed entries with fresh client IDs, keep successful entries out after the user acts on results, and make Helix recovery clear stale clients before visible Chrome authentication.
- The command launchers start AutoEUDM as a detached background service. Its supervisor restarts the web process after updates; Settings can stop the service or enable launch at login. Update requests must use fast-forward Git pulls and refuse to overwrite a dirty checkout. Service control files and diagnostics belong under per-instance application support.
- `scripts/run_lan_test_server.py` manages a separate macOS login service on port 8766 for the LAN simulator. It must always set `EUDM_SIMULATE=true`, use the isolated `lan-test` database, bind only to a user-specified RFC1918 subnet, and pause when this Mac is outside that subnet. It watches local source files and restarts the server after changes. Never let this service read real EUDM environment files or share the default instance's database/browser profile.
- Update checks use the `stable` branch by default. Settings can switch to `development` to track `main`; the same Git fetch, fast-forward, and supervised restart flow is used by the Windows launchers.
- Workbook columns are selected by heading. ALM drafts must be saved while editing and removed when their requests enter the queue; late verification must not recreate a completed draft.
- Validation remains active even when cached verification fills a result immediately.
- PC Toolkit enriches Helix data but never replaces Helix validation or blocks submission. Its query cache, device/person/hardware results, and automatically discovered model catalogue are normalized in SQLite; model-to-status mappings are normalized preference rows. Query cache records through the indexed lookup key on demand; do not load the entire cache at server startup.
- Max Portal request lookup is read-only and uses the authenticated PC Toolkit session. Match only exact usernames, rank active INCs ahead of closed/cancelled requests, retain the chosen request details with the deployment, and do not attempt ticket closure until that workflow is explicitly captured and implemented.

## Checks

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
PYTHONPATH=src .venv/bin/python -m unittest tests.test_state_database -v
PYTHONPATH=src .venv/bin/python -m compileall -q src tests
node --check web/app.js
git diff --check
```
