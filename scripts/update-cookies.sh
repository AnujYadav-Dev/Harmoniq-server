#!/usr/bin/env bash
# Replaces the server's YouTube cookies with the file on stdin, if YouTube accepts it (docs/deployment-vps.md §11.4).
# Install as /opt/harmoniq/update-cookies.sh, owned by root. It's the forced command of the CI cookies key.
set -euo pipefail
umask 077
cd /opt/harmoniq
exec 9>/opt/harmoniq/.deploy.lock
flock -w 600 9

MAX_BYTES=65536
LIVE=secrets/cookies.txt
NEW=secrets/.cookies.new
TEST=secrets/.cookies.test
trap 'rm -f "$NEW" "$TEST"' EXIT
fail() { echo "Rejected: $*. The current cookies were not changed."; exit 1; }

head -c $((MAX_BYTES + 1)) | tr -d '\r' > "$NEW"
[ -s "$NEW" ] || fail "the file is empty"
[ "$(wc -c < "$NEW")" -le "$MAX_BYTES" ] || fail "the file is too big for a cookies file"
head -n 1 "$NEW" | grep -q 'HTTP Cookie File' || fail "this isn't a Netscape-format cookies file"

# Only tab-separated cookie lines for YouTube and Google. Never print the lines: they hold login values.
if ! awk -F'\t' '/^$/ || (/^#/ && !/^#HttpOnly_/) { next }
  { sub(/^#HttpOnly_/, ""); if (NF != 7 || $1 !~ /(^|\.)(youtube|google)\.com$/) bad = 1; found = 1 }
  END { exit (bad || !found) }' "$NEW"; then
  fail "it has no cookies, broken lines, or cookies from other websites"
fi

id=$(docker compose ps -q server)
[ -n "$id" ] || fail "the server isn't running, so the cookies can't be tested"
echo "Testing the new cookies with YouTube..."
cp "$NEW" "$TEST"
docker exec "$id" python -m app.cookie_check "/run/secrets/$(basename "$TEST")" || fail "YouTube didn't accept them"

# The server stops first: a job finishing during the swap would overwrite the new file with its own copy.
echo "Installing them and restarting the server..."
docker compose stop server
[ -f "$LIVE" ] && cp -p "$LIVE" "$LIVE.prev"
mv "$NEW" "$LIVE"
TAG=$(cat .tag) docker compose up -d --no-deps server

for _ in $(seq 1 40); do
  if [ "$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q server)")" = healthy ]; then
    echo "Done. The server is healthy and using the new cookies."
    exit 0
  fi
  sleep 3
done
echo "The server didn't become healthy after the update. Check its logs."
exit 1
