# A few signal-cli command examples for this project.
#
# Run these inside the web container:
# docker compose exec signal-scheduler-web sh

export SIGNAL_CLI_DATA_DIR="${SIGNAL_CLI_DATA_DIR:-/signal-cli-config}"
export ACCOUNT="${SIGNAL_SENDER_NUMBER:-+49123456789}"

# Register and verify an account. If Signal asks for a captcha, add:
# --captcha 'signalcaptcha://...'
signal-cli --data-dir "$SIGNAL_CLI_DATA_DIR" --account "$ACCOUNT" register
signal-cli --data-dir "$SIGNAL_CLI_DATA_DIR" --account "$ACCOUNT" verify "123-456"

# For voice verification, try SMS registration first, wait 60 seconds, then run:
signal-cli --data-dir "$SIGNAL_CLI_DATA_DIR" --account "$ACCOUNT" register --voice

# Send to a phone number.
printf '%s' "Hi, this is a test" \
  | signal-cli --data-dir "$SIGNAL_CLI_DATA_DIR" --account "$ACCOUNT" send --message-from-stdin "+491234567890"

# List groups. The app stores group ids with the historical "group." prefix.
signal-cli --data-dir "$SIGNAL_CLI_DATA_DIR" --output json --account "$ACCOUNT" listGroups

# Send to a group. Use the raw base64 id here, without the app's "group." prefix.
printf '%s' "I am writing to a group" \
  | signal-cli --data-dir "$SIGNAL_CLI_DATA_DIR" --account "$ACCOUNT" send --message-from-stdin --group-id "BASE64_GROUP_ID"

# Receive pending updates. Run this occasionally if you do not send often.
signal-cli --data-dir "$SIGNAL_CLI_DATA_DIR" --output json --account "$ACCOUNT" receive --timeout 5
