# DWP device request automation

First-pass automation for the Macquarie Digital Workplace device-management request.

## Contents

- `automate_device_request.py` — dynamic REST client for the captured questionnaire flow.
- `interactive_device_request.py` — numbered CLI frontend for normal and batch flows.
- `inventory_sheet_cli.py` — guided importer for `Inventory Tracking - Sydney` workbooks.
- `USAGE.md` — operating guide with safety boundaries, workbook mapping, examples, and troubleshooting.
- `big.har` — source network capture used to map the API workflow.
- `requestform.txt` — source Angular DOM capture used to map labels and question IDs.

The captures contain internal and personal data. This repository is public by request; do not add authentication cookies, tokens, or additional sensitive captures.

See [USAGE.md](USAGE.md) for the complete operating guide. Every command also documents its arguments and safety behaviour through `--help`.

On macOS, the three `.command` files in the repository are double-clickable launchers:

```bash
./run-device-request.command --help
./run-interactive-device-request.command --simulate
./run-inventory-sheet.command --dry-run
```

They change into the repository folder before running, use the system `python3`, and pass arguments through to the underlying CLI. If macOS blocks a newly downloaded launcher, right-click it in Finder and choose Open once.

## Run

The script requires an authenticated DWP session. Supply the browser session's `Cookie` request header through `DWP_COOKIE`; it is not stored by the script.

To avoid copying cookies, install Playwright and use a dedicated Chrome profile. The browser mode launches the installed Google Chrome binary directly through Playwright (no AppleScript, `osascript`, or Apple Events), keeps API calls inside that authenticated browser context, and asks you to complete SSO interactively:

```bash
python3 -m pip install -r requirements-browser.txt
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

Serial and user matching is strict. A zero-match or ambiguous exact match is reported in a friendly message. In spreadsheet runs, each failed action remains in the final grouped results and includes the request ID when DWP had already created one.

Simulation can demonstrate these outcomes safely: use serial `NO-MATCH` for no returned device, serial `AMBIGUOUS` for two returned devices, user `no.user` for no person, or user `ambiguous.user` for two matching people.

If Python/OpenSSL rejects the work computer's certificate chain, the client automatically retries that request with the system `curl` trust store. This keeps certificate verification enabled and lets macOS Keychain trust be used. Run with `--verbose` to see when the fallback occurs.

Normal runs show prompts, final request IDs, results, and short action-oriented errors. Response bodies, HTML SSO pages, cookies, and raw JSON are never printed. Add `--verbose` only when you need field-by-field questionnaire progress, matching details, or transport-level diagnostics.

## Sydney inventory workbook

Install spreadsheet support:

```bash
python3 -m pip install -r requirements-sheet.txt
```

For the normal Chrome-based flow on a fresh checkout, install both requirements in one command:

```bash
python3 -m pip install -r requirements-browser.txt -r requirements-sheet.txt
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
python3 automate_device_request.py --serial K9JQ6MYW9R --request-for rkontos --target user --status 'Deployed - New Stock' --deployed-to rkontos
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
