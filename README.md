# DWP device request automation

First-pass automation for the Macquarie Digital Workplace device-management request.

## Contents

- `automate_device_request.py` — dynamic REST client for the captured questionnaire flow.
- `big.har` — source network capture used to map the API workflow.
- `requestform.txt` — source Angular DOM capture used to map labels and question IDs.

The captures contain internal and personal data. This repository is public by request; do not add authentication cookies, tokens, or additional sensitive captures.

## Run

The script requires an authenticated DWP session. Supply the browser session's `Cookie` request header through `DWP_COOKIE`; it is not stored by the script.

To avoid copying cookies, install Playwright and use a dedicated Chrome profile. The browser mode launches the installed Google Chrome binary directly through Playwright (no AppleScript, `osascript`, or Apple Events), keeps the cookies inside the script process, and asks you to complete SSO interactively:

```bash
python3 -m pip install -r requirements-browser.txt
python3 automate_device_request.py --browser-profile ~/.dwp-device-request-chrome --serial K9JQ6MYW9R --request-for rkontos --status 'Deployed - New Stock' --deployed-to rkontos
```

For a location deployment with browser-based SSO:

```bash
python3 automate_device_request.py --browser-profile ~/.dwp-device-request-chrome --serial K9JQ6MYW9R --request-for rkontos --status 'Used Stock' --city 'Sydney, AU' --building '1 Elizabeth Street' --dropped-by rkontos
```

The profile path is separate from your normal Chrome profile so the automation does not interfere with it. Subsequent runs can reuse the profile while its SSO session remains valid.

If Python/OpenSSL rejects the work computer's certificate chain, the client automatically retries that request with the system `curl` trust store. This keeps certificate verification enabled and lets macOS Keychain trust be used. Run with `--verbose` to see when the fallback occurs.

User deployment:

```bash
export DWP_COOKIE='...'
python3 automate_device_request.py --serial K9JQ6MYW9R --request-for rkontos --status 'Deployed - New Stock' --deployed-to rkontos
```

Location deployment:

```bash
python3 automate_device_request.py --serial K9JQ6MYW9R --request-for rkontos --status 'Used Stock' --city 'Sydney, AU' --building '1 Elizabeth Street' --dropped-by rkontos
```

The examples above are dry runs with respect to the final order commit. To commit a request after checking the output, add `--submit`:

```bash
python3 automate_device_request.py --serial K9JQ6MYW9R --request-for rkontos --status 'Deployed - New Stock' --deployed-to rkontos --submit
```

For a location deployment:

```bash
python3 automate_device_request.py --serial K9JQ6MYW9R --request-for rkontos --status 'Used Stock' --city 'Sydney, AU' --building '1 Elizabeth Street' --dropped-by rkontos --submit
```

By default the script stops before the final order commit. Note that creating the request and recording answers are still server-side actions. `--submit` performs the final order commit and should only be used after validating the dynamic path.
