#!/bin/bash
# Optional local dashboard installer. The USB gadget remains running.
set -euo pipefail
umask 077

[[ $EUID -eq 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
private_stdin=false
restart_worker=true
for option in "$@"; do
    case "$option" in
        --hotspot-password-stdin) private_stdin=true ;;
        --defer-worker-restart) restart_worker=false ;;
        *) echo 'Unknown dashboard install option.' >&2; exit 1 ;;
    esac
done
cd -- "$(dirname -- "$0")"
destination=/usr/local/lib/xpengusb
web=/etc/xpengusb-web
[[ -f /etc/xpengusb/config.json && -f $destination/configure.py ]] || {
    echo 'Finish the base XpengUSB installation first.' >&2; exit 1;
}
systemctl is-active --quiet NetworkManager.service || {
    echo 'The dashboard requires NetworkManager.' >&2; exit 1;
}
[[ -f /etc/xpengusb/wifi.json || -t 0 || $private_stdin == true ]] || {
    echo 'Run the first installation in a terminal to enter the hotspot password privately.' >&2; exit 1;
}
missing=()
for package in dnsmasq-base iw openssl; do
    if [[ $(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true) != 'install ok installed' ]]; then
        missing+=("$package")
    fi
done
if (( ${#missing[@]} )); then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}"
fi

if ! id xpengusb-web >/dev/null 2>&1; then
    useradd --system --user-group --no-create-home --shell /usr/sbin/nologin xpengusb-web
fi
install -d -m 0750 -o root -g xpengusb-web "$web"
if [[ ! -e $web/password ]]; then
    python3 -c 'import secrets; print(secrets.token_urlsafe(24))' > "$web/password"
fi
if [[ ! -e $web/ha-token ]]; then
    python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$web/ha-token"
fi
if [[ ! -e $web/cert.pem || ! -e $web/key.pem ]]; then
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
        -sha256 -nodes -days 825 -subj '/CN=xpengusb.local' \
        -addext 'subjectAltName=DNS:xpengusb.local,IP:10.42.0.1' \
        -keyout "$web/key.pem" -out "$web/cert.pem"
fi
chown root:xpengusb-web "$web/password" "$web/key.pem" "$web/cert.pem" "$web/ha-token"
chmod 0640 "$web/password" "$web/key.pem" "$web/cert.pem" "$web/ha-token"

for module in dashboard dashboard_control nas_probe notify runtime; do
    install -m 0644 "xpengusb/$module.py" "$destination/xpengusb/$module.py"
done
install -m 0755 configure.py "$destination/configure.py"
(
    cd "$destination"
    if $private_stdin; then
        python3 -m xpengusb.dashboard_control setup --password-stdin
    else
        python3 -m xpengusb.dashboard_control setup
    fi
)
install -m 0644 systemd/xpengusb-dashboard.socket systemd/xpengusb-dashboard@.service \
    systemd/xpengusb-web.service systemd/xpengusb-network.service systemd/xpengusb-network.timer \
    /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now xpengusb-dashboard.socket xpengusb-web.service xpengusb-network.timer
systemctl restart xpengusb-web.service
if $restart_worker; then
    systemctl restart xpengusb.service
else
    echo 'Restart xpengusb.service between archive cycles to enable dashboard recording status.'
fi
printf '\nDashboard: https://xpengusb.local:8443 (hotspot: https://10.42.0.1:8443)\n'
printf 'Login user: admin. Read the unique password privately with sudo cat %s/password\n' "$web"
openssl x509 -in "$web/cert.pem" -noout -fingerprint -sha256
