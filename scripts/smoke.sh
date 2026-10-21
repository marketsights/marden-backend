#!/bin/sh
# Smoke test of a running Marden deployment (Railway, or local containers): curl checks of the API and
# the web app. Read-only: it sends GETs and one CORS preflight, and never prints a token or a cookie.
#
#   scripts/smoke.sh --api https://api.marden.ai --web https://app.marden.ai
#   API_URL=http://127.0.0.1:4610 WEB_URL=http://127.0.0.1:3610 scripts/smoke.sh --expect-auth unconfigured
#
# Options (or the environment variables in brackets):
#   --api URL            the API's public origin [API_URL]; omit to check only the web app
#   --web URL            the web app's origin [WEB_URL]; omit to check only the API
#   --expect-auth MODE   what the web app should be running [EXPECT_AUTH]:
#                          clerk (default; a deployment): pages redirect to /sign-in, /healthz 200
#                          unconfigured: pages say "Sign-in is not configured", /healthz 503
#   --token-env NAME     name of an environment variable holding a Clerk session token (default
#                        SMOKE_TOKEN). With a token, /api/me must answer 200 and /api/stream must open
#                        an event stream. Get one from a signed-in browser tab: in the console,
#                        `await window.Clerk.session.getToken()` (valid for about a minute).
#   --timeout SECONDS    per-request timeout (default 20; a Neon compute waking up takes a few seconds)
#
# Exit status: 0 when every check passed, 1 when any failed, 2 on bad usage.
set -u

API="${API_URL:-}"
WEB="${WEB_URL:-}"
EXPECT_AUTH="${EXPECT_AUTH:-clerk}"
TOKEN_ENV="SMOKE_TOKEN"
TIMEOUT=20

while [ "$#" -gt 0 ]; do
  case "$1" in
    --api) API="$2"; shift 2 ;;
    --web) WEB="$2"; shift 2 ;;
    --expect-auth) EXPECT_AUTH="$2"; shift 2 ;;
    --token-env) TOKEN_ENV="$2"; shift 2 ;;
    --timeout) TIMEOUT="$2"; shift 2 ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "smoke: unknown option $1" >&2; exit 2 ;;
  esac
done
API="${API%/}"
WEB="${WEB%/}"
case "$EXPECT_AUTH" in clerk|unconfigured) ;; *) echo "smoke: --expect-auth must be clerk or unconfigured" >&2; exit 2 ;; esac
if [ -z "$API" ] && [ -z "$WEB" ]; then
  echo "smoke: give --api and/or --web (or API_URL / WEB_URL)" >&2
  exit 2
fi
command -v curl >/dev/null 2>&1 || { echo "smoke: curl is required" >&2; exit 2; }
TOKEN="$(printenv "$TOKEN_ENV" 2>/dev/null || true)"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT INT TERM
FAILED=0
PASSED=0

pass() { PASSED=$((PASSED + 1)); echo "PASS  $1"; }
fail() { FAILED=$((FAILED + 1)); echo "FAIL  $1"; }

# fetch NAME URL [curl args...]: status in $TMP/NAME.status, headers in .h, body in .b (no redirects followed)
fetch() {
  name="$1"; url="$2"; shift 2
  code="$(curl -sS -o "$TMP/$name.b" -D "$TMP/$name.h" -w '%{http_code}' --max-time "$TIMEOUT" "$@" "$url" 2>"$TMP/$name.err")" || {
    # A timeout after the headers arrived (an event stream that was not refused) still has a status.
    code="$(awk 'NR == 1 { print $2 }' "$TMP/$name.h" 2>/dev/null)"
    [ -n "$code" ] || code="000"
  }
  echo "$code" > "$TMP/$name.status"
}
status() { cat "$TMP/$1.status"; }
header() { grep -i "^$2:" "$TMP/$1.h" | tail -1 | cut -d: -f2- | tr -d '\r' | sed 's/^ *//'; }
body_has() { grep -q -- "$2" "$TMP/$1.b"; }
expect_status() { # NAME EXPECTED LABEL
  got="$(status "$1")"
  if [ "$got" = "$2" ]; then pass "$3 ($got)"; else fail "$3: expected $2, got $got$( [ "$got" = 000 ] && printf ' (%s)' "$(head -c 200 "$TMP/$1.err")")"; fi
}
expect_header() { # NAME HEADER PATTERN LABEL
  value="$(header "$1" "$2")"
  if printf '%s' "$value" | grep -qiE -- "$3"; then pass "$4"; else fail "$4: $2 is '${value:-missing}'"; fi
}
auth_header() { [ -n "$TOKEN" ] && printf 'authorization: Bearer %s' "$TOKEN"; }

# The stream must answer 200 with text/event-stream and send something (an event or the heartbeat
# comment) within a few seconds; --max-time ends the open stream, which curl reports as code 28.
check_stream() { # NAME URL LABEL
  curl -sS -N -o "$TMP/$1.b" -D "$TMP/$1.h" --max-time 5 -H "$(auth_header)" -H 'accept: text/event-stream' "$2" 2>/dev/null
  code=$?
  st="$(head -1 "$TMP/$1.h" 2>/dev/null | awk '{print $2}')"
  type="$(header "$1" content-type)"
  if [ "$st" = 200 ] && printf '%s' "$type" | grep -qi 'text/event-stream' && { [ "$code" = 28 ] || [ "$code" = 0 ]; }; then
    pass "$3 (200 text/event-stream, $(wc -c < "$TMP/$1.b" | tr -d ' ') bytes in 5 s)"
  else
    fail "$3: status ${st:-none}, content-type '${type:-missing}', curl exit $code"
  fi
}

# ------------------------------------------------------------------------------------------- API
if [ -n "$API" ]; then
  echo "== API $API"
  fetch api_health "$API/healthz"
  expect_status api_health 200 "GET /healthz"
  if body_has api_health '"db":"ok"'; then pass "/healthz reports the database reachable"; else fail "/healthz body: $(head -c 200 "$TMP/api_health.b")"; fi
  expect_header api_health cache-control 'no-store' "/healthz is not cached"
  expect_header api_health x-content-type-options 'nosniff' "API sends security headers (helmet)"
  case "$API" in https://*) expect_header api_health strict-transport-security 'max-age=' "API sends HSTS" ;; esac

  fetch api_me "$API/api/me"
  expect_status api_me 401 "GET /api/me without a session is refused (dev auth is off)"
  fetch api_sites "$API/api/sites"
  expect_status api_sites 401 "GET /api/sites without a session is refused"
  fetch api_stream "$API/api/stream" -H 'accept: text/event-stream'
  expect_status api_stream 401 "GET /api/stream without a session is refused"
  fetch api_cors "$API/api/me" -X OPTIONS -H 'origin: https://evil.example' -H 'access-control-request-method: GET'
  if grep -qi '^access-control-allow-origin: https://evil.example' "$TMP/api_cors.h"; then
    fail "CORS: a foreign origin was allowed"
  else
    pass "CORS: a foreign origin is not allowed"
  fi
  fetch api_404 "$API/api/no-such-route"
  st="$(status api_404)"
  if [ "$st" = 404 ] || [ "$st" = 401 ]; then pass "unknown route answers $st, not 5xx"; else fail "unknown route answered $st"; fi

  if [ -n "$TOKEN" ]; then
    fetch api_me_auth "$API/api/me" -H "$(auth_header)"
    expect_status api_me_auth 200 "GET /api/me with a session token"
    check_stream api_stream_auth "$API/api/stream" "GET /api/stream with a session token opens a stream"
  else
    echo "SKIP  signed-in checks: no token in \$$TOKEN_ENV"
  fi
fi

# ------------------------------------------------------------------------------------------- web
if [ -n "$WEB" ]; then
  echo "== web $WEB (expecting auth: $EXPECT_AUTH)"
  fetch web_health "$WEB/healthz"
  if [ "$EXPECT_AUTH" = clerk ]; then
    expect_status web_health 200 "GET /healthz"
    if body_has web_health '"auth":"clerk"'; then pass "web runs Clerk sign-in"; else fail "web /healthz body: $(head -c 200 "$TMP/web_health.b")"; fi
  else
    expect_status web_health 503 "GET /healthz fails while sign-in is not configured"
  fi

  fetch web_home "$WEB/"
  if [ "$EXPECT_AUTH" = clerk ]; then
    st="$(status web_home)"
    loc="$(header web_home location)"
    case "$st" in
      30[1278]) if printf '%s' "$loc" | grep -q 'sign-in'; then pass "GET / without a session redirects to sign-in ($st)"; else fail "GET / redirected to '$loc'"; fi ;;
      *) fail "GET / without a session answered $st (expected a redirect to /sign-in)" ;;
    esac
    fetch web_signin "$WEB/sign-in"
    expect_status web_signin 200 "GET /sign-in"
  else
    expect_status web_home 200 "GET / renders the not-configured page"
    if body_has web_home 'Sign-in is not configured'; then pass "pages say \"Sign-in is not configured\" (fail closed)"; else fail "GET / does not say \"Sign-in is not configured\""; fi
    if body_has web_home 'Development sign-in'; then fail "the development sign-in is on"; else pass "no development sign-in"; fi
  fi
  expect_header web_home x-frame-options 'DENY' "web sends security headers"
  case "$WEB" in https://*) expect_header web_home strict-transport-security 'max-age=' "web sends HSTS" ;; esac
  if [ -n "$(header web_home x-powered-by)" ]; then fail "web sends X-Powered-By"; else pass "web hides X-Powered-By"; fi

  # /api/* is rewritten to the API (same origin): the API itself must answer, and refuse.
  fetch web_me "$WEB/api/me"
  expect_status web_me 401 "GET /api/me through the web app reaches the API and is refused"
  if body_has web_me '"error"'; then pass "the refusal is the API's JSON"; else fail "GET /api/me through the web app: body $(head -c 200 "$TMP/web_me.b")"; fi
  fetch web_stream "$WEB/api/stream" -H 'accept: text/event-stream'
  expect_status web_stream 401 "GET /api/stream through the web app without a session is refused"
  if [ -n "$TOKEN" ]; then
    check_stream web_stream_auth "$WEB/api/stream" "GET /api/stream through the web app with a session token opens a stream"
  fi
fi

echo "== $PASSED passed, $FAILED failed"
[ "$FAILED" -eq 0 ]
