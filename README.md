# DWP device request automation

First-pass automation for the Macquarie Digital Workplace device-management request.

## Contents

- `automate_device_request.py` — dynamic REST client for the captured questionnaire flow.
- `big.har` — source network capture used to map the API workflow.
- `requestform.txt` — source Angular DOM capture used to map labels and question IDs.

The captures contain internal and personal data. This repository is public by request; do not add authentication cookies, tokens, or additional sensitive captures.

## Run

The script requires an authenticated DWP session. Supply the browser session's `Cookie` request header through `DWP_COOKIE`; it is not stored by the script.

To avoid copying cookies, install Playwright and use a dedicated Chrome profile. The browser mode launches the installed Google Chrome binary directly through Playwright (no AppleScript, `osascript`, or Apple Events), keeps API calls inside that authenticated browser context, and asks you to complete SSO interactively:

```bash
python3 -m pip install -r requirements-browser.txt
python3 automate_device_request.py --browser-profile ~/.dwp-device-request-chrome --serial K9JQ6MYW9R --request-for rkontos --status 'Deployed - New Stock' --deployed-to rkontos
```

For a location deployment with browser-based SSO:

```bash
python3 automate_device_request.py --browser-profile ~/.dwp-device-request-chrome --serial K9JQ6MYW9R --request-for rkontos --status 'Used Stock' --city 'Sydney, AU' --building '1 Elizabeth Street' --floor 'Level 15' --room 'Store Room' --dropped-by rkontos
```

`--city` filters the available locations. The script then requires an exact building, floor, and room match before sending the opaque location ID returned by DWP. Add `--cabinet 'Cabinet Name'` if those three fields still match more than one row.

The profile path is separate from your normal Chrome profile so the automation does not interfere with it. Subsequent runs can reuse the profile while its SSO session remains valid; cookies are not extracted and replayed through a separate HTTP client.

If Python/OpenSSL rejects the work computer's certificate chain, the client automatically retries that request with the system `curl` trust store. This keeps certificate verification enabled and lets macOS Keychain trust be used. Run with `--verbose` to see when the fallback occurs.

User deployment:

```bash
export DWP_COOKIE='...'
python3 automate_device_request.py --serial K9JQ6MYW9R --request-for rkontos --status 'Deployed - New Stock' --deployed-to rkontos
```

Location deployment:

```bash
python3 automate_device_request.py --serial K9JQ6MYW9R --request-for rkontos --status 'Used Stock' --city 'Sydney, AU' --building '1 Elizabeth Street' --floor 'Level 15' --room 'Store Room' --dropped-by rkontos
```

The examples above are dry runs with respect to the final order commit. To commit a request after checking the output, add `--submit`:

```bash
python3 automate_device_request.py --serial K9JQ6MYW9R --request-for rkontos --status 'Deployed - New Stock' --deployed-to rkontos --submit
```

For a location deployment:

```bash
python3 automate_device_request.py --serial K9JQ6MYW9R --request-for rkontos --status 'Used Stock' --city 'Sydney, AU' --building '1 Elizabeth Street' --floor 'Level 15' --room 'Store Room' --dropped-by rkontos --submit
```

By default the script stops before the final order commit. Note that creating the request and recording answers are still server-side actions. `--submit` performs the final order commit and should only be used after validating the dynamic path.
