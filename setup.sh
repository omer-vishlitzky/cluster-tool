#!/bin/bash
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
    echo "Do not run this script as root. It will use sudo where needed."
    exit 1
fi

USER=$(whoami)
echo "=== cluster-tool one-time setup for $USER ==="

echo "[1/5] Enabling dnsmasq in NetworkManager..."
echo -e "[main]\ndns=dnsmasq" | sudo tee /etc/NetworkManager/conf.d/cluster-tool-dns.conf > /dev/null

echo "[2/5] Making dnsmasq config dir writable by $USER..."
sudo chown "$USER" /etc/NetworkManager/dnsmasq.d

echo "[3/5] Adding polkit rule for NetworkManager reload..."
cat <<RULES | sudo tee /etc/polkit-1/rules.d/50-cluster-tool-nm.rules > /dev/null
polkit.addRule(function(action, subject) {
    if (action.id == "org.freedesktop.NetworkManager.reload" &&
        subject.user == "$USER") {
        return polkit.Result.YES;
    }
});
RULES

echo "[4/5] Stopping stale dnsmasq processes..."
sudo pkill -x dnsmasq 2>/dev/null || true
sleep 1

echo "[5/5] Breaking systemd-resolved symlink and restarting NetworkManager..."
if [ -L /etc/resolv.conf ]; then
    sudo rm /etc/resolv.conf
fi
sudo systemctl restart NetworkManager

echo ""
echo "Verifying..."
if grep -q "127.0.0.1" /etc/resolv.conf 2>/dev/null; then
    echo "  resolv.conf: OK (nameserver 127.0.0.1)"
else
    echo "  resolv.conf: FAILED (expected nameserver 127.0.0.1)"
    exit 1
fi

if nmcli general reload 2>/dev/null; then
    echo "  nmcli reload: OK (no sudo needed)"
else
    echo "  nmcli reload: FAILED (polkit rule not working)"
    exit 1
fi

echo ""
echo "Setup complete. You can now use cluster-tool without sudo."
