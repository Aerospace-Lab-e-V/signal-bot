# Signal Scheduler

Web app for scheduling Signal messages to imported group channels through
[`AsamK/signal-cli`](https://github.com/AsamK/signal-cli).

The Signal number must already be registered in `signal-cli`. The web app uses
Keycloak OpenID Connect for admin login, stores linked groups and schedules in
SQLite, and sends messages from a single scheduler process.

## Configuration

Create a `.env` file or export these variables before starting Docker Compose:

```sh
SIGNAL_SENDER_NUMBER=+49123456789
SESSION_SECRET=replace-with-a-long-random-string
OIDC_ISSUER=https://keycloak.example.com/realms/example
OIDC_CLIENT_ID=signal-scheduler
OIDC_CLIENT_SECRET=replace-with-keycloak-client-secret
APP_BASE_URL=http://localhost:8000
```

Optional access restrictions:

```sh
OIDC_ALLOWED_GROUP=/signal-admins
OIDC_ALLOWED_GROUPS=/signal-admins,/signal-operators
OIDC_ALLOWED_ROLE=signal-scheduler-admin
OIDC_DEBUG_CLAIMS=false
APP_TIMEZONE=Europe/Berlin
LOGOUT_REDIRECT_URL=https://keycloak.example.com/realms/example/protocol/openid-connect/logout
WEB_PORT=8000
SIGNAL_CLI_DATA_DIR=/signal-cli-config
SIGNAL_CLI_TIMEOUT_SECONDS=120
SIGNAL_RECEIVE_TIMEOUT_SECONDS=5
SIGNAL_RECEIVE_INTERVAL_SECONDS=300
```

If neither `OIDC_ALLOWED_GROUP`, `OIDC_ALLOWED_GROUPS`, nor `OIDC_ALLOWED_ROLE` is
set, any authenticated Keycloak user for the configured client can access the app.
Group restrictions are matched against the OpenID Connect `groups` claim. Set
`OIDC_DEBUG_CLAIMS=true` temporarily to log the received claim keys, groups, and
roles after a manual login without logging tokens.

Set `LOGOUT_REDIRECT_URL` to send users to a specific page after the app clears
their local session. Leave it unset to redirect back to the app dashboard.

For local development only, you can bypass Keycloak:

```sh
AUTH_BYPASS_FOR_DEVELOPMENT=true
```

When this is enabled, the app uses a synthetic `Development Admin` user and shows
an `auth bypass` badge in the header. Keep it unset or `false` outside local
development.

## Run

```sh
docker compose up --build
```

Open `http://localhost:8000`, sign in with Keycloak, import existing Signal groups,
then create one-off, weekly, or monthly schedules.

The app data is stored in `./app-data/app.db`. Signal account data is stored in
`./signal-cli-config` and mounted directly into the web container at
`/signal-cli-config`. Existing group ids keep the historical `group.` prefix in the
app; direct `signal-cli` commands use the raw base64 group id without that prefix.
The app also runs `signal-cli receive` every `SIGNAL_RECEIVE_INTERVAL_SECONDS`
seconds so sessions, groups, and future bot commands stay fresh. Set the interval
to `0` to disable automatic receives. Automatic receives ignore attachments
because this bot only processes text commands; this prevents received media from
growing `./signal-cli-config/attachments` without a bound.

Container stdout/stderr uses Docker's rotating `local` log driver. The Compose
configuration retains at most five 20 MB log files, rather than allowing the
default `json-file` log to consume all available host storage.

Each `signal-cli` command gets a private temporary directory that is deleted when
the process exits, preventing extracted `libsignal` libraries from accumulating.
Compose also mounts `/tmp` as a 512 MB tmpfs so crash leftovers cannot grow the
container's writable layer without a bound. The mount permits execution because
libsignal loads its extracted native `.so` library directly from that directory.

The old `message_sender.py` script is kept as a legacy reference, but the container
now runs `uvicorn app.main:app`.

## Signal CLI

The Docker image installs the native `signal-cli` release. To run maintenance
commands manually:

```sh
docker compose exec signal-scheduler-web sh
signal-cli --data-dir "$SIGNAL_CLI_DATA_DIR" --account "$SIGNAL_SENDER_NUMBER" receive --timeout 5
signal-cli --data-dir "$SIGNAL_CLI_DATA_DIR" --output json --account "$SIGNAL_SENDER_NUMBER" listGroups
```

By default the Docker build resolves `SIGNAL_CLI_VERSION=latest` to the current
stable GitHub release. To force Docker to re-check the latest release, rebuild
without cache:

```sh
docker compose build --no-cache signal-scheduler-web
```

For reproducible deployments, pin a specific release:

```sh
docker compose build --build-arg SIGNAL_CLI_VERSION=0.14.5 signal-scheduler-web
```

## Tests

```sh
python3 -m venv /tmp/asl-signalbot-venv
/tmp/asl-signalbot-venv/bin/pip install -r requirements-dev.txt
/tmp/asl-signalbot-venv/bin/python -m pytest
```
