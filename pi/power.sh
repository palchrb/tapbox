#!/usr/bin/env bash
#
# TapBox power tuning for the Pi Zero 2 W (installed as tapbox-power).
#   sudo tapbox-power save      battery mode: 2 CPU cores off, powersave
#                               governor, ACT LED + HDMI off, Wi-Fi powersave
#   sudo tapbox-power perf      undo everything (back to defaults)
#   sudo tapbox-power status    show current state (+ PiSugar battery if present)
#   sudo tapbox-power boot-on   apply 'save' automatically at every boot
#   sudo tapbox-power boot-off  stop applying at boot
#   sudo tapbox-power log-on    log battery voltage/current/percent to CSV
#                               every 60s (for calibrating the battery curve)
#   sudo tapbox-power log-off   stop logging (the CSV file is kept)
#   sudo tapbox-power idle-on [min]  power off after [min] (default 30) with
#                                    no playback; PiSugar button wakes it
#   sudo tapbox-power idle-off  disable auto-shutdown
#   sudo tapbox-power taps-on   PiSugar button: short=play/pause, double=next,
#                               long=previous (via pisugar-server tap shells)
#   sudo tapbox-power taps-off  restore default PiSugar button behaviour
#   sudo tapbox-power curve     apply the calibrated TapBox battery curve
#                               (percent = remaining playtime; see pi/sugar-config.txt)
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
LOG_FILE=/var/log/tapbox-battery.csv

write() {  # write <value> to <file>; skip silently if file is missing
  if [[ -e $2 ]]; then
    echo "$1" > "$2" 2>/dev/null || echo "warn: could not write $1 to $2" >&2
  fi
}

set_cores() {  # 0 = offline cpu2+cpu3, 1 = online
  local c
  for c in 2 3; do
    write "$1" "/sys/devices/system/cpu/cpu$c/online"
  done
}

set_governor() {
  local g
  for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    write "$1" "$g"
  done
}

set_leds() {  # <trigger> <brightness>
  local led
  for led in /sys/class/leds/ACT /sys/class/leds/led0; do
    write "$1" "$led/trigger"
    write "$2" "$led/brightness"
  done
}

pisugar_get() {  # query pisugar-server, e.g. pisugar_get battery_v
  (echo "get $1"; sleep 0.3) | nc -q1 127.0.0.1 8423 2>/dev/null | awk '{print $2}'
}

status_report() {
  echo "online CPUs:   $(cat /sys/devices/system/cpu/online)"
  echo "governor:      $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor)"
  local khz
  khz="$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq 2>/dev/null || echo '')"
  if [[ -n $khz ]]; then
    echo "arm clock:     $((khz / 1000)) MHz"
  else
    echo "arm clock:     n/a"
  fi
  echo "temp:          $(vcgencmd measure_temp 2>/dev/null | cut -d= -f2 || echo n/a)"
  echo "throttled:     $(vcgencmd get_throttled 2>/dev/null | cut -d= -f2 || echo n/a)"
  echo "wifi pwr save: $(iw dev wlan0 get power_save 2>/dev/null | awk '{print $NF}' || echo n/a)"
  if command -v nc >/dev/null; then
    local bat bv bi
    bat="$(pisugar_get battery)" || true
    bv="$(pisugar_get battery_v)" || true
    bi="$(pisugar_get battery_i)" || true
    local plugged
    plugged="$(pisugar_get battery_power_plugged)" || true
    [[ -n ${bat:-} ]] && LC_ALL=C printf 'PiSugar batt:  ~%.0f%%  (estimate — voltage is the truth)\n' "$bat"
    [[ -n ${bv:-} ]]  && LC_ALL=C printf 'PiSugar volt:  %.2f V  (4.1=full, ~3.75=half, <3.5=charge now)\n' "$bv"
    if [[ ${plugged:-} == true ]]; then
      echo "PiSugar amp:   on charger — battery draw not measurable now"
    elif [[ -n ${bi:-} ]] && awk -v x="$bi" 'BEGIN{exit !(x > 0.001)}'; then
      echo "PiSugar amp:   ${bi} A draw"
    else
      echo "PiSugar amp:   n/a — PiSugar 3 has no battery current sensor"
    fi
  fi
  return 0
}

case "${1:-}" in
  save)
    set_cores 0
    if [[ ! -e /sys/devices/system/cpu/cpu2/online ]]; then
      echo "note: this kernel has no CPU hotplug — cores stay online (idle cores"
      echo "      sleep deeply anyway; add maxcpus=2 to cmdline.txt if you must)"
    fi
    set_governor powersave
    set_leds none 0
    vcgencmd display_power 0 >/dev/null 2>&1 || true
    if [[ $WIFI_POWERSAVE -eq 1 ]]; then
      iw dev wlan0 set power_save on 2>/dev/null || true
    fi
    echo "Power save ON — resulting state:"
    status_report
    ;;
  perf)
    set_cores 1
    set_governor ondemand
    set_leds mmc0 1
    vcgencmd display_power 1 >/dev/null 2>&1 || true
    iw dev wlan0 set power_save off 2>/dev/null || true
    echo "Back to defaults — resulting state:"
    status_report
    ;;
  status)
    status_report
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
  log-on)
    cat > /etc/systemd/system/tapbox-batlog.service <<EOF
[Unit]
Description=TapBox battery logger

[Service]
ExecStart=$SELF _logloop
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now tapbox-batlog.service
    echo "Logging to $LOG_FILE every 60s. Stop with: sudo $0 log-off"
    ;;
  log-off)
    systemctl disable --now tapbox-batlog.service 2>/dev/null || true
    rm -f /etc/systemd/system/tapbox-batlog.service
    systemctl daemon-reload
    echo "Battery logging stopped — data kept in $LOG_FILE"
    ;;
  idle-on)
    mins="${2:-30}"
    idle_bin=/usr/local/bin/tapbox-idle
    if [[ ! -x $idle_bin ]]; then
      echo "tapbox-idle not installed — run install.sh first" >&2
      exit 1
    fi
    cat > /etc/systemd/system/tapbox-idle.service <<EOF
[Unit]
Description=TapBox idle auto-shutdown

[Service]
ExecStart=/opt/tapbox/venv/bin/python3 $idle_bin $mins
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now tapbox-idle.service
    echo "Auto-shutdown ON: powers off after ${mins} min without playback."
    echo "Press the PiSugar button to wake it (cold boot ~25-35s)."
    ;;
  idle-off)
    systemctl disable --now tapbox-idle.service 2>/dev/null || true
    rm -f /etc/systemd/system/tapbox-idle.service
    systemctl daemon-reload
    echo "Auto-shutdown disabled."
    ;;
  taps-on|taps-off)
    cfg=/etc/pisugar-server/config.json
    [[ -f $cfg ]] || { echo "pisugar-server config not found at $cfg" >&2; exit 1; }
    on=true; [[ $1 == taps-off ]] && on=false
    python3 - "$cfg" "$on" <<'PY'
import json, sys
cfg, on = sys.argv[1], sys.argv[2] == "true"
c = json.load(open(cfg))
btn = "python3 /usr/local/bin/tapbox-buttons"
c["single_tap_enable"] = on
c["double_tap_enable"] = on
c["long_tap_enable"]   = on
if on:
    c["single_tap_shell"] = f"{btn} playpause"
    c["double_tap_shell"] = f"{btn} next"
    c["long_tap_shell"]   = f"{btn} prev"
json.dump(c, open(cfg, "w"), indent=2)
PY
    systemctl restart pisugar-server
    if [[ $1 == taps-on ]]; then
      echo "PiSugar button: short=play/pause, double=next, long=previous."
    else
      echo "PiSugar button tap actions disabled."
    fi
    ;;
  curve)
    # Apply the calibrated TapBox battery curve (measured 2026-07-05 on a
    # full discharge run: percent = remaining playtime under playback load,
    # 0% = the safe-shutdown point). Source of truth: pi/sugar-config.txt.
    cfg=/etc/pisugar-server/config.json
    [[ -f $cfg ]] || { echo "pisugar-server config not found at $cfg" >&2; exit 1; }
    python3 - "$cfg" <<'PY'
import json, sys
cfg = sys.argv[1]
c = json.load(open(cfg))
c["battery_curve"] = [
    [4.20, 100.0], [4.10, 91.0], [4.00, 79.0], [3.90, 65.0], [3.85, 56.0],
    [3.80, 41.0], [3.75, 18.0], [3.70, 9.0], [3.60, 3.0], [3.50, 0.0],
]
json.dump(c, open(cfg, "w"), indent=2)
PY
    systemctl restart pisugar-server
    echo "Calibrated battery curve applied (percent = remaining playtime)."
    echo "Note: with this curve 5% safe-shutdown fires at ~3.65V (~15 min left)."
    ;;
  _logloop)  # internal: run by tapbox-batlog.service
    [[ -f $LOG_FILE ]] || echo "time,volt,amp,percent,plugged" > "$LOG_FILE"
    while true; do
      echo "$(date +'%F %T'),$(pisugar_get battery_v),$(pisugar_get battery_i),$(pisugar_get battery),$(pisugar_get battery_power_plugged)" >> "$LOG_FILE"
      sleep 60
    done
    ;;
  *)
    sed -n '4,22p' "$0"
    exit 1
    ;;
esac
