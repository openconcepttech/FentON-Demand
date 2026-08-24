#!/bin/bash
# Restrict published Docker ports to the LAN.
#
# Published container ports are DNAT'd and traverse FORWARD -> DOCKER-USER, NOT
# INPUT, so INPUT/ufw rules do not filter them. DOCKER-USER is the documented
# hook Docker leaves for exactly this and it survives container restarts.
#
# Binding the port to the LAN IP in compose would NOT help: a router port-forward
# rewrites the destination to that same LAN IP, so it would still be accepted.
# Filtering on SOURCE address is what actually blocks WAN traffic.
#
# Usage: lockdown-ports.sh [port ...]     (default: 8080)
set -e

PORTS="${*:-8080}"
LAN="192.168.1.0/24"
DOCKER_NETS="172.16.0.0/12"

iptables -N DOCKER-USER 2>/dev/null || true

for P in $PORTS; do
  # remove any previous copies so re-running is idempotent
  while iptables -D DOCKER-USER -p tcp --dport "$P" -s "$LAN" -j RETURN 2>/dev/null; do :; done
  while iptables -D DOCKER-USER -p tcp --dport "$P" -s "$DOCKER_NETS" -j RETURN 2>/dev/null; do :; done
  while iptables -D DOCKER-USER -p tcp --dport "$P" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN 2>/dev/null; do :; done
  while iptables -D DOCKER-USER -p tcp --dport "$P" -j DROP 2>/dev/null; do :; done

  # insert in priority order: allows first, then the catch-all drop
  iptables -I DOCKER-USER 1 -p tcp --dport "$P" -j DROP
  iptables -I DOCKER-USER 1 -p tcp --dport "$P" -s "$DOCKER_NETS" -j RETURN
  iptables -I DOCKER-USER 1 -p tcp --dport "$P" -s "$LAN" -j RETURN
  iptables -I DOCKER-USER 1 -p tcp --dport "$P" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
  echo "  port $P: LAN + docker only"
done

echo "--- DOCKER-USER chain now ---"
iptables -L DOCKER-USER -n -v --line-numbers
