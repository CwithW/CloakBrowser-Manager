# Agent Instructions

## Quality Gates

Before completing work that changes code, run the relevant checks:

```bash
.venv/bin/python -m pytest backend/tests -q
cd frontend && npm test -- --run --maxWorkers=1
cd frontend && npm run build
bash -n entrypoint.sh
docker compose config
git diff --check
```

If a check cannot run in the current environment, report the exact blocker.

## Reproducible Browser Releases

- Before a new version is tagged, resolve the currently available upstream
  browser versions with `python scripts/build_image.py --refresh-lock` and
  commit `docker/browser-versions.json` together with the exact CloakBrowser
  and Camoufox SDK versions in `backend/requirements.txt`.
- Every tagged release commit must contain exact Chromium, Firefox ESR,
  Camoufox binary and CloakBrowser binary versions. Camoufox downloads must
  also have their archive URL and SHA-256 checksum committed. CloakBrowser
  uses the latest binary available without a license key unless a licensed
  build is explicitly requested; its SDK must support exact binary downloads.
- Build releases with `python scripts/build_image.py --tag IMAGE
  --release-tag GIT_TAG`. Rebuilding a tag must read that tag's committed pins;
  never refresh versions, use floating browser downloads, or fall back to
  another version when the pinned version cannot be installed.
- Keep browser self-updates disabled: hold native browser APT packages,
  disable Chromium component updates, apply Firefox/Camoufox's
  `DisableAppUpdate` policy, and force CloakBrowser's bundled binary with
  `CLOAKBROWSER_AUTO_UPDATE=false`. Changing browser versions requires a new
  committed lock and repeating the real Docker browser/protocol E2E tests.

## Release Completion

When a task explicitly includes a release or push:

1. Run the quality gates.
2. Confirm the intended commit and version tag.
3. Push the commit and tag to the configured remote.
4. Build and push the requested release image.
5. Verify the remote Git refs and Docker image digest.
