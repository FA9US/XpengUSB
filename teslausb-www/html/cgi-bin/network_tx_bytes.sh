#!/bin/bash

# Function to get the total tx_bytes for ethernet and wifi devices
get_tx_bytes() {
    local total=0
    for dev in /sys/class/net/eth* /sys/class/net/en* /sys/class/net/wl*; do
        if [ -d "$dev" ] && [ -r "$dev/statistics/tx_bytes" ]; then
            val=$(cat "$dev/statistics/tx_bytes")
            total=$((total + val))
        fi
    done
    echo $total
}

tx_bytes=$(get_tx_bytes)
sample_ms=$(date +%s%3N)

cat << EOF
HTTP/1.0 200 OK
Content-type: text/plain

${sample_ms} ${tx_bytes}
EOF

