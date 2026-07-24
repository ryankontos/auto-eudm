# DWP device request automation

First-pass automation for the Macquarie Digital Workplace device-management request.

## Contents

- `automate_device_request.py` — dynamic REST client for the captured questionnaire flow.
- `big.har` — source network capture used to map the API workflow.
- `requestform.txt` — source Angular DOM capture used to map labels and question IDs.

The captures contain internal and personal data. Keep this repository private and do not commit authentication cookies or tokens.

## Run

The script requires an authenticated DWP session. Supply the browser session's `Cookie` request header through `DWP_COOKIE`; it is not stored by the script.

User deployment:

```bash
export DWP_COOKIE='...'
python3 automate_device_request.py \\
  --serial K9JQ6MYW9R \\
  --request-for rkontos \\
  --status 'Deployed - New Stock' \\
  --deployed-to rkontos
```

Location deployment:

```bash
python3 automate_device_request.py \\
  --serial K9JQ6MYW9R \\
  --request-for rkontos \\
  --status 'Used Stock' \\
  --city 'Sydney, AU' \\
  --building '1 Elizabeth Street' \\
  --dropped-by rkontos
```

By default the script stops before the final order commit. Note that creating the request and recording answers are still server-side actions. `--submit` performs the final order commit and should only be used after validating the dynamic path.
