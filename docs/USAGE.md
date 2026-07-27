# Operating guide

This guide covers the localhost web interface, command-line interfaces, their
safety boundaries, and the Sydney workbook import rules. Run any command with `--help` to see the
exact argument reference for the version you have checked out.

The packaged Python code lives under `src/auto_eudm/`, the macOS
launchers live in `launchers/`, and the sample workbook lives in `samples/`.

## Quick start on a new computer

The simplest setup is to clone the public repository and run the platform
launcher. It creates `.env` from the safe simulation template, creates `.venv`,
installs dependencies, and opens the website.

```bash
git clone https://github.com/ryankontos/auto-eudm.git
cd auto-eudm
./launchers/run-auto-eudm-web.command
```

On Windows PowerShell:

```powershell
git clone https://github.com/ryankontos/auto-eudm.git
cd auto-eudm
.\launchers\run-auto-eudm-web.ps1
```

On Windows Command Prompt, run `launchers\run-auto-eudm-web.bat` instead. The
same `.bat` file can be double-clicked. Python 3.10 or newer is required; live
EUDM mode also uses an installed Google Chrome for SSO. Simulation mode is the
safe default and does not need Chrome or EUDM access.

The shared implementation is `start_auto_eudm.py`, so `python
start_auto_eudm.py` is another cross-platform option.

## Automatic setup for other commands

Every launcher and Python entry point creates `.venv` on its first real run,
installs the relevant requirements, and restarts with that interpreter.
Spreadsheet commands install `openpyxl`; live browser commands install
Playwright. Simulation mode avoids the browser install.

Set `EUDM_SKIP_AUTO_INSTALL=1` to disable this behaviour, or set
`EUDM_VENV_DIR=/path/to/venv` to choose a different environment location.

## Choose a command

| Command | Best for | Result |
| --- | --- | --- |
| `eudm_web.py` | Preparing, editing, searching, importing, and submitting many mixed requests | A localhost request workspace with live progress and downloadable results. |
| `eudm_request.py` | Repeatable, fully specified single or batch requests | Populates one EUDM request; batch mode applies several serials to one location. |
| `eudm_wizard.py` | Choosing from live EUDM devices, statuses, locations, and people | A numbered wizard for one device or batch-to-location flow. |
| `eudm_inventory_import.py` | Deploying devices from an Inventory Tracking - Sydney workbook | One user request per selected new or old serial, with grouped results. |
| `eudm_user_returns.py` | Pasting or piping `SERIAL USERNAME` pairs | One user request per line, with one status applied to the entire batch. |
| `eudm_location_batch.py` | Many serials to one location, no associated user | One EUDM batch request after guided serial entry. |

## macOS launchers

The repository includes double-clickable launchers:

| File | Runs |
| --- | --- |
| `launchers/run-auto-eudm-web.command` | `eudm_web.py` and opens the localhost workspace |
| `launchers/run-eudm-request.command` | `eudm_request.py` |
| `launchers/run-eudm-wizard.command` | `eudm_wizard.py` |
| `launchers/run-eudm-inventory-import.command` | `eudm_inventory_import.py` |
| `launchers/run-eudm-user-returns.command` | `eudm_user_returns.py` |
| `launchers/run-eudm-location-batch.command` | `eudm_location_batch.py` |

They locate the repository from the launcher’s own path, so they work from
Finder or Terminal. Arguments are passed through unchanged:

```bash
./launchers/run-eudm-wizard.command --simulate --manual
```

```bash
./launchers/run-eudm-inventory-import.command --dry-run
```

Opening `launchers/run-eudm-request.command` without arguments displays the direct
script help and a copyable example because that script requires request values.
The other launchers start their interactive flows when opened without
arguments. The serial/user launcher accepts pasted lines until a blank line.

## Local web workspace

Run:

```bash
./launchers/run-auto-eudm-web.command
```

The launcher starts a server bound to `127.0.0.1` and opens
`http://127.0.0.1:8765`. It installs spreadsheet support automatically and,
only for a real run, installs the browser support needed for EUDM SSO. Keep its
Terminal window open. Stop it with Control-C.

When a visible Chrome window is needed for SSO, AutoEUDM closes that temporary
window after it verifies the signed-in EUDM session. If EUDM later redirects an
API request to SSO, the workspace marks the session as expired, preserves the
queue, and presents a reconnect action.

You can run the same launcher again at any time. If AutoEUDM is already running
on that port, it opens the existing workspace in the browser and does not start
a duplicate server. Pass `--no-open` when a browser tab should not be opened.

The website follows the computer's light or dark appearance by default. The
appearance button in the header switches to the opposite theme; press it again
to return to the live system setting. A manual override is remembered by that
browser.

The web workspace supports a mixed queue of:

- one **Deploy to user** request;
- one **Add to location stock** request, with a returner when supplied;
- one **Bulk add to location stock** request containing many serials for one exact
  location and no user;
- multiple bulk requests with different statuses or locations.

The left rail adds individual requests, Quick import lines, or an Inventory
Tracking workbook. Quick import accepts `SERIAL` or `SERIAL USERNAME` on every
line, then presents each device for a choice of **Deploy to user** or **Add to
location stock**. A supplied username becomes the receiving user for a user
deployment, or the returning user for location stock. Lines without a username
can only be added to location stock. Use the bulk action control to apply the
same choice to every eligible line. Devices can also be added or removed from
the review step before they reach the queue. The dialog starts with the most recently
used location when available (otherwise the configured default), and
automatically loads other locations for that city. The centre queue is
the complete execution plan.

This automatic location behaviour is shared by Quick import, individual
**Add to location stock** requests and bulk location requests: selecting or opening a location-based request immediately loads
the locations for its selected city. The **Refresh** button is only for
updating the list if EUDM's locations have changed.
The right inspector edits the selected request and provides live EUDM device,
user, city, and location searches. The spreadsheet mode reproduces the
CLI wizard as three steps: choose the workbook; choose its sheet, deployment
date, and new devices, returns, or both; then preview every generated request
and ignored-serial reason. New deployments and pending returns are presented in
separate preview sections. Every preview row can be unchecked, and each section
has quick All and None controls. New devices default to Deployed - New Stock and
can be changed individually to Deployed - Existing Stock. Old devices in column
L become Deployed - Pending Return requests for the column D user. `Bookings
2026` is preselected when present; otherwise every dated sheet is available.

When a location request is marked as a return, the inspector shows the device,
returning user, and destination. The separate confirmation checkbox is not
needed because the details remain visible in the editor and the request appears
again in final review. Review & Submit remains disabled until every queue entry
is valid and, during a real run, until EUDM is connected. A prominent connection
notice explains what is needed while the queue remains editable. Spreadsheet
date choices include local relative labels such as `[Today]`, `[Tomorrow]`, and
`[Next Week]`.

Review & Submit displays the whole queue before creating requests. Submission
progress reports each row as queued, running, submitted, or failed and shows
the EUDM request ID as soon as it is created. Closing the completed run clears
the prepared queue; the **Request history** menu keeps the run, request IDs,
and statuses available for the rest of the server session. The final view
keeps every request ID visible and downloads a plain-text summary. The same
summary is written to the gitignored `results/` directory.

The web workspace always requires this queue-level review, so it is safe and
clear whether or not `EUDM_MANUAL_REVIEW` is enabled. It does not use terminal
`y/n` prompts inside web jobs.

Useful commands:

```bash
python3 eudm_web.py --help
```

```bash
EUDM_SIMULATE=true python3 eudm_web.py
```

```bash
python3 eudm_web.py --port 8787
```

The web server accepts only localhost browser requests, does not expose
authentication cookies to JavaScript, and has no external web-framework or
Node dependency. Simulation is controlled only by `EUDM_SIMULATE` when the
server starts. For a real connection, the requester is detected from the
authenticated EUDM cart response and shown read-only; `EUDM_REQUEST_FOR` is the
fallback when EUDM does not return the signed-in user ID.

## Shared `.env` configuration

All frontends load the gitignored `.env` beside the scripts. The repository also
contains `.env.example`, which documents every supported setting. On a new clone,
run `cp .env.example .env`, then edit the local file to match the work computer.
This current checkout already has that generated local file. Set `EUDM_ENV_FILE`
to use a configuration file elsewhere.

| Variable | Used for |
| --- | --- |
| `EUDM_REQUEST_FOR` | Default requesting login ID. |
| `EUDM_BROWSER_PROFILE` | Dedicated installed-Chrome profile. |
| `EUDM_BROWSER_HEADLESS` | Run that profile with no visible Chrome window; use `--no-headless` when SSO needs attention. |
| `EUDM_BASE` | EUDM REST base URL. |
| `EUDM_CITY`, `EUDM_BUILDING`, `EUDM_FLOOR`, `EUDM_ROOM`, `EUDM_CABINET` | Direct-script location defaults. |
| `EUDM_DEFAULT_USER_STATUS` | Default direct/new pasted-pair user status. |
| `EUDM_DEFAULT_LOCATION_STATUS` | Default direct location status. |
| `EUDM_SIMULATE`, `EUDM_VERBOSE`, `EUDM_LOGGING`, `EUDM_MANUAL_REVIEW` | Shared default CLI modes. |
| `EUDM_CONCURRENCY` | Parallel requests, from 1 to 50. |

Command-line options take precedence over shell variables, which take precedence
over `.env`. Use `--no-simulate`, `--no-verbose`, or `--no-manual-review` when a
boolean is enabled in `.env` but should be disabled for one run. `EUDM_COOKIE` is
deliberately excluded from the file; keep it short-lived in the shell.

Values that are present in the shared environment are not re-asked by an
interactive CLI. In particular, `EUDM_REQUEST_FOR` is used silently; the
request-for prompt appears only when it is not configured or supplied with
`--request-for`.

## Safety and modes

| Mode | Browser/network | Real EUDM request | Final EUDM order |
| --- | --- | --- | --- |
| Direct script without `--submit` | Yes | Created and populated | Not sent |
| Direct script with `--submit` | Yes | Created and populated | Sent |
| Spreadsheet `--dry-run` | No | None | None |
| Any CLI with `--simulate` | No | None | None; local IDs only |

The direct script without `--submit` is not a zero-change preview: it still
creates and fills a real request. Use spreadsheet `--dry-run` for a no-API
preview, or `--simulate` on any CLI for a complete local rehearsal.

Simulation uses sample devices, people, and locations. It produces `SIM-REQ-*`
and `SIM-ORDER-*` identifiers and never starts Chrome, uses cookies, connects to
EUDM, or changes real data. It tests the interface and validation, not whether
an actual serial or user exists.

Normal output intentionally stays compact but shows a live per-request progress
line for multi-request deployments. Add `--verbose` for field-by-field
questionnaire progress, matches, and safe transport diagnostics. Set
`EUDM_LOGGING=true` or use `--logging` to save safe API/authentication event
timings and statuses in `logs/`; cookies, request bodies, and response bodies
are never logged. Every completed deployment run writes a text summary with
serials, users/locations, statuses, request IDs, and failures to `results/`.
For user deployment batches, `EUDM_CONCURRENCY=2` or `3` can substantially reduce
waits. The tool prints each request ID immediately after EUDM creates it. Manual
review remains sequential so individual approvals cannot overlap.

## Manual review before ordering

All frontends support `--manual-review` (also available as `--review` or
`--manual`). It
displays the values already populated in the request — request ID, requester,
serial or serial list, status, and destination — then asks for an explicit
`y`/`n` before the final EUDM order. It does not alter any answers.

The direct script requires `--submit --manual-review`; otherwise it would have
no final order to approve. The interactive wizard and spreadsheet importer still
keep their existing overall confirmation. Spreadsheet manual review additionally
asks for an approval after every request has been populated, so each device can
be accepted or declined independently.

When a review is declined, the populated request remains un-ordered. Spreadsheet
results report this as `NOT SUBMITTED` with the request ID, separately from
successful submissions and failures.

The simulator includes safe matching-error fixtures: serial `NO-MATCH` returns
no device, serial `AMBIGUOUS` returns two devices, `no.user` returns no person,
and `ambiguous.user` returns two people. They are useful for checking error
presentation without EUDM access.

## Real authentication

The recommended real-EUDM path is a dedicated Chrome profile:

```bash
python3 eudm_wizard.py --browser-profile ~/.auto-eudm-chrome
```

Complete SSO in the opened browser and then return to the terminal. This profile
is separate from normal Chrome. The automation launches Chrome through
Playwright; it does not use Apple Events, AppleScript, or `osascript`.

Alternatively set `EUDM_COOKIE` to the full browser `Cookie` request header and
use `--cookie-mode` in any CLI. Cookies are never saved. Do not put
them in source code, shared shell history, or the public repository.

The real client uses the operating system `curl` trust store directly while
retaining certificate verification. Add
`--verbose` to see transport diagnostics, questionnaire field updates, matching
details, and per-request spreadsheet progress; it never prints cookies or bodies.

## Direct script

```bash
python3 eudm_request.py --help
```

User deployment:

```bash
python3 eudm_request.py --serial K9JQ6MYW9R --request-for rkontos --target user --status 'Deployed - New Stock' --deployed-to rkontos --submit
```

Location deployment. The location fields must exactly match one row returned
after filtering by city:

```bash
python3 eudm_request.py --serial K9JQ6MYW9R --request-for rkontos --target location --status 'Used Stock' --city 'Sydney, AU' --building '1 Elizabeth Street' --floor 'Level 15' --room 'Store Room' --dropped-by rkontos --submit
```

Batch location deployment uses EUDM’s `BULK by Serial Number` flow, checks that
every serial matches once, selects all matches, sets return-from-user to `NO`,
and rejects all user arguments:

```bash
python3 eudm_request.py --batch --serials 'K9JQ6MYW9R,ANOTHER123,THIRD456' --request-for rkontos --target location --status 'Used Stock' --city 'Sydney, AU' --building '1 Elizabeth Street' --floor 'Level 15' --room 'Store Room' --submit
```

All combinations are validated before authentication or an API call. In
particular, user targets reject location fields; normal locations require city,
building, floor, room, and a drop-off user; batch locations do not accept one.

Local direct-script rehearsal:

```bash
python3 eudm_request.py --simulate --serial ABC1234 --request-for tester --target user --status 'Deployed - New Stock' --deployed-to simulated.user --submit
```

Add `--manual-review` to the command above to rehearse the final review screen.

## Interactive wizard

```bash
python3 eudm_wizard.py --help
```

```bash
python3 eudm_wizard.py
```

The wizard begins with one-device versus batch-to-location mode, then lists
available choices with numbers. It asks before final submission. Cancelling at
that point leaves a real request populated but not ordered.

Use this to rehearse every prompt locally:

```bash
python3 eudm_wizard.py --simulate
```

Add `--manual-review` to see the populated request summary immediately before
the final approval.

## Spreadsheet importer

Install the workbook reader once:

```bash
python3 -m pip install -r requirements/requirements-sheet.txt
```

```bash
python3 eudm_inventory_import.py --help
```

With no file argument, the importer offers the newest Downloads file whose name
starts with `Inventory Tracking - Sydney` and ends in `.xlsx` or `.xlsm`.
Press Enter to accept it or type a different path. Supplying a file skips that
choice:

```bash
python3 eudm_inventory_import.py '/path/to/Inventory Tracking - Sydney.xlsx'
```

It selects `Bookings 2026` automatically. If that tab is absent, it displays
all sheet names and asks you to select one, rather than silently using an active
sheet.

The repo also includes `samples/Inventory Tracking - Sydney - Test Data.xlsx`,
which matches the same column layout for local rehearsals.

### Workbook mapping

| Column | Meaning | Behaviour |
| --- | --- | --- |
| A | Deployment date | The CLI lists dates and asks which date to process. |
| D | Username | Used as the deployed-to login ID. |
| G | Eligibility flag | A row is ignored only when this value is explicitly `false`. |
| F | Email | Ignored. The importer never derives usernames from email. |
| J | New serial | Required. It becomes a new user deployment. |
| L | Old serial | If valid, it becomes a separate `Deployed - Pending Return` request for that user. |

Rows are excluded before authentication when column G is explicitly `false`,
any cell in A:L has red font, or no username is present in D. New and old serials are checked
independently, so a missing serial in one column does not discard a valid serial
in the other. Short markers such as `1` to `5` are not serials. The preview
reports ignored serial numbers.

After choosing a date, choose new serials, returns, or both. New serials default
to `Deployed - New Stock`. Enter numbers/ranges such as `2,4-6` at the override
prompt to switch selected new serials to `Deployed - Existing Stock`.

The preview shows serial, user, status, and ignored serial-number counts.
It asks once more before real authentication. Results are grouped as `New
deployments` and `Pending returns`; one failure does not hide the others.

True no-change preview:

```bash
python3 eudm_inventory_import.py --dry-run
```

Full local rehearsal after the preview:

```bash
python3 eudm_inventory_import.py --simulate
```

For independent per-device approval after the batch preview:

```bash
python3 eudm_inventory_import.py --simulate --manual-review
```

## Serial/user text batch

Each nonblank input line must contain exactly two whitespace-separated values:

```text
ABC1234 user.one
DEF5678 user.two
GHI9012 user.three
```

Start the interactive paste flow:

```bash
./launchers/run-eudm-user-returns.command
```

Or pass the whole string in one shell argument:

```bash
python3 eudm_user_returns.py $'ABC1234 user.one\nDEF5678 user.two' --simulate
```

The status menu is intentionally limited to user-deployment statuses:

1. Used stock (`Deployed - Existing Stock`)
2. New stock (`Deployed - New Stock`)
3. Loan (`Loan`)
4. Pending return (`Pending Return`)

The selected status applies to every line. Duplicate serials and malformed lines
are rejected before authentication. A preview is always shown; `--dry-run` stops
there, while `--simulate` runs the complete local workflow. `--file FILE` reads a
text file, and `-` reads standard input, including `pbpaste` output.

The batch runner is shared with the spreadsheet importer, so results, request IDs,
manual review, and retry-or-skip matching behave identically.

## Troubleshooting

| Problem | Action |
| --- | --- |
| SSO redirect or unauthenticated Chrome | Refresh/complete SSO in the dedicated EUDM Chrome window, then retry. |
| Cookie mode redirects to SSO | Refresh the browser session and replace the whole `EUDM_COOKIE`, or use Chrome mode. |
| Certificate validation error | Confirm the corporate certificate is trusted by the operating system; API requests use system `curl` directly. |
| No serial or username match | The request pauses and asks for a corrected value or `skip`; it never guesses. |
| More than one exact serial/user match | The request pauses and asks for a more specific value or `skip`; it never picks a match for you. |
| `NOT SUBMITTED` in spreadsheet results | Manual review declined the final order; the shown request ID is populated but not ordered. |
| No `Bookings 2026` tab | Select the correct tab from the numbered fallback list. |
| No spreadsheet actions | Check date, red formatting, J serials, and D/F user data. |
| Duplicate serial error | Correct the source sheet before submitting; duplicate requests are blocked deliberately. |

When requesting help, provide the friendly error and the last displayed step.
Do not provide cookies, raw SSO HTML, HAR files, or EUDM response bodies.
