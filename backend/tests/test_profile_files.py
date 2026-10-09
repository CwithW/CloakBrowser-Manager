"""Home transfers preserve files and stay within one profile's directory."""

from pathlib import Path

import pytest

from backend import profile_files


def test_upload_download_and_list(app_client):
    profile = app_client.post("/api/profiles", json={"name": "Files"}).json()
    base = f"/api/profiles/{profile['id']}/files"
    content = b"binary\x00\xff"
    response = app_client.put(base + "/upload?path=Downloads/test.bin", content=content)
    assert response.status_code == 200
    assert (Path(profile["user_data_dir"]) / "Home/Downloads/test.bin").read_bytes() == content
    listing = app_client.get(base + "?path=Downloads").json()
    assert listing["entries"][0]["name"] == "test.bin"
    assert app_client.get(base + "/download?path=Downloads/test.bin").content == content
    assert app_client.put(base + "/upload?path=Downloads/test.bin", content=b"overwrite").status_code == 409
    assert app_client.get(base + "/download?path=Downloads/test.bin").content == content


@pytest.mark.parametrize("path", ["../profiles.db", "/etc/passwd", "Downloads/../../profiles.db"])
def test_reject_paths_outside_home(app_client, path):
    profile = app_client.post("/api/profiles", json={"name": "Files"}).json()
    base = f"/api/profiles/{profile['id']}/files"
    for endpoint in ("", "/download"):
        assert app_client.get(base + endpoint, params={"path": path}).status_code == 400
    assert app_client.put(base + "/upload", params={"path": path}, content=b"test").status_code == 400


def test_reject_symlinks_and_isolate_profiles(app_client, tmp_path):
    one = app_client.post("/api/profiles", json={"name": "One"}).json()
    two = app_client.post("/api/profiles", json={"name": "Two"}).json()
    base = f"/api/profiles/{one['id']}/files"
    app_client.put(base + "/upload?path=private.txt", content=b"private")
    assert app_client.get(f"/api/profiles/{two['id']}/files/download?path=private.txt").status_code == 404
    root = profile_files.profile_home(one)
    (root / "external").symlink_to(tmp_path)
    assert app_client.get(base + "?path=external").status_code == 400
    assert not any(entry["name"] == "external" for entry in app_client.get(base).json()["entries"])


def test_upload_limit_removes_incomplete_file(app_client, monkeypatch):
    monkeypatch.setattr(profile_files, "MAX_UPLOAD", 3)
    profile = app_client.post("/api/profiles", json={"name": "Files"}).json()
    base = f"/api/profiles/{profile['id']}/files"
    assert app_client.put(base + "/upload?path=large.bin", content=b"1234").status_code == 413
    names = [entry["name"] for entry in app_client.get(base).json()["entries"]]
    assert names == ["Downloads"]
