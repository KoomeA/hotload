#!/usr/bin/env bash
# Checks the egress filter's DENY rules with the real squid.conf, on a Docker network that has no route out, so nothing
# can leave the machine while it runs. Usage: deploy/egress/check.sh [squid-image]
#
# What it proves: private, loopback, link-local/metadata, CGNAT, IPv4-mapped and IPv6-literal (NAT64, 6to4) destinations are
# refused with 403, as is any port other than 80/443, and that a public address on 80/443 is NOT refused by policy (it can
# only fail with 503 here, because the network has no way out).
# What it cannot prove: that the filter can reach the real internet from your production network. Check that from a worker
# container on the real stack: `curl -m 5 https://example.com/` must work (see docs/LAUNCH.md).
set -u
IMAGE="${1:-ubuntu/squid:latest}"
HERE="$(cd "$(dirname "$0")" && pwd)"
NET=hl-egress-check
cleanup() { docker rm -f "$NET-squid" "$NET-client" >/dev/null 2>&1; docker network rm "$NET" >/dev/null 2>&1; }
trap cleanup EXIT
cleanup
docker network create --internal "$NET" >/dev/null
docker run -d --name "$NET-squid" --network "$NET" --network-alias egress -v "$HERE/squid.conf:/etc/squid/squid.conf:ro" "$IMAGE" >/dev/null
docker run -d --name "$NET-client" --network "$NET" "${CLIENT_IMAGE:-alpine:3.20}" sleep 300 >/dev/null
for _ in $(seq 1 30); do docker logs "$NET-squid" 2>&1 | grep -q "Accepting HTTP" && break; sleep 1; done
docker logs "$NET-squid" 2>&1 | grep -q "Accepting HTTP" || { echo "FAIL squid did not start:"; docker logs "$NET-squid" 2>&1 | tail -5; exit 2; }

fails=0
status() { docker exec "$NET-client" sh -c "printf '$1' | nc -w 4 egress 3128 | head -1" 2>/dev/null | tr -d '\r' | awk '{print $2}'; }
expect() { # expect <code> <label> <request>
  got="$(status "$3")"
  if [ "$got" = "$1" ]; then echo "ok    $2 -> $got"; else echo "FAIL  $2 -> ${got:-no answer} (wanted $1)"; fails=$((fails + 1)); fi
}
for t in 169.254.169.254:443 10.0.0.1:443 127.0.0.1:443 192.168.1.1:443 172.16.0.1:443 100.64.0.1:443 0.0.0.0:443 \
         "[::1]:443" "[64:ff9b::a00:1]:443" "[2002:a00:1::]:443" "[::ffff:10.0.0.1]:443" 93.184.216.34:22 93.184.216.34:8443; do
  expect 403 "CONNECT $t" "CONNECT $t HTTP/1.1\\r\\nHost: $t\\r\\n\\r\\n"
done
for u in "http://169.254.169.254/latest/meta-data/" "http://10.0.0.1/" "http://localhost/" "http://[::ffff:169.254.169.254]/" "http://93.184.216.34:8080/"; do
  expect 403 "GET $u" "GET $u HTTP/1.1\\r\\nHost: x\\r\\n\\r\\n"
done
expect 503 "CONNECT 93.184.216.34:443 (public: allowed by policy, unreachable here)" "CONNECT 93.184.216.34:443 HTTP/1.1\\r\\nHost: x\\r\\n\\r\\n"
expect 503 "GET http://93.184.216.34/ (public: allowed by policy, unreachable here)" "GET http://93.184.216.34/ HTTP/1.1\\r\\nHost: x\\r\\n\\r\\n"
[ "$fails" -eq 0 ] && echo "all egress checks passed" || { echo "$fails check(s) failed"; exit 1; }
