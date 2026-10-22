#!/bin/sh
# Runs when the web container starts (the nginx image runs every script of /docker-entrypoint.d).
#
# 1. Writes the security headers that nginx includes in every response (default.conf), among them
#    the Content-Security-Policy. connect-src allows this origin and the origin of
#    VITE_KEEPER_BASE_URL. Environment:
#      LOREBASE_WEB_CSP              the whole policy, replacing the one built here
#      LOREBASE_WEB_CSP_CONNECT_SRC  further connect-src sources, space separated
#      LOREBASE_WEB_HSTS             Strict-Transport-Security value, e.g. "max-age=31536000"; empty: off
#                                    (TLS is normally terminated before this container)
#      LOREBASE_WEB_HEADERS_FILE     where the headers are written
# 2. Writes /usr/share/nginx/html/config.js from the VITE_* variables of the environment, for
#    example VITE_API_MODE=live and VITE_KEEPER_BASE_URL=https://keeper.example.com. Without such
#    variables, or when config.js is mounted read-only (the Helm chart does that), it leaves it.
set -eu

headers_file="${LOREBASE_WEB_HEADERS_FILE:-/etc/nginx/conf.d/lorebase-security-headers.inc}"
target="${LOREBASE_WEB_CONFIG_FILE:-/usr/share/nginx/html/config.js}"

# The values end up inside double quotes of an nginx directive.
check_value() {
  case "$2" in
    *'"'* | *'$'* | *'\'* | *'{'* | *'}'*)
      echo "lorebase-web: $1 contains a character that is not allowed (double quote, \$, backslash, brace)" >&2
      exit 1
      ;;
  esac
  if [ "$(printf '%s' "$2" | wc -l)" -gt 0 ]; then
    echo "lorebase-web: $1 must be a single line" >&2
    exit 1
  fi
}

# scheme://host[:port] of an absolute URL; nothing for a relative one.
origin_of() {
  printf '%s' "$1" | sed -n 's|^\(https\{0,1\}://[A-Za-z0-9._:-]*\)\([/?#].*\)\{0,1\}$|\1|p'
}

write_headers() {
  csp="${LOREBASE_WEB_CSP:-}"
  if [ -z "$csp" ]; then
    connect="'self'"
    keeper_origin="$(origin_of "${VITE_KEEPER_BASE_URL:-}")"
    if [ -n "$keeper_origin" ]; then
      connect="$connect $keeper_origin"
    fi
    if [ -n "${LOREBASE_WEB_CSP_CONNECT_SRC:-}" ]; then
      connect="$connect $LOREBASE_WEB_CSP_CONNECT_SRC"
    fi
    csp="default-src 'self'; script-src 'self'; worker-src 'self' blob:; connect-src $connect;"
    csp="$csp style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self';"
    csp="$csp object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
  fi
  check_value LOREBASE_WEB_CSP "$csp"
  check_value LOREBASE_WEB_HSTS "${LOREBASE_WEB_HSTS:-}"
  {
    printf 'add_header Content-Security-Policy "%s" always;\n' "$csp"
    printf 'add_header X-Content-Type-Options "nosniff" always;\n'
    printf 'add_header X-Frame-Options "DENY" always;\n'
    printf 'add_header Referrer-Policy "no-referrer" always;\n'
    if [ -n "${LOREBASE_WEB_HSTS:-}" ]; then
      printf 'add_header Strict-Transport-Security "%s" always;\n' "$LOREBASE_WEB_HSTS"
    fi
  } >"$headers_file"
  echo "lorebase-web: wrote $headers_file"
}

write_headers

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
