#!/usr/bin/env bash
# Install the read-only SSH entry point for ops-agent on one node.
#
#   FROM=10.42.0.0/16,192.168.1.0/24 ADMIN_KEY=~/.ssh/admin_key \
#     deploy/node/install.sh admin@node1 secrets/id_ed25519.pub
#
#   FROM       where the agent connects from: k3s pod CIDR (local node) + LAN (pod traffic to another
#              node is SNATed to the node's LAN IP). Required.
#   ADMIN_KEY  your SSH key for a sudo-capable user on the node (default ~/.ssh/id_ed25519).
#
# What it sets up on the node (idempotent, safe to re-run after changing node_policy.py):
#   - system user `ops-agent`: no password, no sudo except the three docker read commands below,
#     member of systemd-journal only (to read the journal). NOT in the docker group (root-equivalent).
#   - /usr/local/bin/ops-agent-ro = ops_agent/tools/node_policy.py, the allowlist of commands.
#   - authorized_keys owned by root with
#       restrict                       no pty, no port/agent/X11 forwarding, no ~/.ssh/rc
#       from="…"                       pod network and LAN only
#       command="…/ops-agent-ro"       whatever the client asks, only the allowlist runs
#   - /etc/sudoers.d/ops-agent, checked with visudo before it is installed.
#
# Remove: sudo userdel -r ops-agent; sudo rm /usr/local/bin/ops-agent-ro /etc/sudoers.d/ops-agent
set -euo pipefail

TARGET=${1:?usage: install.sh user@host path/to/key.pub}
PUBKEY_FILE=${2:?usage: install.sh user@host path/to/key.pub}
FROM=${FROM:?set FROM to the pod CIDR + LAN the agent connects from, e.g. 10.42.0.0/16,192.168.1.0/24}
ADMIN_KEY=${ADMIN_KEY:-$HOME/.ssh/id_ed25519}

HERE=$(cd "$(dirname "$0")" && pwd)
POLICY="$HERE/../../ops_agent/tools/node_policy.py"
PUBKEY=$(cat "$PUBKEY_FILE")
[[ $PUBKEY == ssh-ed25519\ * ]] || { echo "not an ed25519 public key: $PUBKEY_FILE" >&2; exit 1; }

ssh -i "$ADMIN_KEY" "$TARGET" "cat > /tmp/ops-agent-ro" < "$POLICY"

# The sudoers lines must match the argv node_policy builds, so take the formats from it.
PS_FORMAT=$(cd "$HERE/../../ops_agent/tools" && python3 -c 'import node_policy as p; print(p.DOCKER_PS_FORMAT)')
STATS_FORMAT=$(cd "$HERE/../../ops_agent/tools" && python3 -c 'import node_policy as p; print(p.DOCKER_STATS_FORMAT)')

ssh -i "$ADMIN_KEY" "$TARGET" sudo bash -s -- "$FROM" "'$PUBKEY'" "'$PS_FORMAT'" "'$STATS_FORMAT'" <<'REMOTE'
set -euo pipefail
FROM=$1
PUBKEY=$2
PS_FORMAT=$3
STATS_FORMAT=$4

if ! id ops-agent >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/ops-agent --shell /bin/bash ops-agent
fi
passwd -l ops-agent >/dev/null
usermod -G systemd-journal ops-agent          # replaces supplementary groups: journal only

install -o root -g root -m 0755 /tmp/ops-agent-ro /usr/local/bin/ops-agent-ro
rm -f /tmp/ops-agent-ro
python3 -c 'import ast,sys; ast.parse(open("/usr/local/bin/ops-agent-ro").read())'

# Home and keys owned by root: the user can't add keys or drop a ~/.ssh/rc.
install -d -o root -g root -m 0755 /var/lib/ops-agent /var/lib/ops-agent/.ssh
printf 'restrict,from="%s",command="/usr/local/bin/ops-agent-ro" %s\n' "$FROM" "$PUBKEY" \
  > /var/lib/ops-agent/.ssh/authorized_keys
chown root:root /var/lib/ops-agent/.ssh/authorized_keys
chmod 0644 /var/lib/ops-agent/.ssh/authorized_keys

DOCKER=$(command -v docker || true)
if [[ -n $DOCKER ]]; then
  tmp=$(mktemp)
  cat > "$tmp" <<EOF
# ops-agent: read-only docker commands only (see ops_agent/tools/node_policy.py)
ops-agent ALL=(root) NOPASSWD: $DOCKER ps -a --format $PS_FORMAT, \\
    $DOCKER stats --no-stream --format $STATS_FORMAT, \\
    $DOCKER logs --tail *
EOF
  visudo -cf "$tmp" >/dev/null
  install -o root -g root -m 0440 "$tmp" /etc/sudoers.d/ops-agent
  rm -f "$tmp"
else
  rm -f /etc/sudoers.d/ops-agent
fi

echo "ops-agent installed on $(hostname): $(id ops-agent)"
REMOTE
