import json
import logging
import os
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - this app is deployed on Linux
    fcntl = None

logger = logging.getLogger(__name__)

_THREAD_LOCK = threading.Lock()


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        logger.warning("Invalid %s, falling back to %s", name, default)
        return default


def _signal_cli_timeout_seconds():
    return _int_env("SIGNAL_CLI_TIMEOUT_SECONDS", 120)


def _signal_receive_timeout_seconds():
    return _int_env("SIGNAL_RECEIVE_TIMEOUT_SECONDS", 5)


SIGNAL_CLI_TIMEOUT_SECONDS = _signal_cli_timeout_seconds()
SIGNAL_RECEIVE_TIMEOUT_SECONDS = _signal_receive_timeout_seconds()
SIGNAL_CLI_ERROR_MAX_CHARS = 8_000


@dataclass
class SignalAPIResult:
    ok: bool
    status_code: Optional[int] = None
    data: Optional[Any] = None
    error: Optional[str] = None

    def __bool__(self):
        return self.ok


@dataclass
class _CommandResult:
    returncode: int
    data: Optional[Any] = None
    stdout: str = ""
    stderr: str = ""


@contextmanager
def _locked_signal_data_dir(data_dir: Path):
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / ".asl-signalbot.lock"

    with _THREAD_LOCK:
        with lock_path.open("w", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _parse_json_output(stdout: str):
    stripped = stdout.strip()
    if not stripped:
        return None

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    parsed_lines = []
    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed_lines.append(json.loads(line))
        except json.JSONDecodeError:
            return stripped

    if len(parsed_lines) == 1:
        return parsed_lines[0]
    return parsed_lines


def _summarize_command_error(error: str, max_chars: int = SIGNAL_CLI_ERROR_MAX_CHARS) -> str:
    """Keep subprocess failures useful without persisting multi-megabyte crash dumps."""
    if len(error) <= max_chars:
        return error

    marker = f"\n... signal-cli error truncated ({len(error)} characters total) ...\n"
    available = max_chars - len(marker)
    head_chars = (available * 3) // 4
    return f"{error[:head_chars]}{marker}{error[-(available - head_chars):]}"


def _group_id_for_cli(recipient: str) -> Optional[str]:
    if recipient.startswith("group."):
        return recipient.removeprefix("group.")
    return None


def _group_id_for_app(group_id: str) -> str:
    if group_id.startswith("group."):
        return group_id
    return f"group.{group_id}"


class SignalAPI:
    """Small adapter around AsamK/signal-cli."""

    def __init__(
        self,
        recipients: Optional[List[str]] = None,
        sender_number: Optional[str] = None,
        signal_cli_path: Optional[str] = None,
        signal_cli_data_dir: Optional[str] = None,
        command_timeout_seconds: Optional[int] = None,
        receive_timeout_seconds: Optional[int] = None,
    ):
        self._default_recipients = recipients or []
        self._sender_number = sender_number or os.getenv("SIGNAL_SENDER_NUMBER", "")
        self._signal_cli_path = signal_cli_path or os.getenv("SIGNAL_CLI_PATH", "signal-cli")
        self._signal_cli_data_dir = Path(signal_cli_data_dir or os.getenv("SIGNAL_CLI_DATA_DIR", "/signal-cli-config"))
        self._command_timeout_seconds = command_timeout_seconds or SIGNAL_CLI_TIMEOUT_SECONDS
        self._receive_timeout_seconds = receive_timeout_seconds or SIGNAL_RECEIVE_TIMEOUT_SECONDS
        logger.info(
            "Initialized SignalAPI for sender %s via %s using data dir %s",
            self._sender_number,
            self._signal_cli_path,
            self._signal_cli_data_dir,
        )

    def _base_command(self) -> list[str]:
        return [
            self._signal_cli_path,
            "--data-dir",
            str(self._signal_cli_data_dir),
            "--output",
            "json",
            "--account",
            self._sender_number,
        ]

    def _run(self, args: list[str], stdin: Optional[str] = None) -> _CommandResult:
        command = self._base_command() + args
        log_command = " ".join(command[:7] + ["...", *args])
        logger.debug("Running %s", log_command)

        try:
            with _locked_signal_data_dir(self._signal_cli_data_dir):
                # libsignal extracts native libraries at process startup. Give
                # every short-lived signal-cli process a private directory so
                # those files are removed as soon as the command exits.
                with tempfile.TemporaryDirectory(prefix="signal-cli-") as temp_dir:
                    env = os.environ.copy()
                    env["TMPDIR"] = temp_dir
                    completed = subprocess.run(
                        command,
                        input=stdin,
                        text=True,
                        capture_output=True,
                        timeout=self._command_timeout_seconds,
                        check=False,
                        env=env,
                    )
        except subprocess.TimeoutExpired as exc:
            logger.exception("signal-cli timed out while running %s", args[0] if args else "command")
            return _CommandResult(
                returncode=124,
                stdout=exc.stdout or "",
                stderr=exc.stderr or f"signal-cli timed out after {self._command_timeout_seconds} seconds",
            )
        except FileNotFoundError:
            logger.exception("signal-cli executable was not found")
            return _CommandResult(returncode=127, stderr=f"signal-cli executable not found: {self._signal_cli_path}")
        except OSError as exc:
            logger.exception("signal-cli failed to start")
            return _CommandResult(returncode=1, stderr=str(exc))

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        data = _parse_json_output(stdout)
        if completed.returncode != 0:
            logger.error(
                "signal-cli failed: returncode=%s stderr=%s",
                completed.returncode,
                _summarize_command_error(stderr.strip()),
            )

        return _CommandResult(
            returncode=completed.returncode,
            data=data,
            stdout=stdout,
            stderr=stderr,
        )

    def _result_from_command(self, result: _CommandResult) -> SignalAPIResult:
        if result.returncode == 0:
            return SignalAPIResult(ok=True, status_code=0, data=result.data)

        error = result.stderr.strip() or result.stdout.strip() or f"signal-cli exited with code {result.returncode}"
        error = _summarize_command_error(error)
        return SignalAPIResult(ok=False, status_code=result.returncode, data=result.data, error=error)

    def _send_to_recipient(self, recipient: str, message: str) -> SignalAPIResult:
        group_id = _group_id_for_cli(recipient)
        if group_id:
            args = ["send", "--message-from-stdin", "--group-id", group_id]
        else:
            args = ["send", "--message-from-stdin", recipient]

        result = self._run(args, stdin=message)
        return self._result_from_command(result)

    def send_message(self, recipients, message=None):
        if message is None:
            message = recipients
            recipients = self._default_recipients

        if not self._sender_number:
            return SignalAPIResult(ok=False, error="SIGNAL_SENDER_NUMBER is not configured")

        if not recipients:
            return SignalAPIResult(ok=False, error="At least one recipient is required")

        logger.info("Sending Signal message to %d recipient(s)", len(recipients))
        results = []
        for recipient in recipients:
            result = self._send_to_recipient(recipient, message)
            results.append({"recipient": recipient, "ok": result.ok, "status_code": result.status_code, "data": result.data})
            if not result.ok:
                return SignalAPIResult(ok=False, status_code=result.status_code, data=results, error=result.error)

        logger.info("Signal message sent successfully to %d recipient(s)", len(recipients))
        return SignalAPIResult(ok=True, status_code=0, data=results)

    def _normalize_group(self, group):
        if isinstance(group, str):
            return _group_id_for_app(group)

        if not isinstance(group, dict):
            return group

        normalized = dict(group)
        group_id = (
            normalized.get("id")
            or normalized.get("groupId")
            or normalized.get("group_id")
            or normalized.get("internal_id")
            or normalized.get("group")
        )
        if group_id:
            normalized["id"] = _group_id_for_app(str(group_id))
        return normalized

    def list_groups(self):
        if not self._sender_number:
            return SignalAPIResult(ok=False, error="SIGNAL_SENDER_NUMBER is not configured")

        logger.info("Fetching Signal groups")
        result = self._run(["listGroups"])
        api_result = self._result_from_command(result)
        if not api_result.ok:
            return api_result

        if not isinstance(api_result.data, list):
            return SignalAPIResult(
                ok=False,
                status_code=api_result.status_code,
                data=api_result.data,
                error="signal-cli returned an unexpected groups payload",
            )

        return SignalAPIResult(
            ok=True,
            status_code=api_result.status_code,
            data=[self._normalize_group(group) for group in api_result.data],
        )

    def trust_number(self, number: str):
        if not self._sender_number:
            return SignalAPIResult(ok=False, error="SIGNAL_SENDER_NUMBER is not configured")

        logger.info("Trusting Signal identity for number %s", number)
        result = self._run(["trust", "--trust-all-known-keys", number])
        return self._result_from_command(result)

    def get_identities(self):
        if not self._sender_number:
            return SignalAPIResult(ok=False, error="SIGNAL_SENDER_NUMBER is not configured")

        logger.info("Fetching Signal identities")
        result = self._run(["listIdentities"])
        return self._result_from_command(result)

    def receive_updates(self):
        if not self._sender_number:
            return SignalAPIResult(ok=False, error="SIGNAL_SENDER_NUMBER is not configured")

        logger.info("Receiving Signal updates")
        # This bot only consumes message text. Without --ignore-attachments,
        # signal-cli stores every received media file in its persistent data
        # directory indefinitely.
        result = self._run(
            ["receive", "--timeout", str(self._receive_timeout_seconds), "--ignore-attachments"]
        )
        return self._result_from_command(result)

    def _identity_number(self, identity):
        if not isinstance(identity, dict):
            return None
        return (
            identity.get("number")
            or identity.get("recipient")
            or identity.get("address")
            or identity.get("name")
        )

    def _identity_is_untrusted(self, identity):
        if not isinstance(identity, dict):
            return False
        status = identity.get("status") or identity.get("trustLevel") or identity.get("trust_level")
        return "UNTRUSTED" in str(status or "").upper()

    def trust_all(self):
        self.receive_updates()
        response = self.get_identities()
        if not response.ok or not isinstance(response.data, list):
            return response

        for number_details in response.data:
            if self._identity_is_untrusted(number_details):
                number = self._identity_number(number_details)
                if number:
                    self.trust_number(str(number))

        self.receive_updates()
        logger.info("Finished trusting all known untrusted Signal identities")
        return SignalAPIResult(ok=True, data=response.data)
