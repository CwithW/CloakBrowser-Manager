"""Install and verify exactly the browser versions committed with this image."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import logging
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

LOCK_PATH = Path(__file__).with_name("browser-versions.json")
CLOAK_DIR = Path("/opt/cbm/cloakbrowser")
UPDATE_POLICY = {"policies": {"DisableAppUpdate": True}}


def native(lock):
    packages = [f"{lock[kind]['package']}={lock[kind]['version']}" for kind in ("chromium", "firefox")]
    subprocess.run(["apt-get", "-o", "Acquire::Retries=5", "update"], check=True)
    subprocess.run(["apt-get", "-o", "Acquire::Retries=5", "install", "-y", "--no-install-recommends", *packages],
                   check=True, env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"})
    subprocess.run(["apt-mark", "hold", "chromium", "chromium-common", "firefox-esr"], check=True)
    write_policy(Path("/usr/lib/firefox-esr"))


def write_policy(directory):
    path = directory / "distribution/policies.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(path.read_text()) if path.exists() else {"policies": {}}
    existing.setdefault("policies", {}).update(UPDATE_POLICY["policies"])
    path.write_text(json.dumps(existing, indent=2) + "\n")


def camoufox(lock):
    from camoufox.pkgman import INSTALL_DIR
    from camoufox.multiversion import BROWSERS_DIR, COMPAT_FLAG, save_config

    version = lock["camoufox"]
    directory = BROWSERS_DIR / "official" / f"{version['version']}-{version['release']}-{version['sha256'][:8]}"
    assert importlib.metadata.version("camoufox") == version["python_version"]
    cache = Path("/var/cache/cbm-browsers")
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / (version["sha256"] + ".zip")
    if not archive.exists():
        temporary = archive.with_suffix(".partial")
        subprocess.run(["curl", "--fail", "--location", "--retry", "5", "--retry-all-errors",
                        "--connect-timeout", "30", "--max-time", "1200", version["url"], "--output", str(temporary)], check=True)
        with temporary.open("rb") as stream:
            assert hashlib.file_digest(stream, "sha256").hexdigest() == version["sha256"], "Camoufox checksum mismatch"
        temporary.replace(archive)
    with archive.open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == version["sha256"], "Camoufox checksum mismatch"
    # Build steps always extract into a new image layer, never a user profile.
    directory.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zipped:
        for name in zipped.namelist():
            assert (directory / name).resolve().is_relative_to(directory.resolve()), "Invalid archive path"
        zipped.extractall(directory)
    for path in directory.rglob("*"):
        path.chmod(0o755)
    (directory / "version.json").write_text(json.dumps({"version": version["version"], "build": version["release"],
                                                        "sha256": version["sha256"], "interface": 1}))
    COMPAT_FLAG.touch()
    save_config({"active_version": str(directory.relative_to(INSTALL_DIR)), "pinned": f"official/{version['version']}-{version['release']}"})
    write_policy(directory)
    from camoufox.pkgman import camoufox_path
    assert camoufox_path(download_if_missing=False) == directory
    # Bundle the SDK's default addons during the build, rather than on launch.
    from camoufox.utils import launch_options
    launch_options(headless=True, geoip=False)


def cloakbrowser(lock):
    from cloakbrowser import download
    from cloakbrowser.config import get_binary_path, get_binary_dir, get_fallback_download_url

    version = lock["cloakbrowser"]
    assert importlib.metadata.version("cloakbrowser") == version["python_version"]
    # Build the public binary, independently of any runtime license/settings.
    for key in ("CLOAKBROWSER_BINARY_PATH", "CLOAKBROWSER_LICENSE_KEY", "CLOAKBROWSER_DOWNLOAD_URL"):
        os.environ.pop(key, None)
    os.environ["CLOAKBROWSER_AUTO_UPDATE"] = "false"
    pinned = version["version"]
    binary = get_binary_path(pinned)
    if not binary.exists():
        # Prefer the official GitHub asset on this network. Keep the pinned
        # SDK's mandatory signature, version binding and checksum verification;
        # a custom CLOAKBROWSER_DOWNLOAD_URL would bypass that official path.
        archive = Path("/root/.cloakbrowser") / f"download-{pinned}.tar.gz"
        archive.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["curl", "--fail", "--location", "--retry", "5", "--retry-all-errors",
                        "--connect-timeout", "30", "--max-time", "1200", get_fallback_download_url(pinned), "--output", str(archive)], check=True)
        download._verify_download_checksum(archive, pinned)
        download._extract_archive(archive, get_binary_dir(pinned), binary)
    source = Path(download.ensure_binary(browser_version=pinned))
    shutil.copytree(source.parent, CLOAK_DIR)


def verify(lock):
    installed = {}
    for kind in ("chromium", "firefox"):
        version = subprocess.check_output(["dpkg-query", "-W", "-f=${Version}", lock[kind]["package"]], text=True)
        assert version == lock[kind]["version"], (kind, version)
        installed[kind] = version
    from camoufox.pkgman import camoufox_path
    cam_directory = camoufox_path(download_if_missing=False)
    cam = json.loads((cam_directory / "version.json").read_text())
    assert (cam["version"], cam["build"], cam["sha256"]) == tuple(lock["camoufox"][key] for key in ("version", "release", "sha256")), cam
    for kind in ("camoufox", "cloakbrowser"):
        assert importlib.metadata.version(kind) == lock[kind]["python_version"]
    assert (CLOAK_DIR / "chrome").is_file()
    for directory in (Path("/usr/lib/firefox-esr"), cam_directory):
        assert json.loads((directory / "distribution/policies.json").read_text())["policies"]["DisableAppUpdate"] is True
    installed.update(camoufox=cam, cloakbrowser=lock["cloakbrowser"])
    print(json.dumps(installed, indent=2))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    manifest = json.loads(LOCK_PATH.read_text())
    assert manifest["schema"] == 1 and manifest["platform"] == "linux/amd64"
    {"native": native, "camoufox": camoufox, "cloakbrowser": cloakbrowser, "verify": verify}[sys.argv[1]](manifest)
