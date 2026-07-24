# DWP device request automation

First-pass automation for the Macquarie Digital Workplace device-management request.

## Contents

- `src/dwp_device_request/` — the packaged Python implementation.
- `launchers/` — double-clickable macOS `.command` launchers.
- `docs/USAGE.md` — operating guide with safety boundaries, workbook mapping, examples, and troubleshooting.
- `requirements/` — optional dependency sets for browser and spreadsheet support.
- `samples/Inventory Tracking - Sydney - Test Data.xlsx` — sample workbook in the same layout as the real file.

The captures contain internal and personal data. This repository is public by request; do not add authentication cookies, tokens, or additional sensitive captures.

See [docs/USAGE.md](docs/USAGE.md) for the complete operating guide. Every command also documents its arguments and safety behaviour through `--help`.

## Automatic setup

Every launcher and Python entry point creates a local `.venv` on its first
real run, installs only the dependency set it needs, and restarts inside that
environment. Spreadsheet commands install `openpyxl`; live browser commands
install Playwright. Simulation mode avoids the browser install.

Set `DWP_SKIP_AUTO_INSTALL=1` to manage dependencies manually, or
`DWP_VENV_DIR=/path/to/venv` to choose another environment location.

On macOS, the four `.command` files in `launchers/` are double-clickable launchers:

```bash
./launchers/run-device-request.command --help
./launchers/run-interactive-device-request.command --simulate
./launchers/run-inventory-sheet.command --dry-run
./launchers/run-serial-user-batch.command --simulate
```

They change into the repository folder before running, start with the available
`python3` (which performs the automatic setup above), and pass arguments
through to the underlying CLI. If macOS blocks a newly downloaded launcher,
right-click it in Finder and choose Open once.

## Shared configuration

Every script loads `.env` from the repository folder before reading its command-line options. This checkout has a generated, gitignored local `.env`; on another computer, copy the public template once and edit it:

```bash
cp .env.example .env
```

Use it to set the requesting login, Chrome profile, Sydney location, status defaults, and default CLI modes. Set `DWP_ENV_FILE` if you prefer a shared file at another path.

Precedence is: explicit command-line option, existing shell environment, `.env`, then built-in default. Boolean settings also have `--no-*` overrides, such as `--no-simulate`, `--no-verbose`, and `--no-manual-review`.

```dotenv
DWP_REQUEST_FOR=rkontos
DWP_CITY="Sydney, AU"
DWP_BUILDING="1 Elizabeth Street"
DWP_FLOOR="Level 15"
DWP_ROOM="Store Room"
DWP_DEFAULT_USER_STATUS="Deployed - Existing Stock"
```

Do not put `DWP_COOKIE` in `.env`; export short-lived cookies in the current shell or use the dedicated Chrome profile.

## Serial and username batch CLI

Run the new CLI and paste one `SERIAL USERNAME` pair per line. Finish with a blank line:

```bash
./launchers/run-serial-user-batch.command
```

It then displays user-only deployment statuses applied to the entire batch. Option 1 is **Used stock**, submitted to DWP as `Deployed - Existing Stock`. The remaining options are New stock, Loan, and Pending return. It previews every pair before authentication and uses the same strict retry-or-skip matching, manual review, simulation, and grouped result handling as the spreadsheet importer.

Pass a multiline string directly:

```bash
python3 serial_user_cli.py $'ABC1234 user.one\nDEF5678 user.two' --simulate
```

Read a file or clipboard contents:

```bash
python3 serial_user_cli.py --file assignments.txt --dry-run
```

```bash
pbpaste | python3 serial_user_cli.py - --simulate
```

## Run

The script requires an authenticated DWP session. The configured Chrome profile is the default. To use a browser session's `Cookie` request header through `DWP_COOKIE`, add `--cookie-mode`; it is not stored by the script.

To avoid copying cookies, install Playwright and use a dedicated Chrome profile. The browser mode launches the installed Google Chrome binary directly through Playwright (no AppleScript, `osascript`, or Apple Events), keeps API calls inside that authenticated browser context, and asks you to complete SSO interactively:

```bash
python3 -m pip install -r requirements/requirements-browser.txt
python3 automate_device_request.py --browser-profile ~/.dwp-device-request-chrome --serial K9JQ6MYW9R --request-for rkontos --target user --status 'Deployed - New Stock' --deployed-to rkontos
```

For a location deployment with browser-based SSO:

```bash
python3 automate_device_request.py --browser-profile ~/.dwp-device-request-chrome --serial K9JQ6MYW9R --request-for rkontos --target location --status 'Used Stock' --city 'Sydney, AU' --building '1 Elizabeth Street' --floor 'Level 15' --room 'Store Room' --dropped-by rkontos
```

`--city` filters the available locations. The script then requires an exact building, floor, and room match before sending the opaque location ID returned by DWP. Add `--cabinet 'Cabinet Name'` if those three fields still match more than one row.

The profile path is separate from your normal Chrome profile so the automation does not interfere with it. Subsequent runs can reuse the profile while its SSO session remains valid; cookies are not extracted and replayed through a separate HTTP client.

## Interactive frontend

The interactive wrapper emulates the supported form flow with numbered live options for mode, device, status, city, exact location, and users. It always asks before the final order commit:

```bash
python3 interactive_device_request.py
```

It launches installed Google Chrome with the dedicated `~/.dwp-device-request-chrome` profile by default. To use `DWP_COOKIE` instead:

```bash
python3 interactive_device_request.py --cookie-mode
```

## Local simulation

Every CLI has `--simulate`. Simulation uses the same validation and interface but substitutes an in-memory DWP service, so it never opens Chrome, authenticates, connects to the network, or changes real DWP data.

Simulate a direct user deployment:

```bash
python3 automate_device_request.py --simulate --serial ABC1234 --request-for tester --target user --status 'Deployed - New Stock' --deployed-to simulated.user --submit
```

Simulate a direct location deployment:

```bash
python3 automate_device_request.py --simulate --serial ABC1234 --request-for tester --target location --status 'Used Stock' --city 'Sydney, AU' --building '1 Elizabeth Street' --floor 'Level 15' --room 'Store Room' --dropped-by simulated.user --submit
```

Simulate the interactive questionnaire:

```bash
python3 interactive_device_request.py --simulate
```

Simulate spreadsheet submissions, including generated `SIM-REQ-...` request IDs:

```bash
python3 inventory_sheet_cli.py --simulate
```

`--dry-run` on the spreadsheet CLI stops after the preview. `--simulate` continues through the submission interface locally, making it useful for testing request progress and result grouping.

## Manual review before ordering

Add `--manual-review` (also `--review` or `--manual`) when you want to inspect the populated request and approve its final order with `y` or `n`. The direct script requires `--submit` as well; the interactive wizard and spreadsheet importer already have an overall confirmation, then manual review adds a concise per-request approval.

```bash
python3 automate_device_request.py --simulate --serial ABC1234 --request-for tester --target user --status 'Deployed - New Stock' --deployed-to simulated.user --submit --manual-review
```

```bash
python3 interactive_device_request.py --simulate --manual-review
```

```bash
python3 inventory_sheet_cli.py --simulate --manual-review
```

The review shows the request ID, requester, serial or serial list, status, and user or location destination. Declining leaves the populated request un-ordered. Spreadsheet results label it `NOT SUBMITTED` and include the request ID.

Serial and user matching is strict. A zero-match or ambiguous exact match always pauses the request, even without `--manual-review`: enter a corrected serial/username to try again, or type `skip`. The prompt repeats until a unique match is found or the request is skipped. In spreadsheet runs, skipped actions remain in the final grouped results with the request ID when DWP had already created one.

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
python3 inventory_sheet_cli.py
```

Run it with a specific workbook:

```bash
python3 inventory_sheet_cli.py '/path/to/Inventory Tracking - Sydney.xlsx'
```

Preview only, with no Chrome window and no DWP API requests:

```bash
python3 inventory_sheet_cli.py --dry-run
```

Use a specific workbook in true dry-run mode:

```bash
python3 inventory_sheet_cli.py '/path/to/Inventory Tracking - Sydney.xlsx' --dry-run
```

Use `DWP_COOKIE` instead of the dedicated installed-Chrome profile:

```bash
python3 inventory_sheet_cli.py --cookie-mode
```

The importer lists the dates from column A, then asks whether to process new devices from column J, old devices from column L, or both. Column D supplies the target username; if a formula result there is unavailable, the importer can use the login portion of the column F email address and calls this out in the preview.

A row is excluded before authentication when any cell in columns A-L is marked with red font or red fill, when column J has no usable serial, or when no username can be resolved. An old serial from column L becomes a separate `Pending Return` request for the same user. Obvious non-serial sheet markers, such as the single digits `1`-`5`, are not treated as serial numbers.

New devices default to `Deployed - New Stock`. Before the preview, enter one or more displayed numbers such as `2,4-6` to change those devices to `Deployed - Existing Stock`. The final preview includes every serial, username, source row, and status. The CLI asks once more before authentication and submission.

After processing, request IDs are grouped under `New deployments` and `Old / pending return`. One failed device does not hide the results for the others; failed items include a useful message and, when DWP had already created it, the request ID.

User deployment:

```bash
export DWP_COOKIE='...'
python3 automate_device_request.py --cookie-mode --serial K9JQ6MYW9R --request-for rkontos --target user --status 'Deployed - New Stock' --deployed-to rkontos
```

Location deployment:

```bash
python3 automate_device_request.py --serial K9JQ6MYW9R --request-for rkontos --target location --status 'Used Stock' --city 'Sydney, AU' --building '1 Elizabeth Street' --floor 'Level 15' --room 'Store Room' --dropped-by rkontos
```

Batch serial list to one location, with no deployed-to or drop-off user:

```bash
python3 automate_device_request.py --batch --serials 'K9JQ6MYW9R,ANOTHER123,THIRD456' --request-for rkontos --target location --status 'Used Stock' --city 'Sydney, AU' --building '1 Elizabeth Street' --floor 'Level 15' --room 'Store Room' --browser-profile ~/.dwp-device-request-chrome
```

Batch mode uses DWP's `BULK by Serial Number` path, verifies that every requested serial uniquely matches a returned asset, selects all matched assets, sets the return-from-user answer to `NO`, and never accepts user arguments.

The examples above are dry runs with respect to the final order commit. To commit a request after checking the output, add `--submit`:

```bash
python3 automate_device_request.py --serial K9JQ6MYW9R --request-for rkontos --target user --status 'Deployed - New Stock' --deployed-to rkontos --submit
```

For a location deployment:

```bash
python3 automate_device_request.py --serial K9JQ6MYW9R --request-for rkontos --target location --status 'Used Stock' --city 'Sydney, AU' --building '1 Elizabeth Street' --floor 'Level 15' --room 'Store Room' --dropped-by rkontos --submit
```

By default the script stops before the final order commit. Note that creating the request and recording answers are still server-side actions. `--submit` performs the final order commit and should only be used after validating the dynamic path.

All argument combinations are validated before Chrome starts or any API request is made. `--target user` rejects location-only arguments. Normal `--target location` mode requires the complete city/building/floor/room/drop-off-user set, while `--batch --target location` requires the location fields and rejects all user arguments. The explicit target avoids guessing from DWP status labels, whose displayed and submitted values can differ.
