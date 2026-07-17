import os
import subprocess

from signal_api import SignalAPI


def make_api(tmp_path):
    return SignalAPI(
        sender_number="+49123",
        signal_cli_path="/usr/local/bin/signal-cli",
        signal_cli_data_dir=str(tmp_path),
        command_timeout_seconds=5,
        receive_timeout_seconds=1,
    )


def test_send_message_success(monkeypatch, tmp_path):
    calls = []

    def fake_run(command, input, text, capture_output, timeout, check, env):
        calls.append(
            {
                "command": command,
                "input": input,
                "text": text,
                "capture_output": capture_output,
                "timeout": timeout,
                "check": check,
                "env": env,
            }
        )
        return subprocess.CompletedProcess(command, 0, stdout='{"timestamp":123}\n', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    api = make_api(tmp_path)

    result = api.send_message(["group.abc"], "Hello")

    assert result.ok is True
    assert result.status_code == 0
    assert result.data[0]["data"] == {"timestamp": 123}
    assert calls[0]["input"] == "Hello"
    assert calls[0]["timeout"] == 5
    assert calls[0]["env"]["TMPDIR"].startswith("/tmp/signal-cli-")
    assert not os.path.exists(calls[0]["env"]["TMPDIR"])
    assert calls[0]["command"] == [
        "/usr/local/bin/signal-cli",
        "--data-dir",
        str(tmp_path),
        "--output",
        "json",
        "--account",
        "+49123",
        "send",
        "--message-from-stdin",
        "--group-id",
        "abc",
    ]


def test_send_message_failure(monkeypatch, tmp_path):
    def fake_run(command, input, text, capture_output, timeout, check, env):
        return subprocess.CompletedProcess(command, 3, stdout="", stderr="server offline")

    monkeypatch.setattr(subprocess, "run", fake_run)
    api = make_api(tmp_path)

    result = api.send_message(["group.abc"], "Hello")

    assert result.ok is False
    assert result.status_code == 3
    assert result.error == "server offline"


def test_send_message_timeout(monkeypatch, tmp_path):
    temp_dirs = []

    def fake_run(command, input, text, capture_output, timeout, check, env):
        temp_dirs.append(env["TMPDIR"])
        assert os.path.isdir(env["TMPDIR"])
        raise subprocess.TimeoutExpired(command, timeout, output="", stderr="too slow")

    monkeypatch.setattr(subprocess, "run", fake_run)
    api = make_api(tmp_path)

    result = api.send_message(["group.abc"], "Hello")

    assert result.ok is False
    assert result.status_code == 124
    assert "too slow" in result.error
    assert not os.path.exists(temp_dirs[0])


def test_list_groups_success(monkeypatch, tmp_path):
    def fake_run(command, input, text, capture_output, timeout, check, env):
        return subprocess.CompletedProcess(command, 0, stdout='[{"id":"abc","name":"Lab"}]\n', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    api = make_api(tmp_path)

    result = api.list_groups()

    assert result.ok is True
    assert result.data[0]["id"] == "group.abc"
    assert result.data[0]["name"] == "Lab"


def test_list_groups_rejects_unexpected_payload(monkeypatch, tmp_path):
    def fake_run(command, input, text, capture_output, timeout, check, env):
        return subprocess.CompletedProcess(command, 0, stdout='{"not":"a list"}\n', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    api = make_api(tmp_path)

    result = api.list_groups()

    assert result.ok is False
    assert "unexpected" in result.error


def test_receive_ignores_attachments(monkeypatch, tmp_path):
    calls = []

    def fake_run(command, input, text, capture_output, timeout, check, env):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="[]\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = make_api(tmp_path).receive_updates()

    assert result.ok is True
    assert calls[0][-4:] == ["receive", "--timeout", "1", "--ignore-attachments"]
