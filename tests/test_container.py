from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_resolves_latest_jvm_signal_cli_distribution():
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "ARG SIGNAL_CLI_VERSION=latest" in dockerfile
    assert "releases/latest" in dockerfile
    assert 'SIGNAL_CLI_VERSION="${latest_url##*/v}"' in dockerfile
    assert "signal-cli-${SIGNAL_CLI_VERSION}.tar.gz" in dockerfile
    assert "Linux-native" not in dockerfile
    assert "ENTRYPOINT" in dockerfile


def test_entrypoint_rejects_elf_signal_cli_binary():
    entrypoint = (PROJECT_ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")

    assert "7f454c46" in entrypoint
    assert "GraalVM native signal-cli binary" in entrypoint
