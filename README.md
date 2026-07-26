# AutoEUDM

Automation for the Macquarie End User Device Management (EUDM) request form.

## Contents

- `src/auto_eudm/` — the packaged Python implementation.
- `web/` — dependency-light localhost interface assets.
- `launchers/` — double-clickable macOS `.command` launchers.
- `docs/USAGE.md` — operating guide with safety boundaries, workbook mapping, examples, and troubleshooting.
- `requirements/` — optional dependency sets for browser and spreadsheet support.
- `samples/Inventory Tracking - Sydney - Test Data.xlsx` — sample workbook in the same layout as the real file.

The captures contain internal and personal data. This repository is public by request; do not add authentication cookies, tokens, or additional sensitive captures.

See [docs/USAGE.md](docs/USAGE.md) for the complete operating guide. Every command also documents its arguments and safety behaviour through `--help`.

## Quick start on a new computer

Clone the public repository, enter it, and run one launcher command. The first
run creates `.env` from the safe simulation template, creates `.venv`, installs
the required packages, and opens the local website. Later runs reuse the same
environment.

macOS (Terminal or Finder):

```bash
git clone https://github.com/ryankontos/auto-eudm.git
cd auto-eudm
./launchers/run-auto-eudm-web.command
```

Windows PowerShell:

```powershell
git clone https://github.com/ryankontos/auto-eudm.git
cd auto-eudm
.\launchers\run-auto-eudm-web.ps1
```

Windows Command Prompt (or by double-clicking the `.bat` file):

```bat
git clone https://github.com/ryankontos/auto-eudm.git
cd auto-eudm
launchers\run-auto-eudm-web.bat
```

Python 3.10 or newer is the only prerequisite. Install it from
[python.org](https://www.python.org/downloads/) if it is not already present.
The live EUDM mode also expects Google Chrome, which the application opens with
the saved profile for SSO; simulation mode needs neither Chrome nor EUDM access.

The first-run command is also available directly as `python
start_auto_eudm.py` on either operating system.

## Automatic setup for other commands

Every launcher and Python entry point creates a local `.venv` on its first
real run, installs only the dependency set it needs, and restarts inside that
environment. Spreadsheet commands install `openpyxl`; live browser commands
install Playwright. Simulation mode avoids the browser install.

Set `EUDM_SKIP_AUTO_INSTALL=1` to manage dependencies manually, or
`EUDM_VENV_DIR=/path/to/venv` to choose another environment location.

On macOS, the `.command` files in `launchers/` are double-clickable launchers:

```bash
./launchers/run-auto-eudm-web.command
./launchers/run-eudm-request.command --help
./launchers/run-eudm-wizard.command --simulate
./launchers/run-eudm-inventory-import.command --dry-run
./launchers/run-eudm-user-returns.command --simulate
./launchers/run-eudm-location-batch.command --simulate
```

They change into the repository folder before running, start with the available
`python3` (which performs the automatic setup above), and pass arguments
through to the underlying CLI. If macOS blocks a newly downloaded launcher,
right-click it in Finder and choose Open once.

## Local web workspace

For most work, double-click `launchers/run-auto-eudm-web.command` or run:

```bash
./launchers/run-auto-eudm-web.command
```

AutoEUDM opens at `http://127.0.0.1:8765`. Keep the launcher’s Terminal window
open while using it. The server binds only to this computer and uses plain
HTML, CSS, JavaScript, and Python; there is no Node build or web framework to
install.

Running the web launcher again is safe: if the local server is already running,
the command opens that existing workspace in the browser instead of starting a
duplicate server. Use `--no-open` when you intentionally want no browser tab.

The web workspace is designed for preparing many requests quickly:

- follow the computer's light or dark appearance automatically, with a remembered
  header toggle for a temporary override;
- add and duplicate **Deploy to user** or **Add to location stock** requests;
- add multiple **Bulk add to location stock** requests, each with its own serial list;
- use Quick import with `SERIAL` or `SERIAL USERNAME` lines, then choose
  **Deploy to user** or **Add to location stock** per device (or set one action
  for every eligible line at once), adding or removing devices before queueing;
- preselect the most recently used location in every location workflow,
  and automatically load the other locations for that city;
- run the Inventory Tracking import wizard: choose file, sheet, date, and new,
  return, or both; preview exclusions; mark individual rows as do not deploy;
  then change individual new devices between new and existing stock;
- search EUDM for devices, people, cities, and locations;
- record who returned a device on **Add to location stock** requests when needed;
- review the device, returning user, and destination details required by EUDM;
- edit every imported status or destination before submission;
- submit independent requests concurrently with live status and request IDs;
- review completed submission runs from the Request history menu after the queue is cleared;
- open current EUDM-backed details for each completed request, or open all request pages in new tabs;
- optionally populate valid drafts while editing so final submission can skip repeated lookups;
- download a final text summary containing successful and failed assignments.

The queue enforces the EUDM branches before any network work: user statuses
cannot carry locations, location statuses cannot carry deployed-to users,
individual requests require one exact serial, duplicate serials are rejected
across the queue, and EUDM bulk-location requests cannot carry a returning user.
After a real connection, status and city options refresh from the current EUDM
questionnaire so changed form options are not silently assumed.

After submissions, the progress view links each request ID to an AutoEUDM
details page and provides an **Open all request pages** button. That page reads
the authenticated EUDM event self-link captured from the recent-activity API,
then provides a fallback link to EUDM's native Activity gallery. The native
`/dwp/app/#activity/events/details` route cannot be deep-linked by adding the
request ID to its URL: EUDM passes the event self-link in router and browser
storage state.

Set `EUDM_SIMULATE=true` to run the complete web interface locally without
Chrome, SSO, EUDM, or real requests. Simulation runs still produce request and
order IDs with a `SIM-` prefix. The web page cannot turn simulation on or off;
it uses the shared environment setting loaded when the server starts.

For real runs, Connect to EUDM completes or reuses SSO and reads the signed-in
account ID from EUDM. That account is used as the requester automatically.
`EUDM_REQUEST_FOR` is retained as a fallback if EUDM does not return an account
ID, and the requester is shown read-only in the web workspace. Until the real
connection is ready, the workspace displays a blocking connection notice and
keeps Review & Submit disabled. Spreadsheet dates also show a local relative
label such as `[Today]`, `[Tomorrow]`, or `[Next Week]`.

The header’s **Open EUDM form** link opens the native End User Device
Management catalogue item as a fallback. **Prepare drafts early** is optional
and defaults from `EUDM_PREPARE_DRAFTS`. When enabled, each complete row is
populated after editing pauses. A matching draft is claimed at submission, so
only the final order call remains. Editing a prepared row creates a replacement
draft; EUDM may retain the older unsubmitted draft.

If EUDM returns its SSO page after a connection has been established, AutoEUDM
marks the session as expired, keeps the queue intact, and prompts for a
reconnect. When a visible Chrome SSO session succeeds, its temporary window is
closed automatically after AutoEUDM has confirmed the session.

## Shared configuration

Every script loads `.env` from the repository folder before reading its command-line options. This checkout has a generated, gitignored local `.env`; on another computer, copy the public template once and edit it:

```bash
cp .env.example .env
```

Use it to set the requesting login, Chrome profile, Sydney location, status defaults, and default CLI modes. Set `EUDM_ENV_FILE` if you prefer a shared file at another path.

Precedence is: explicit command-line option, existing shell environment, `.env`, then built-in default. Boolean settings also have `--no-*` overrides, such as `--no-simulate`, `--no-verbose`, `--no-logging`, and `--no-manual-review`.

Configured identity and location values are used silently: for example, when
`EUDM_REQUEST_FOR=rkontos` is set, no CLI asks for the request-for login ID.
It is prompted only when neither the command line nor the shared environment
provides it.

```dotenv
EUDM_REQUEST_FOR=rkontos
EUDM_BROWSER_HEADLESS=true
EUDM_CITY="Sydney, AU"
EUDM_BUILDING="1 Elizabeth Street"
EUDM_FLOOR="Level 15"
EUDM_ROOM="Store Room"
EUDM_DEFAULT_USER_STATUS="Deployed - Existing Stock"
EUDM_LOGGING=true
EUDM_CONCURRENCY=3
```

Do not put `EUDM_COOKIE` in `.env`; export short-lived cookies in the current shell or use the dedicated Chrome profile.

When SSO is already automatic in the dedicated profile, set
`EUDM_BROWSER_HEADLESS=true` (enabled in this local checkout) to run Chrome in
the background with no visible window. If SSO needs attention, use
`--no-headless` for one run and complete the visible login.

## User return / user deployment batch CLI

Run the new CLI and paste one `SERIAL USERNAME` pair per line. Finish with a blank line:

```bash
./launchers/run-eudm-user-returns.command
```

It then displays user-only deployment statuses applied to the entire batch. Option 1 is **Used stock**, submitted to EUDM as `Deployed - Existing Stock`. The remaining options are New stock, Loan, and Pending return. It previews every pair before authentication and uses the same strict retry-or-skip matching, manual review, simulation, and grouped result handling as the spreadsheet importer.

Pass a multiline string directly:

```bash
python3 eudm_user_returns.py $'ABC1234 user.one\nDEF5678 user.two' --simulate
```

Read a file or clipboard contents:

```bash
python3 eudm_user_returns.py --file assignments.txt --dry-run
```

```bash
pbpaste | python3 eudm_user_returns.py - --simulate
```

## Batch location deployment CLI

Use this for one location deployment containing many serials and no associated
user. It asks for one location status, then accepts a pasted comma/newline list
or one serial at a time. It creates one EUDM batch request, which is materially
faster than creating one request per serial.

```bash
./launchers/run-eudm-location-batch.command --simulate
```

## Activity logs and results

Set `EUDM_LOGGING=true` in `.env` (or pass `--logging`) to save safe
authentication and API timing/status events in `logs/`. Cookies, request
bodies, and response bodies are never written. Every completed deployment run
also writes a text summary of assignments, request IDs, successes, and failures
to `results/`.

For one-user-per-device work, set `EUDM_CONCURRENCY=2` or `3` to overlap EUDM
requests and reduce long waits. Each request ID is printed as soon as EUDM creates
it. Manual review deliberately runs sequentially so each approval remains clear.

## Run

The script requires an authenticated EUDM session. The configured Chrome profile is the default. To use a browser session's `Cookie` request header through `EUDM_COOKIE`, add `--cookie-mode`; it is not stored by the script.

To avoid copying cookies, install Playwright and use a dedicated Chrome profile. The browser mode launches the installed Google Chrome binary directly through Playwright (no AppleScript, `osascript`, or Apple Events), keeps API calls inside that authenticated browser context, and asks you to complete SSO interactively:

```bash
python3 -m pip install -r requirements/requirements-browser.txt
python3 eudm_request.py --browser-profile ~/.auto-eudm-chrome --serial K9JQ6MYW9R --request-for rkontos --target user --status 'Deployed - New Stock' --deployed-to rkontos
```

For a location deployment with browser-based SSO:

```bash
python3 eudm_request.py --browser-profile ~/.auto-eudm-chrome --serial K9JQ6MYW9R --request-for rkontos --target location --status 'Used Stock' --city 'Sydney, AU' --building '1 Elizabeth Street' --floor 'Level 15' --room 'Store Room' --dropped-by rkontos
```

`--city` filters the available locations. The script then requires an exact building, floor, and room match before sending the opaque location ID returned by EUDM. Add `--cabinet 'Cabinet Name'` if those three fields still match more than one row.

The profile path is separate from your normal Chrome profile so the automation does not interfere with it. Subsequent runs can reuse the profile while its SSO session remains valid; cookies are not extracted and replayed through a separate HTTP client.

## Interactive frontend

The interactive wrapper emulates the supported form flow with numbered live options for mode, device, status, city, location, and users. It always asks before the final order commit:

```bash
python3 eudm_wizard.py
```

It launches installed Google Chrome with the dedicated `~/.auto-eudm-chrome` profile by default. To use `EUDM_COOKIE` instead:

```bash
python3 eudm_wizard.py --cookie-mode
```

## Local simulation

Every CLI has `--simulate`. Simulation uses the same validation and interface but substitutes an in-memory EUDM service, so it never opens Chrome, authenticates, connects to the network, or changes real EUDM data.

Simulate a direct user deployment:

```bash
python3 eudm_request.py --simulate --serial ABC1234 --request-for tester --target user --status 'Deployed - New Stock' --deployed-to simulated.user --submit
```

Simulate a direct location deployment:

```bash
python3 eudm_request.py --simulate --serial ABC1234 --request-for tester --target location --status 'Used Stock' --city 'Sydney, AU' --building '1 Elizabeth Street' --floor 'Level 15' --room 'Store Room' --dropped-by simulated.user --submit
```

Simulate the interactive questionnaire:

```bash
python3 eudm_wizard.py --simulate
```

Simulate spreadsheet submissions, including generated `SIM-REQ-...` request IDs:

```bash
python3 eudm_inventory_import.py --simulate
```

`--dry-run` on the spreadsheet CLI stops after the preview. `--simulate` continues through the submission interface locally, making it useful for testing request progress and result grouping.

## Manual review before ordering

Add `--manual-review` (also `--review` or `--manual`) when you want to inspect the populated request and approve its final order with `y` or `n`. The direct script requires `--submit` as well; the interactive wizard and spreadsheet importer already have an overall confirmation, then manual review adds a concise per-request approval.

```bash
python3 eudm_request.py --simulate --serial ABC1234 --request-for tester --target user --status 'Deployed - New Stock' --deployed-to simulated.user --submit --manual-review
```

```bash
python3 eudm_wizard.py --simulate --manual-review
```

```bash
python3 eudm_inventory_import.py --simulate --manual-review
```

The review shows the request ID, requester, serial or serial list, status, and user or location destination. Declining leaves the populated request un-ordered. Spreadsheet results label it `NOT SUBMITTED` and include the request ID.

Serial and user matching is strict. A zero-match or ambiguous exact match always pauses the request, even without `--manual-review`: enter a corrected serial/username to try again, or type `skip`. The prompt repeats until a unique match is found or the request is skipped. In spreadsheet runs, skipped actions remain in the final grouped results with the request ID when EUDM had already created one.

Simulation can demonstrate these outcomes safely: use serial `NO-MATCH` for no returned device, serial `AMBIGUOUS` for two returned devices, user `no.user` for no person, or user `ambiguous.user` for two matching people.

If Python/OpenSSL rejects the work computer's certificate chain, the client automatically retries that request with the system `curl` trust store. This keeps certificate verification enabled and lets macOS Keychain trust be used. Run with `--verbose` to see when the fallback occurs.

Normal runs show prompts, final request IDs, results, and short action-oriented errors. Response bodies, HTML SSO pages, cookies, and raw JSON are never printed. Add `--verbose` only when you need field-by-field questionnaire progress, matching details, or transport-level diagnostics.

## Sydney inventory workbook

Install spreadsheet support:

```bash
python3 -m pip install -r requirements/requirements-sheet.txt
```

For the normal Chrome-based flow on a fresh checkout, install both requirements in one command:

```bash
python3 -m pip install -r requirements/requirements-browser.txt -r requirements/requirements-sheet.txt
```

Run the guided importer. With no file argument it offers the newest `Inventory Tracking - Sydney*.xlsx` or `.xlsm` file in Downloads. It automatically uses a sheet named `Bookings 2026`; if that sheet is absent, it displays the available sheet names and asks you to choose one:

```bash
python3 eudm_inventory_import.py
```

Run it with a specific workbook:

```bash
python3 eudm_inventory_import.py '/path/to/Inventory Tracking - Sydney.xlsx'
```

Preview only, with no Chrome window and no EUDM API requests:

```bash
python3 eudm_inventory_import.py --dry-run
```

Use a specific workbook in true dry-run mode:

```bash
python3 eudm_inventory_import.py '/path/to/Inventory Tracking - Sydney.xlsx' --dry-run
```

Use `EUDM_COOKIE` instead of the dedicated installed-Chrome profile:

```bash
python3 eudm_inventory_import.py --cookie-mode
```

The importer lists the dates from column A, then asks whether to process new devices from column J, old devices from column L, or both. Column D supplies the target username; column F is never used.

A row is excluded before authentication when any cell in columns A-L uses red font, or when no username is present in column D. New and old serials are evaluated independently, so a missing serial in one column does not discard a valid serial in the other. An old serial from column L becomes a separate `Deployed - Pending Return` request for the same user. Obvious non-serial sheet markers, such as the single digits `1`-`5`, are not treated as serial numbers.

New devices default to `Deployed - New Stock`. Before the preview, enter one or more displayed numbers such as `2,4-6` to change those devices to `Deployed - Existing Stock`. The final preview includes every serial, username, source row, and status. The CLI asks once more before authentication and submission.

After processing, request IDs are grouped under `New deployments` and `Pending returns`. One failed device does not hide the results for the others; failed items include a useful message and, when EUDM had already created it, the request ID.

User deployment:

```bash
export EUDM_COOKIE='...'
python3 eudm_request.py --cookie-mode --serial K9JQ6MYW9R --request-for rkontos --target user --status 'Deployed - New Stock' --deployed-to rkontos
```

Location deployment:

```bash
python3 eudm_request.py --serial K9JQ6MYW9R --request-for rkontos --target location --status 'Used Stock' --city 'Sydney, AU' --building '1 Elizabeth Street' --floor 'Level 15' --room 'Store Room' --dropped-by rkontos
```

Batch serial list to one location, with no deployed-to or drop-off user:

```bash
python3 eudm_request.py --batch --serials 'K9JQ6MYW9R,ANOTHER123,THIRD456' --request-for rkontos --target location --status 'Used Stock' --city 'Sydney, AU' --building '1 Elizabeth Street' --floor 'Level 15' --room 'Store Room' --browser-profile ~/.auto-eudm-chrome
```

Batch mode uses EUDM's `BULK by Serial Number` path, verifies that every requested serial uniquely matches a returned asset, selects all matched assets, sets the return-from-user answer to `NO`, and never accepts user arguments.

The examples above are dry runs with respect to the final order commit. To commit a request after checking the output, add `--submit`:

```bash
python3 eudm_request.py --serial K9JQ6MYW9R --request-for rkontos --target user --status 'Deployed - New Stock' --deployed-to rkontos --submit
```

For a location deployment:

```bash
python3 eudm_request.py --serial K9JQ6MYW9R --request-for rkontos --target location --status 'Used Stock' --city 'Sydney, AU' --building '1 Elizabeth Street' --floor 'Level 15' --room 'Store Room' --dropped-by rkontos --submit
```

By default the script stops before the final order commit. Note that creating the request and recording answers are still server-side actions. `--submit` performs the final order commit and should only be used after validating the dynamic path.

All argument combinations are validated before Chrome starts or any API request is made. `--target user` rejects location-only arguments. Normal `--target location` mode requires the complete city/building/floor/room/drop-off-user set, while `--batch --target location` requires the location fields and rejects all user arguments. The explicit target avoids guessing from EUDM status labels, whose displayed and submitted values can differ.
