# ViewShipping Free Official-Source Relay

This public GitHub repository collects validated snapshots from three official maritime authorities and commits them under `relay/` once per hour:

- Brazil CHM — NAVAREA V warnings
- Panama Canal Authority — Advisories to Shipping
- Maritime and Port Authority of Singapore — Port Marine Notices

The repository contains no paid service, secret API key, proxy or hosted server.

## First run

1. Open the repository's **Actions** tab.
2. Select **Update ViewShipping relay**.
3. Select **Run workflow**.
4. Wait for the run to finish successfully.
5. Open `relay/manifest.json` and confirm each source shows `"status": "ok"`.

The companion WordPress plugin reads the public raw files from this repository. Keep the repository public.

## Failure behaviour

A temporary source failure does not erase the last valid snapshot. The collector records the failed attempt in the manifest and retains the previous files.
