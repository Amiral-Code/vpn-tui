#!/usr/bin/env bash
# Install vpn-tui to ~/.local/bin (override with PREFIX=/some/prefix).
#   ./install.sh             install
#   ./install.sh uninstall   remove the script (settings in ~/.config/vpn-tui stay)
set -euo pipefail

prefix="${PREFIX:-$HOME/.local}"
target="$prefix/bin/vpn-tui"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ${1:-} == uninstall ]]; then
  rm -f "$target"
  echo "Removed $target"
  exit
fi

command -v python3 >/dev/null || { echo "vpn-tui needs python3" >&2; exit 1; }
command -v nmcli >/dev/null || { echo "vpn-tui needs NetworkManager (nmcli)" >&2; exit 1; }
plugin=
for f in /usr/lib/NetworkManager/VPN/nm-openvpn-service.name /usr/lib64/NetworkManager/VPN/nm-openvpn-service.name \
  /usr/lib/*/NetworkManager/VPN/nm-openvpn-service.name /etc/NetworkManager/VPN/nm-openvpn-service.name; do
  [[ -e $f ]] && plugin=$f
done
if [[ -z $plugin ]]; then
  echo "Note: NetworkManager's OpenVPN plugin doesn't seem to be installed" >&2
  echo "      (networkmanager-openvpn on Arch, network-manager-openvpn on Debian/Ubuntu," >&2
  echo "      NetworkManager-openvpn on Fedora)." >&2
fi

install -Dm755 "$here/vpn-tui" "$target"
echo "Installed $target"

case ":$PATH:" in
*":$prefix/bin:"*) ;;
*) echo "Add $prefix/bin to your PATH to run it as 'vpn-tui'." ;;
esac
