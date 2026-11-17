"""Verify a built wheel and its installed launcher from outside the project."""

import argparse
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zipfile import ZipFile


def _validate_wheel(wheel: Path) -> None:
    """Verify that the wheel includes the settings defaults and built web bundle."""
    with ZipFile(wheel) as archive:
        paths = archive.namelist()
        index = "daily_ml/web/index.html"
        assert index in paths and "daily_ml/defaults.toml" in paths
        assets = re.findall(rb'(?:src|href)="(/assets/[^"]+)"', archive.read(index))
        assert any(asset.endswith(b".js") for asset in assets)
        assert any(asset.endswith(b".css") for asset in assets)
        assert all(f"daily_ml/web{asset.decode()}" in paths for asset in assets)


def _exercise(origin: str) -> None:
    """Verify the same HTTP origin delivers the compiled UI and the protected API."""
    with urlopen(origin, timeout=3) as page:
        html = page.read()
    asset = re.search(rb'src="(/assets/[^"]+\.js)"', html)
    assert asset is not None
    with urlopen(origin + asset[1].decode(), timeout=3) as script:
        assert script.status == 200 and script.read()
    with urlopen(origin + "/api/session", timeout=3) as session:
        token = json.load(session)["csrf_token"]
        cookie = session.headers["Set-Cookie"].split(";", 1)[0]
    headers = {"Cookie": cookie}
    with urlopen(Request(origin + "/api/connection", headers=headers), timeout=3) as status:
        assert json.load(status)["state"] == "signed_out"
    action = Request(
        origin + "/api/connection/sign-out", method="POST",
        headers={**headers, "Origin": origin, "X-CSRF-Token": token},
    )
    with urlopen(action, timeout=3) as sign_out:
        assert json.load(sign_out)["state"] == "signed_out"
    try:
        urlopen(Request(origin, headers={"Host": "localhost"}), timeout=3).close()
    except HTTPError as error:
        assert error.code == 400
        error.close()
    else:
        raise AssertionError("Unexpected Host was accepted")


def _launch(executable: Path, root: Path) -> None:
    """Start and stop the installed executable, checking its clean shutdown."""
    log_path = root / "launcher.log"
    env = {**os.environ, "DML_DATA_DIR": str(root / "App data"), "DML_PORT": "0"}
    command = [str(executable), "run", "--no-browser"]
    with log_path.open("wb") as output:
        child = subprocess.Popen(command, cwd=root, env=env, stdout=output, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                log = log_path.read_text()
                address = re.search(r"http://127\.0\.0\.1:\d+", log)
                if address:
                    _exercise(address[0])
                    break
                if child.poll() is not None:
                    raise AssertionError(f"Installed launcher exited early: {log}")
                time.sleep(0.05)
            else:
                raise AssertionError("Installed launcher did not report its loopback address")
        finally:
            if child.poll() is None:
                child.send_signal(signal.SIGINT)
                try:
                    child.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                    raise
    assert child.returncode == 0, f"Unexpected launcher exit {child.returncode}"


def main() -> None:
    """Check an installed launcher and wheel using a temporary data directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("executable", type=Path)
    args = parser.parse_args()
    _validate_wheel(args.wheel)
    with tempfile.TemporaryDirectory(prefix="daily-ml installed ") as directory:
        executable = args.executable.absolute()
        result = subprocess.run(
            [str(executable), "validate-bank"], cwd=directory, check=True,
            capture_output=True, text=True, timeout=8,
        )
        assert "12 questions across 6 sections" in result.stdout
        _launch(executable, Path(directory))
    print("Wheel and installed launcher passed.")


if __name__ == "__main__":
    main()
