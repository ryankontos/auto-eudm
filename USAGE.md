# Operating guide

This guide covers the four command-line interfaces, their safety boundaries,
and the Sydney workbook import rules. Run any command with `--help` to see the
exact argument reference for the version you have checked out.

## Choose a command

| Command | Best for | Result |
| --- | --- | --- |
| `automate_device_request.py` | Repeatable, fully specified single or batch requests | Populates one DWP request; batch mode applies several serials to one location. |
| `interactive_device_request.py` | Choosing from live DWP devices, statuses, locations, and people | A numbered wizard for one device or batch-to-location flow. |
| `inventory_sheet_cli.py` | Deploying devices from an Inventory Tracking - Sydney workbook | One user request per selected new or old serial, with grouped results. |
| `serial_user_cli.py` | Pasting or piping `SERIAL USERNAME` pairs | One user request per line, with one status applied to the entire batch. |

## macOS launchers

The repository includes double-clickable launchers:

| File | Runs |
| --- | --- |
| `run-device-request.command` | `automate_device_request.py` |
| `run-interactive-device-request.command` | `interactive_device_request.py` |
| `run-inventory-sheet.command` | `inventory_sheet_cli.py` |
| `run-serial-user-batch.command` | `serial_user_cli.py` |

They locate the repository from the launcher’s own path, so they work from
Finder or Terminal. Arguments are passed through unchanged:

```bash
./run-interactive-device-request.command --simulate --manual
```

```bash
./run-inventory-sheet.command --dry-run
```

Opening `run-device-request.command` without arguments displays the direct
script help and a copyable example because that script requires request values.
The other three launchers start their interactive flows when opened without
arguments. The serial/user launcher accepts pasted lines until a blank line.

## Shared `.env` configuration

All four CLIs load the gitignored `.env` beside the scripts. The repository also
contains `.env.example`, which documents every supported setting. On a new clone,
run `cp .env.example .env`, then edit the local file to match the work computer.
This current checkout already has that generated local file. Set `DWP_ENV_FILE`
to use a configuration file elsewhere.

| Variable | Used for |
| --- | --- |
| `DWP_REQUEST_FOR` | Default requesting login ID. |
| `DWP_BROWSER_PROFILE` | Dedicated installed-Chrome profile. |
| `DWP_BASE` | DWP REST base URL. |
| `DWP_CITY`, `DWP_BUILDING`, `DWP_FLOOR`, `DWP_ROOM`, `DWP_CABINET` | Direct-script location defaults. |
| `DWP_DEFAULT_USER_STATUS` | Default direct/new pasted-pair user status. |
| `DWP_DEFAULT_LOCATION_STATUS` | Default direct location status. |
| `DWP_SIMULATE`, `DWP_VERBOSE`, `DWP_MANUAL_REVIEW` | Shared default CLI modes. |

Command-line options take precedence over shell variables, which take precedence
over `.env`. Use `--no-simulate`, `--no-verbose`, or `--no-manual-review` when a
boolean is enabled in `.env` but should be disabled for one run. `DWP_COOKIE` is
deliberately excluded from the file; keep it short-lived in the shell.

## Safety and modes

| Mode | Browser/network | Real DWP request | Final DWP order |
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
DWP, or changes real data. It tests the interface and validation, not whether
an actual serial or user exists.

Normal output intentionally stays compact: prompts, previews, final IDs, results,
and friendly errors are shown. Add `--verbose` only for field-by-field
questionnaire progress, matches, per-request spreadsheet progress, and safe
transport diagnostics.

## Manual review before ordering

All frontends support `--manual-review` (also available as `--review` or
`--manual`). It
displays the values already populated in the request — request ID, requester,
serial or serial list, status, and destination — then asks for an explicit
`y`/`n` before the final DWP order. It does not alter any answers.

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
presentation without DWP access.

## Real authentication

The recommended real-DWP path is a dedicated Chrome profile:

```bash
python3 interactive_device_request.py --browser-profile ~/.dwp-device-request-chrome
```

Complete SSO in the opened browser and then return to the terminal. This profile
is separate from normal Chrome. The automation launches Chrome through
Playwright; it does not use Apple Events, AppleScript, or `osascript`.

Alternatively set `DWP_COOKIE` to the full browser `Cookie` request header and
use `--cookie-mode` in any CLI. Cookies are never saved. Do not put
them in source code, shared shell history, or the public repository.

If Python cannot validate a corporate certificate chain, the real client retries
using macOS system `curl` trust while retaining certificate verification. Add
`--verbose` to see transport diagnostics, questionnaire field updates, matching
details, and per-request spreadsheet progress; it never prints cookies or bodies.

## Direct script

```bash
python3 automate_device_request.py --help
```

User deployment:

```bash
python3 automate_device_request.py --serial K9JQ6MYW9R --request-for rkontos --target user --status 'Deployed - New Stock' --deployed-to rkontos --submit
```

Location deployment. The location fields must exactly match one row returned
after filtering by city:

```bash
python3 automate_device_request.py --serial K9JQ6MYW9R --request-for rkontos --target location --status 'Used Stock' --city 'Sydney, AU' --building '1 Elizabeth Street' --floor 'Level 15' --room 'Store Room' --dropped-by rkontos --submit
```

Batch location deployment uses DWP’s `BULK by Serial Number` flow, checks that
every serial matches once, selects all matches, sets return-from-user to `NO`,
and rejects all user arguments:

```bash
python3 automate_device_request.py --batch --serials 'K9JQ6MYW9R,ANOTHER123,THIRD456' --request-for rkontos --target location --status 'Used Stock' --city 'Sydney, AU' --building '1 Elizabeth Street' --floor 'Level 15' --room 'Store Room' --submit
```

All combinations are validated before authentication or an API call. In
particular, user targets reject location fields; normal locations require city,
building, floor, room, and a drop-off user; batch locations do not accept one.

Local direct-script rehearsal:

```bash
python3 automate_device_request.py --simulate --serial ABC1234 --request-for tester --target user --status 'Deployed - New Stock' --deployed-to simulated.user --submit
```

Add `--manual-review` to the command above to rehearse the final review screen.

## Interactive wizard

```bash
python3 interactive_device_request.py --help
```

```bash
python3 interactive_device_request.py
```

The wizard begins with one-device versus batch-to-location mode, then lists
available choices with numbers. It asks before final submission. Cancelling at
that point leaves a real request populated but not ordered.

Use this to rehearse every prompt locally:

```bash
python3 interactive_device_request.py --simulate
```

Add `--manual-review` to see the populated request summary immediately before
the final approval.

## Spreadsheet importer

Install the workbook reader once:

```bash
python3 -m pip install -r requirements-sheet.txt
```

```bash
python3 inventory_sheet_cli.py --help
```

With no file argument, the importer offers the newest Downloads file whose name
starts with `Inventory Tracking - Sydney` and ends in `.xlsx` or `.xlsm`.
Press Enter to accept it or type a different path. Supplying a file skips that
choice:

```bash
python3 inventory_sheet_cli.py '/path/to/Inventory Tracking - Sydney.xlsx'
```

It selects `Bookings 2026` automatically. If that tab is absent, it displays
all sheet names and asks you to select one, rather than silently using an active
sheet.

### Workbook mapping

| Column | Meaning | Behaviour |
| --- | --- | --- |
| A | Deployment date | The CLI lists dates and asks which date to process. |
| D | Username | Used as the deployed-to login ID. |
| F | Email | Fallback only: the text before `@` is used if D is unavailable. |
| J | New serial | Required. It becomes a new user deployment. |
| L | Old serial | If valid, it becomes a separate `Pending Return` request for that user. |

Rows are excluded before authentication when any cell in A:L has red font or
fill, J lacks a usable serial, or no username can be found in D/F. Short markers
such as `1` to `5` are not serials. The preview reports ignored rows and
email-derived usernames.

After choosing a date, choose new serials, returns, or both. New serials default
to `Deployed - New Stock`. Enter numbers/ranges such as `2,4-6` at the override
prompt to switch selected new serials to `Deployed - Existing Stock`.

The preview shows source row, serial, user, status, exclusions, and fallbacks.
It asks once more before real authentication. Results are grouped as `New
deployments` and `Old / pending return`; one failure does not hide the others.

True no-change preview:

```bash
python3 inventory_sheet_cli.py --dry-run
```

Full local rehearsal after the preview:

```bash
python3 inventory_sheet_cli.py --simulate
```

For independent per-device approval after the batch preview:

```bash
python3 inventory_sheet_cli.py --simulate --manual-review
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
./run-serial-user-batch.command
```

Or pass the whole string in one shell argument:

```bash
python3 serial_user_cli.py $'ABC1234 user.one\nDEF5678 user.two' --simulate
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
| SSO redirect or unauthenticated Chrome | Refresh/complete SSO in the dedicated DWP Chrome window, then retry. |
| Cookie mode redirects to SSO | Refresh the browser session and replace the whole `DWP_COOKIE`, or use Chrome mode. |
| Certificate validation error | Retry normally; macOS `curl` trust is attempted automatically. Add `--verbose` to confirm. |
| No serial or username match | The request pauses and asks for a corrected value or `skip`; it never guesses. |
| More than one exact serial/user match | The request pauses and asks for a more specific value or `skip`; it never picks a match for you. |
| `NOT SUBMITTED` in spreadsheet results | Manual review declined the final order; the shown request ID is populated but not ordered. |
| No `Bookings 2026` tab | Select the correct tab from the numbered fallback list. |
| No spreadsheet actions | Check date, red formatting, J serials, and D/F user data. |
| Duplicate serial error | Correct the source sheet before submitting; duplicate requests are blocked deliberately. |

When requesting help, provide the friendly error and the last displayed step.
Do not provide cookies, raw SSO HTML, HAR files, or DWP response bodies.
