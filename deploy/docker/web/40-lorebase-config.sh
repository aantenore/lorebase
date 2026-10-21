#!/bin/sh
# Runs when the web container starts (the nginx image runs every script of /docker-entrypoint.d).
# Writes /usr/share/nginx/html/config.js from the VITE_* variables of the environment, for
# example VITE_API_MODE=live and VITE_KEEPER_BASE_URL=https://keeper.example.com. Without such
# variables, or when config.js is mounted read-only (the Helm chart does that), it changes nothing.
set -eu

target="${LOREBASE_WEB_CONFIG_FILE:-/usr/share/nginx/html/config.js}"
names="$(env | sed -n 's/^\(VITE_[A-Z0-9_]*\)=.*/\1/p' | sort)"

if [ -z "$names" ]; then
  exit 0
fi
if [ -e "$target" ] && [ ! -w "$target" ]; then
  echo "lorebase-web: $target is read-only, leaving it as it is"
  exit 0
fi

# A JSON string body: single line, backslashes and double quotes escaped.
escape() {
  printf '%s' "$1" | tr -d '\n\r' | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

{
  printf 'window.__LOREBASE_CONFIG__ = {'
  separator=''
  for name in $names; do
    eval "value=\${$name}" # the name comes from env and matches VITE_[A-Z0-9_]*
    # shellcheck disable=SC2154 # assigned by the eval above
    printf '%s"%s":"%s"' "$separator" "$name" "$(escape "$value")"
    separator=','
  done
  printf '};\n'
} >"$target"
echo "lorebase-web: wrote $target"
