#!/bin/sh
set -eu

signal_cli_path="$(command -v "${SIGNAL_CLI_PATH:-signal-cli}")"
signal_cli_magic="$(od -An -tx1 -N4 "$signal_cli_path" | tr -d ' \n')"

if [ "$signal_cli_magic" = "7f454c46" ]; then
    echo "Refusing to start: $signal_cli_path is a GraalVM native signal-cli binary; the JVM distribution is required." >&2
    exit 78
fi

echo "Using JVM signal-cli launcher at $signal_cli_path ($("$signal_cli_path" --version))"
exec "$@"
