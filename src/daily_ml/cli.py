"""Launch the installed app or validate its content bank offline."""

import argparse
import asyncio
import json
import logging
import socket
import sqlite3
import webbrowser
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn

from .app import LaunchError, create_app, resolve_assets
from .bank import BankError, validate_bank
from .config import ConfigError, load_settings, resolve_installation
from .credentials import StorageError
from .connection import Connection
from .database import Database
from .grader_evaluation import EvaluationRunner, load_fixed_cases
from .locking import LockBusy, file_lock
from .provider import Provider

LOOPBACK = "127.0.0.1"
logger = logging.getLogger(__name__)


@contextmanager
def own_data_directory(data_dir: Path) -> Iterator[None]:
    """Hold the installation's single-server lock until shutdown.

    Parameters
    ----------
    data_dir : Path
        Resolved local app data directory.

    Yields
    ------
    None
        While this process owns the directory.

    Raises
    ------
    LaunchError
        If another server already owns the directory.
    """
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with file_lock(data_dir / ".server.lock", wait=0):
            yield
    except LockBusy:
        raise LaunchError("Daily ML Practice is already running for this data directory.") from None


def bind_loopback(port: int) -> socket.socket:
    """Reserve an IPv4 loopback socket without a check-then-bind gap.

    Parameters
    ----------
    port : int
        Requested port. Zero lets the operating system select an available port.

    Returns
    -------
    socket.socket
        Bound socket to pass directly to the single server.

    Raises
    ------
    LaunchError
        If the requested port cannot be used.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((LOOPBACK, port))
        listener.listen()
        return listener
    except OSError:
        listener.close()
        raise LaunchError(
            f"Could not bind 127.0.0.1 port {port}. Close the process using it or set DML_PORT=0."
        ) from None


@contextmanager
def _logging(data_dir: Path) -> Iterator[None]:
    """Record only app-owned, redacted events in bounded files and on the console."""
    app_logger = logging.getLogger("daily_ml")
    previous_level = app_logger.level
    previous_propagation = app_logger.propagate
    file = RotatingFileHandler(data_dir / "daily-ml.log", maxBytes=256_000, backupCount=2, encoding="utf-8")
    file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    console = logging.StreamHandler()
    app_logger.setLevel(logging.INFO)
    app_logger.propagate = False
    for handler in (file, console):
        app_logger.addHandler(handler)
    try:
        yield
    finally:
        for handler in (file, console):
            app_logger.removeHandler(handler)
            handler.close()
        app_logger.setLevel(previous_level)
        app_logger.propagate = previous_propagation


class _LocalServer(uvicorn.Server):
    """Open the browser only once the server is ready to accept requests."""

    def __init__(self, config: uvicorn.Config, *, url: str, open_browser: bool) -> None:
        super().__init__(config)
        self.url = url
        self.open_browser = open_browser

    async def startup(self, sockets=None):
        """Publish the actual URL and optionally launch it in the system browser."""
        await super().startup(sockets)
        if not self.started:
            return
        logger.info("Daily ML Practice is available at %s — press Ctrl+C to stop.", self.url)
        if self.open_browser:
            try:
                opened = await asyncio.to_thread(webbrowser.open, self.url)
            except (OSError, webbrowser.Error):
                opened = False
            if not opened:
                logger.info("Could not open the browser. Open the address above manually.")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the installed Daily ML Practice command.

    Parameters
    ----------
    argv : Sequence of str or None
        Command arguments, defaulting to the process arguments.

    Returns
    -------
    int
        Zero after clean shutdown, or one for a startup failure.
    """
    parser = argparse.ArgumentParser(prog="daily-ml")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Start Daily ML Practice")
    run.add_argument("--no-browser", action="store_true", help="Print the address and leave the browser closed")
    run.add_argument("--config", type=Path, help="Use this installation TOML")
    evaluate = commands.add_parser("evaluate-grader", help="Run the reviewed fixed grader set using your ChatGPT plan")
    evaluate.add_argument("--model", required=True, help="Account model to use for the explicit live evaluation")
    evaluate.add_argument("--config", type=Path, help="Use this installation TOML")
    evaluate.add_argument("--confirm-reviewed-fixtures", action="store_true",
                          help="Confirm an independent reviewer checked expected fixture outcomes")
    evaluate.add_argument("--recover-confirmed-failures", action="store_true",
                          help="Explicitly create linked retries for confirmed evaluation failures")
    validate = commands.add_parser("validate-bank", help="Validate the reviewed question bank offline")
    validate.add_argument("--bank-dir", type=Path, help="Read a candidate curriculum and questions directory")
    args = parser.parse_args(argv)
    try:
        if args.command == "evaluate-grader":
            if not args.confirm_reviewed_fixtures:
                parser.exit(2, "Daily ML Practice: pass --confirm-reviewed-fixtures after independent review.\n")
            installation = resolve_installation(args.config)
            interrupted = False
            with own_data_directory(installation.data_dir), _logging(installation.data_dir):
                settings = load_settings(installation=installation)
                cases = load_fixed_cases()
                database = Database(settings.data_dir)
                connection = Connection(settings.data_dir)
                try:
                    runner = EvaluationRunner(database, Provider(connection, timeout=settings.request_timeout), settings)
                    runner.jobs.recover_interrupted()
                    print(
                        "Planned requests: at most 18 model sends in one explicit invocation (possibly fewer). "
                        "Model sends may use your account's shared ChatGPT-plan usage. Model catalog and OAuth "
                        "traffic are separate. Retries and cumulative counts can exceed "
                        "this over explicit resumptions."
                    )
                    run_id = runner._run_id(args.model, cases)
                    try:
                        summary = asyncio.run(runner.run(
                            args.model, cases=cases, recover_confirmed_failures=args.recover_confirmed_failures,
                        ))
                    except KeyboardInterrupt:
                        summary = runner.summary(run_id, cases)
                        interrupted = True
                    report = runner.report(summary.run_id, cases=cases)
                    report["interrupted"] = interrupted
                    path = settings.data_dir / f"grader-evaluation-{summary.run_id}.json"
                    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                    print(json.dumps({"report": str(path), "completed": summary.completed_count,
                                      "agreement": summary.agreement_count, "critical_coverage": summary.critical_coverage_count,
                                      "release_passed": summary.release_passed, "paused": summary.paused_state,
                                      "interrupted": interrupted}, sort_keys=True))
                finally:
                    connection.close()
            return 1 if interrupted else 0
        if args.command == "validate-bank":
            bank = validate_bank(args.bank_dir)
            section_count = sum(len(domain.sections) for domain in bank.curriculum.domains)
            print(f"Validated {len(bank.questions)} questions across {section_count} sections.")  # CLI output
            return 0
        assets = resolve_assets()
        installation = resolve_installation(args.config)
        with own_data_directory(installation.data_dir), _logging(installation.data_dir):
            settings = load_settings(installation=installation)
            with bind_loopback(settings.port) as listener:
                port = listener.getsockname()[1]
                app = create_app(settings, port=port, assets_dir=assets)
                config = uvicorn.Config(
                    app, host=LOOPBACK, port=port, workers=1, access_log=False,
                    proxy_headers=False, log_level="warning", timeout_graceful_shutdown=25,
                )
                server = _LocalServer(
                    config, url=f"http://{LOOPBACK}:{port}", open_browser=not args.no_browser
                )
                server.run(sockets=[listener])
                if not server.started:
                    raise LaunchError("Could not start Daily ML Practice. Check local data access.")
        return 0
    except KeyboardInterrupt:
        return 0
    except PackageNotFoundError:
        parser.exit(1, "Daily ML Practice: Could not read installed package versions. "
                       "Reinstall Daily ML Practice and its dependencies.\n")
    except (BankError, ConfigError, LaunchError, StorageError) as error:
        parser.exit(1, f"Daily ML Practice: {error}\n")
    except (OSError, sqlite3.Error):
        parser.exit(1, "Daily ML Practice: Could not open local data. Check your data directory and permissions.\n")
