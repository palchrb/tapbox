#!/usr/bin/env bash
#
# TapBox power tuning for the Pi Zero 2 W (installed as tapbox-power).
#   sudo tapbox-power save      battery mode: 2 CPU cores off, powersave
#                               governor, ACT LED + HDMI off, Wi-Fi powersave
#   sudo tapbox-power perf      undo everything (back to defaults)
#   sudo tapbox-power status    show current state (+ PiSugar battery if present)
#   sudo tapbox-power boot-on   apply 'save' automatically at every boot
#   sudo tapbox-power boot-off  stop applying at boot
#
# Bluetooth is deliberately left alone — it drives the speaker.
# If Spotify playback stutters in save mode, set WIFI_POWERSAVE=0 below:
# Wi-Fi power save trades latency for power and is the usual suspect.

set -euo pipefail

WIFI_POWERSAVE=1

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo $0 $*" >&2
  exit 1
fi
SELF="$(readlink -f "$0")"

leds() {  # <trigger> <brightness>
  local led
  for led in /sys/class/leds/ACT /sys/class/leds/led0; do
    [[ -d $led ]] || continue
    echo "$1" > "$led/trigger" 2>/dev/null || true
    echo "$2" > "$led/brightness" 2>/dev/null || true
  done
}

governor() {
  local g
  for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    echo "$1" > "$g" 2>/dev/null || true
  done
}

cores() {  # 0 = offline cpu2+cpu3, 1 = online
  local c
  for c in /sys/devices/system/cpu/cpu[23]/online; do
    [[ -f $c ]] && { echo "$1" > "$c" 2>/dev/null || true; }
  done
}

case "${1:-}" in
  save)
    cores 0
    governor powersave
    leds none 0
    vcgencmd display_power 0 >/dev/null 2>&1 || true
    if [[ $WIFI_POWERSAVE -eq 1 ]]; then
      iw dev wlan0 set power_save on 2>/dev/null || true
    fi
    echo "Power save ON: 2 cores offline, powersave governor, LED+HDMI off."
    ;;
  perf)
    cores 1
    governor ondemand
    leds mmc0 1
    vcgencmd display_power 1 >/dev/null 2>&1 || true
    iw dev wlan0 set power_save off 2>/dev/null || true
    echo "Back to defaults: all cores, ondemand governor, LED+HDMI on."
    ;;
  status)
    echo "online CPUs:   $(cat /sys/devices/system/cpu/online)"
    echo "governor:      $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor)"
    echo "arm clock:     $(vcgencmd measure_clock arm 2>/dev/null | cut -d= -f2 || echo n/a) Hz"
    echo "temp:          $(vcgencmd measure_temp 2>/dev/null | cut -d= -f2 || echo n/a)"
    echo "throttled:     $(vcgencmd get_throttled 2>/dev/null | cut -d= -f2 || echo n/a)"
    echo "wifi pwr save: $(iw dev wlan0 get power_save 2>/dev/null | awk '{print $NF}' || echo n/a)"
    if command -v nc >/dev/null; then
      bat="$( (echo 'get battery'; sleep 0.3) | nc -q1 127.0.0.1 8423 2>/dev/null | awk '{print $2}' )"
      [[ -n ${bat:-} ]] && echo "PiSugar batt:  ${bat}%"
    fi
    ;;
  boot-on)
    cat > /etc/systemd/system/tapbox-power.service <<EOF
[Unit]
Description=TapBox power save mode at boot
After=multi-user.target

[Service]
Type=oneshot
ExecStart=$SELF save

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable tapbox-power.service
    echo "Power save will be applied at every boot (tapbox-power.service)."
    ;;
  boot-off)
    systemctl disable tapbox-power.service 2>/dev/null || true
    rm -f /etc/systemd/system/tapbox-power.service
    systemctl daemon-reload
    echo "Boot-time power save disabled."
    ;;
  *)
    sed -n '4,13p' "$0"
    exit 1
    ;;
esac
