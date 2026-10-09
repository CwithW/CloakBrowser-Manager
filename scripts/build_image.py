"""Resolve browser versions once per release; rebuild only from committed pins."""
from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "docker/browser-versions.json"
REQUIREMENTS = ROOT / "backend/requirements.txt"


def download(url):
    # Use the configured proxy for upstream assets even if the workstation's
    # no_proxy list normally bypasses PyPI; curl also retries transient EOFs.
    return subprocess.check_output(["curl", "--fail", "--silent", "--show-error", "--location", "--retry", "5",
                                    "--retry-all-errors", "--connect-timeout", "30", "--max-time", "300", "--noproxy", "", url])


def fetch_json(url):
    return json.loads(download(url))


def refresh():
    # Resolve against the same Debian base and mirrors used by Dockerfile.
    probe = '''set -e
sed -i -e 's|http://deb.debian.org/debian-security|https://mirrors.tuna.tsinghua.edu.cn/debian-security|g' -e 's|http://deb.debian.org/debian|https://mirrors.tuna.tsinghua.edu.cn/debian|g' /etc/apt/sources.list.d/debian.sources
apt-get -o Acquire::Retries=5 update >&2
python -c 'import json,re,subprocess; print(json.dumps({p:re.search(r"Candidate: (\\S+)",subprocess.check_output(["apt-cache","policy",p],text=True)).group(1) for p in ("chromium","firefox-esr")}))'
'''
    command = ["docker", "run", "--rm", "--network=host"]
    for key in ("http_proxy", "https_proxy", "no_proxy"):
        if key in os.environ:
            command.extend(["--env", key])
    packages = json.loads(subprocess.check_output([*command, "python:3.12-slim", "sh", "-c", probe], text=True))
    cloak = fetch_json("https://pypi.org/pypi/cloakbrowser/json")
    cam = fetch_json("https://pypi.org/pypi/camoufox/json")
    wheel = next(item for item in cloak["urls"] if item["filename"].endswith(".whl"))
    content = download(wheel["url"])
    assert hashlib.sha256(content).hexdigest() == wheel["digests"]["sha256"]
    with zipfile.ZipFile(io.BytesIO(content)) as zipped:
        config = ast.parse(zipped.read("cloakbrowser/config.py"))
    # The pinned SDK publishes the latest public/free binary in its config.
    public_version = next(ast.literal_eval(node.value) for node in config.body
                          if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "CHROMIUM_VERSION" for target in node.targets))
    releases = fetch_json("https://api.github.com/repos/daijro/camoufox/releases?per_page=30")
    asset = next(asset for release in releases if not release["draft"] for asset in release["assets"]
                 if re.fullmatch(r"camoufox-.+-lin\.x86_64\.zip", asset["name"]))
    match = re.fullmatch(r"camoufox-(.+)-(beta\.\d+|alpha\.\d+|stable\.\d+)-lin\.x86_64\.zip", asset["name"])
    if match is None:
        raise RuntimeError("Camoufox asset naming changed; inspect its version before updating the lock")
    checksum = (asset.get("digest") or "").removeprefix("sha256:")
    if not re.fullmatch(r"[a-f0-9]{64}", checksum):
        # Older GitHub assets lack digest metadata. Stream instead of holding
        # the browser archive in memory while resolving the release lock.
        with tempfile.TemporaryFile() as stream, urllib.request.urlopen(asset["browser_download_url"], timeout=60) as response:
            while chunk := response.read(1024 * 1024):
                stream.write(chunk)
            stream.seek(0)
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    lock = {
        "schema": 1, "platform": "linux/amd64",
        "chromium": {"package": "chromium", "version": packages["chromium"]},
        "firefox": {"package": "firefox-esr", "version": packages["firefox-esr"]},
        "camoufox": {"python_version": cam["info"]["version"], "version": match[1], "release": match[2],
                     "url": asset["browser_download_url"], "sha256": checksum},
        "cloakbrowser": {"python_version": cloak["info"]["version"], "version": public_version, "tier": "free"},
    }
    requirements = REQUIREMENTS.read_text()
    for kind in ("cloakbrowser", "camoufox"):
        requirements, count = re.subn(rf"(?m)^{kind}\[geoip\].*$", f"{kind}[geoip]=={lock[kind]['python_version']}", requirements)
        assert count == 1, f"Missing {kind} dependency"
    LOCK.write_text(json.dumps(lock, indent=2) + "\n")
    REQUIREMENTS.write_text(requirements)
    print(LOCK.read_text(), end="")
    print("Review and commit the lock and requirements before tagging the release.")


def build(args):
    lock = json.loads(LOCK.read_text())
    requirements = REQUIREMENTS.read_text().splitlines()
    for kind in ("cloakbrowser", "camoufox"):
        assert f"{kind}[geoip]=={lock[kind]['python_version']}" in requirements, f"{kind} SDK differs from browser lock"
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if args.release_tag:
        tagged = subprocess.check_output(["git", "rev-parse", f"{args.release_tag}^{{commit}}"], cwd=ROOT, text=True).strip()
        assert tagged == commit, "Release tag must point to the checked-out commit"
        assert not subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip(), "Release builds require a clean checkout"
        committed = subprocess.check_output(["git", "show", f"{args.release_tag}:docker/browser-versions.json"], cwd=ROOT)
        assert committed == LOCK.read_bytes(), "Release tag must contain the exact browser lock"
    command = ["docker", "build", "--network=host", "--platform=linux/amd64",
               "--build-arg", f"GIT_COMMIT={commit}", "--build-arg", f"RELEASE_VERSION={args.release_tag or 'development'}"]
    for key in ("http_proxy", "https_proxy", "no_proxy"):
        if key in os.environ:
            command.extend(["--build-arg", key])
    subprocess.run([*command, "--tag", args.tag, str(ROOT)], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh-lock", action="store_true", help="Resolve current upstream versions, update lock/SDK pins, then exit")
    parser.add_argument("--tag", help="Local Docker image tag to build from the lock")
    parser.add_argument("--release-tag", help="Require a clean checkout of this Git tag, including its committed version lock")
    arguments = parser.parse_args()
    if arguments.refresh_lock:
        if arguments.tag or arguments.release_tag:
            parser.error("Refresh and commit the lock before building a release")
        refresh()
    elif arguments.tag:
        build(arguments)
    else:
        parser.error("Use --refresh-lock or --tag IMAGE")
