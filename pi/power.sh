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
#   sudo tapbox-power hat-audio-on   enable I2S audio for the HAT speaker
#                                    (hifiberry-dac overlay; reboot needed)
#   sudo tapbox-power hat-audio-off  remove the overlay again
#   sudo tapbox-power curve     apply the calibrated TapBox battery curve
#                               (percent = remaining playtime; see pi/sugar-config.txt)
#   sudo tapbox-power btsnoop-on   btmon RAM-ring capture (hci0 crash evidence);
#                                  copy /run/tapbox-btsnoop off BEFORE rebooting
#   sudo tapbox-power btsnoop-off  stop the capture
#
# Bluetooth is deliberately left alone — it drives the speaker.
# If Spotify playback stutters in save mode, set WIFI_POWERSAVE=0 below:
# Wi-Fi power save trades latency for power and is the usual suspect.

set -euo pipefail

WIFI_POWERSAVE=1
# Tailscale's keepalives (DERP pings, STUN, netmap polls) wake the wifi
# radio around the clock and defeat its power-save naps. Set to 1 to stop
# tailscaled in save mode (loses remote ssh until 'normal' or reboot).
STOP_TAILSCALE=0

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

pisugar_cmd() {  # send a raw pisugar-server command, e.g. rtc_rtc2pi
  (echo "$1"; sleep 0.3) | nc -q1 127.0.0.1 8423 2>/dev/null
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
    # Core parking is NOT possible at runtime here: the Pi kernel has no CPU
    # hotplug, so writing cpuN/online is a no-op (this used to call a dead
    # set_cores). The real 2-core lever is maxcpus=2 in cmdline.txt + reboot.
    if [[ ! -e /sys/devices/system/cpu/cpu2/online ]]; then
      echo "note: 4 cores stay online (no runtime hotplug); for 2-core"
      echo "      operation add maxcpus=2 to cmdline.txt and reboot"
    fi
    set_governor powersave
    set_leds none 0
    vcgencmd display_power 0 >/dev/null 2>&1 || true
    if [[ $WIFI_POWERSAVE -eq 1 ]]; then
      iw dev wlan0 set power_save on 2>/dev/null || true
    fi
    if [[ $STOP_TAILSCALE -eq 1 ]]; then
      systemctl stop tailscaled 2>/dev/null || true
      echo "tailscaled stopped (STOP_TAILSCALE=1) — no remote ssh until 'normal'"
    fi
    # At boot (no tty) skip the status report: its PiSugar reads block on a
    # still-starting pisugar-server (~5s) and this unit runs After=multi-user,
    # so the report needlessly stretched 'Startup finished' (and threw the
    # broken-pipe noise). Interactive `tapbox-power save` still prints it.
    if [ -t 1 ]; then
      echo "Power save ON — resulting state:"
      status_report
    fi
    ;;
  perf)
    systemctl start tailscaled 2>/dev/null || true
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
# pisugar-server's socket (:8423) isn't up the instant we boot; order
# after it so the first poll has something to talk to
After=pisugar-server.service
Wants=pisugar-server.service

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
    # install.sh installs+enables tapbox-idle by default now; this stays
    # as a convenience alias (the PWA setting is the real knob)
    systemctl enable --now tapbox-idle.service
    echo "tapbox-idle enabled — timeout follows the PWA setting (0 = never)."
    ;;
  idle-off)
    # prefer setting 'Auto-off when idle: never' in the PWA — this stops
    # the daemon outright (install.sh re-enables it on the next run)
    systemctl disable --now tapbox-idle.service 2>/dev/null || true
    echo "tapbox-idle stopped. Tip: the PWA setting 'never' is permanent."
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
  hat-audio-on|hat-audio-off)
    # I2S audio for the Pirate Audio / Amp SHIM speaker (MAX98357A).
    # Adds/removes dtoverlay=hifiberry-dac; takes effect after a reboot.
    boot=/boot/firmware/config.txt
    [[ -f $boot ]] || boot=/boot/config.txt
    [[ -f $boot ]] || { echo "config.txt not found" >&2; exit 1; }
    if [[ $1 == hat-audio-on ]]; then
      if grep -q '^dtoverlay=hifiberry-dac' "$boot"; then
        echo "hifiberry-dac overlay already enabled in $boot"
      else
        # gpio=25=op,dh: the Pirate Audio amp has an enable pin on BCM 25
        # that must be driven high, or the DAC stays muted (silence).
        printf '\n# TapBox HAT speaker (MAX98357A I2S)\ndtoverlay=hifiberry-dac\ngpio=25=op,dh\n' >> "$boot"
        echo "Enabled hifiberry-dac + amp-enable (BCM25) in $boot — reboot to activate."
        echo "After the reboot: pick 'Built-in' in the PWA (or POST /output)."
      fi
      # older runs of this command lacked the amp-enable line
      if ! grep -q '^gpio=25=op,dh' "$boot"; then
        sed -i '/^dtoverlay=hifiberry-dac/a gpio=25=op,dh' "$boot"
        echo "Added the missing amp-enable line (gpio=25=op,dh) — reboot to apply."
      fi
    else
      sed -i '/^# TapBox HAT speaker/d; /^dtoverlay=hifiberry-dac/d; /^gpio=25=op,dh/d' "$boot"
      echo "Disabled the HAT audio overlay — reboot to apply."
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
  rtc-load)  # boot: set the system clock from the PiSugar's battery-backed
             # RTC, so an OFFLINE boot has a sane time (the Zero has no RTC;
             # NTP later corrects it and rtc-save writes it back)
    command -v nc >/dev/null || { echo "nc missing"; exit 0; }
    for _ in $(seq 1 15); do  # wait for pisugar-server to answer
      [[ -n "$(pisugar_get battery)" ]] && break; sleep 1
    done
    before="$(date '+%F %T')"
    pisugar_cmd rtc_rtc2pi >/dev/null || true
    echo "RTC -> system clock (was $before, now $(date '+%F %T'))"
    ;;
  rtc-save)  # write the current time back to the RTC — but ONLY when the
             # clock is NTP-synced, so we never persist a wrong time
    command -v nc >/dev/null || exit 0
    if timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -q yes; then
      pisugar_cmd rtc_pi2rtc >/dev/null || true
      echo "system clock -> RTC ($(date '+%F %T'))"
    else
      echo "clock not NTP-synced yet — RTC left untouched"
    fi
    ;;
  _logloop)  # internal: run by tapbox-batlog.service
    [[ -f $LOG_FILE ]] || echo "time,volt,amp,percent,plugged" > "$LOG_FILE"
    # one connection for all four values — pisugar-server logs every
    # connect (2x INFO) and close (WARN), so per-value nc calls put
    # 12 journal lines in every 60s tick
    val() { awk -v k="$1:" '$1==k{print $2; exit}' <<<"$vals"; }
    while true; do
      # Tolerate a not-yet-ready / hiccuping pisugar-server: a failed poll
      # must SKIP this tick, never let 'set -e' kill the logger. Before
      # this, the boot-time socket race exited _logloop and RestartSec=10
      # landed on the boot critical path (systemd-analyze 2026-07-18).
      if vals="$( (for p in battery_v battery_i battery battery_power_plugged; do
                     # 2>/dev/null: when nc dies early (pisugar-server not
                     # up yet) the echo takes a SIGPIPE and spams 'Broken
                     # pipe' into the journal on every boot
                     echo "get $p" 2>/dev/null; sleep 0.2
                   done) | nc -q1 127.0.0.1 8423 2>/dev/null )" && [[ -n $vals ]]; then
        echo "$(date +'%F %T'),$(val battery_v),$(val battery_i),$(val battery),$(val battery_power_plugged)" >> "$LOG_FILE"
      fi
      sleep 60
    done
    ;;
  _followloop)  # internal: run by tapbox-chargefollow.service
    # Charger-follow (owner 2026-07-29): the 600MHz powersave park
    # exists FOR the battery — on wall power it is pure sluggishness
    # (slow menus, glacial syncs/installs). Follow the plug: ondemand
    # on charger, powersave on battery. STANDALONE on purpose: plugged
    # state comes straight from pisugar-server, so this must never
    # depend on the opt-in battery logger. Guards: act only on a
    # definite reading (a hiccuping pisugar-server skips the tick),
    # only on real transitions (quiet journal), and never while a
    # tapbox-extra runs — its wrapper owns the governor then. Note:
    # 'tapbox-power perf' on BATTERY is re-parked within a tick by
    # design; plug in for sustained performance.
    while true; do
      plugged="$(pisugar_get battery_power_plugged || true)"
      if [[ "$plugged" == true || "$plugged" == false ]] \
          && ! systemctl is-active --quiet tapbox-extra 2>/dev/null; then
        want=powersave
        [[ "$plugged" == true ]] && want=ondemand
        cur="$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor \
               2>/dev/null || true)"
        if [[ -n "$cur" && "$cur" != "$want" ]]; then
          set_governor "$want" || true
          echo "charger-follow: governor -> $want (plugged=$plugged)"
        fi
      fi
      sleep 60
    done
    ;;
  btsnoop-on)
    # RAM-ring btmon capture (tapbox-btsnoop.service, installed disabled
    # by install.sh) — the layer-attribution evidence for hci0 crashes
    if [[ ! -e /etc/systemd/system/tapbox-btsnoop.service ]]; then
      echo "tapbox-btsnoop.service missing — run pi/install.sh first"
      exit 1
    fi
    systemctl enable --now tapbox-btsnoop
    echo "btmon capture ON — ring segments in /run/tapbox-btsnoop (RAM!)"
    echo "NOTE: copy segments OFF the box before rebooting — a reboot"
    echo "      (the instinctive crash response) evaporates the evidence"
    ;;
  btsnoop-off)
    systemctl disable --now tapbox-btsnoop 2>/dev/null || true
    echo "btmon capture OFF (any saved segments in /run are kept until reboot)"
    ;;
  *)
    sed -n '4,24p' "$0"
    exit 1
    ;;
esac
