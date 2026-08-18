#!/usr/bin/env bash
#
# Vibb power tuning for the Pi Zero 2 W (installed as vibb-power).
#   sudo vibb-power save      battery mode: 2 CPU cores off, powersave
#                               governor, ACT LED off, Wi-Fi powersave
#   sudo vibb-power perf      undo everything (back to defaults)
#   sudo vibb-power status    show current state (+ PiSugar battery if present)
#   sudo vibb-power boot-on   apply 'save' automatically at every boot
#   sudo vibb-power boot-off  stop applying at boot
#   sudo vibb-power log-on    log battery voltage/current/percent to CSV
#                               every 60s (for calibrating the battery curve)
#   sudo vibb-power log-off   stop logging (the CSV file is kept)
#   sudo vibb-power idle-on [min]  power off after [min] (default 30) with
#                                    no playback; PiSugar button wakes it
#   sudo vibb-power idle-off  disable auto-shutdown
#   sudo vibb-power taps-on   PiSugar button: short=play/pause, double=next,
#                               long=previous (via pisugar-server tap shells)
#   sudo vibb-power taps-off  restore default PiSugar button behaviour
#   sudo vibb-power hat-audio-on   enable I2S audio for the HAT speaker
#                                    (hifiberry-dac overlay; reboot needed)
#   sudo vibb-power hat-audio-off  remove the overlay again
#   sudo vibb-power curve     apply the calibrated Vibb battery curve
#                               (percent = remaining playtime; see pi/sugar-config.txt)
#   sudo vibb-power btsnoop-on   btmon RAM-ring capture (hci0 crash evidence);
#                                  copy /run/vibb-btsnoop off BEFORE rebooting
#   sudo vibb-power btsnoop-off  stop the capture
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
LOG_FILE=/var/log/vibb-battery.csv

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
  wait-ui)
    # Hold the CPU at boot clock until the screen is up. Measured
    # 2026-08-18: the UI's startup is CPU-bound, not only I/O-bound —
    # warm, same page cache, only the clock differing, imports run 0.4s
    # at 900MHz against 0.6s at the 600MHz powersave park, and panel
    # init 0.8s against 1.1s. Parking the CPU at ~6.5s into a boot (when
    # basic.target lands) therefore slowed everything the child waits
    # for, and the first menu presses after boot too.
    #
    # Bounded three ways, because this must never keep a box awake:
    # only waits when the screen service is ENABLED (a headless box
    # returns at once), gives up after WAIT_S regardless, and the
    # marker lives on tmpfs so a crashed UI cannot leave a stale one
    # behind across a reboot.
    WAIT_S="${VIBB_UI_WAIT:-30}"
    if systemctl is-enabled vibb-ui.service >/dev/null 2>&1; then
      for _ in $(seq "$WAIT_S"); do
        [[ -e /run/vibb-ui-ready ]] && break
        sleep 1
      done
    fi
    exit 0
    ;;
  save)
    # Core parking is NOT possible at runtime here: the Pi kernel has no CPU
    # hotplug, so writing cpuN/online is a no-op (this used to call a dead
    # set_cores). The real 2-core lever is maxcpus=2 in cmdline.txt + reboot.
    if [[ ! -e /sys/devices/system/cpu/cpu2/online ]]; then
      echo "note: 4 cores stay online (no runtime hotplug); for 2-core"
      echo "      operation add maxcpus=2 to cmdline.txt and reboot"
    fi
    # The 600MHz park exists FOR the battery. On wall power it is pure
    # sluggishness, so `save` follows the plug exactly like charger-follow
    # does at runtime — otherwise a mains box booted parked and stayed
    # slow for up to a minute until the follower's next tick corrected it
    # (owner 2026-08-18: "på strøm og ved boot skal vi uansett være på
    # ondemand"). Unknown reads as ondemand: no pisugar means no battery,
    # which means wall power.
    plugged="$(pisugar_get battery_power_plugged || true)"
    if [[ "$plugged" == false ]]; then
      set_governor powersave
    else
      set_governor ondemand
      [[ "$plugged" == true ]] \
        && echo "on charger — governor stays ondemand" \
        || echo "no battery reading — assuming wall power, governor ondemand"
    fi
    set_leds none 0
    # NO HDMI blanking. 'vcgencmd display_power 0' is a ONE-WAY door on
    # this box (field 2026-08-04): it blanks, but display_power 1 only
    # flips the firmware flag — no mode is re-negotiated, so the TV
    # stays dark until a reboot. This box has no KMS at all (empty
    # /sys/class/drm, no /dev/dri, no vc4 overlay in config.txt) and
    # tvservice is gone from trixie, so there is no supported way to
    # bring the output back. The saving was a few mA on an output
    # nobody was using; the cost was a TV that could not be woken and a
    # RetroPie session with no picture.
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
    # broken-pipe noise). Interactive `vibb-power save` still prints it.
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
    cat > /etc/systemd/system/vibb-power.service <<EOF
[Unit]
Description=Vibb power save mode at boot
# basic.target, NOT multi-user.target: multi-user waits for
# network-online (go-librespot pulls it in), so a boot where wifi
# struggles left the HDMI signal LIT and the CPU unparked for as long
# as NM-wait-online took — field 2026-08-04: console visible on the TV
# with 'Startup finished in 1min 17s'. Power save must not be a
# hostage of the radio. Runtime wifi power save is the daemon's
# governor anyway, so nothing here needs wlan0 to exist yet.
After=basic.target

[Service]
Type=oneshot
# Hold the boot clock until the screen is up — see the wait-ui action.
# '-' prefixed and bounded inside it, so a headless box or a UI that
# never comes up costs at most VIBB_UI_WAIT seconds, never a hung boot.
ExecStartPre=-$SELF wait-ui
ExecStart=$SELF save

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable vibb-power.service
    echo "Power save will be applied at every boot (vibb-power.service)."
    ;;
  boot-off)
    systemctl disable vibb-power.service 2>/dev/null || true
    rm -f /etc/systemd/system/vibb-power.service
    systemctl daemon-reload
    echo "Boot-time power save disabled."
    ;;
  log-on)
    cat > /etc/systemd/system/vibb-batlog.service <<EOF
[Unit]
Description=Vibb battery logger
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
    systemctl enable --now vibb-batlog.service
    echo "Logging to $LOG_FILE every 60s. Stop with: sudo $0 log-off"
    ;;
  log-off)
    systemctl disable --now vibb-batlog.service 2>/dev/null || true
    rm -f /etc/systemd/system/vibb-batlog.service
    systemctl daemon-reload
    echo "Battery logging stopped — data kept in $LOG_FILE"
    ;;
  idle-on)
    # install.sh installs+enables vibb-idle by default now; this stays
    # as a convenience alias (the PWA setting is the real knob)
    systemctl enable --now vibb-idle.service
    echo "vibb-idle enabled — timeout follows the PWA setting (0 = never)."
    ;;
  idle-off)
    # prefer setting 'Auto-off when idle: never' in the PWA — this stops
    # the daemon outright (install.sh re-enables it on the next run)
    systemctl disable --now vibb-idle.service 2>/dev/null || true
    echo "vibb-idle stopped. Tip: the PWA setting 'never' is permanent."
    ;;
  taps-on|taps-off)
    cfg=/etc/pisugar-server/config.json
    [[ -f $cfg ]] || { echo "pisugar-server config not found at $cfg" >&2; exit 1; }
    on=true; [[ $1 == taps-off ]] && on=false
    python3 - "$cfg" "$on" <<'PY'
import json, sys
cfg, on = sys.argv[1], sys.argv[2] == "true"
c = json.load(open(cfg))
btn = "python3 /usr/local/bin/vibb-buttons"
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
        printf '\n# Vibb HAT speaker (MAX98357A I2S)\ndtoverlay=hifiberry-dac\ngpio=25=op,dh\n' >> "$boot"
        echo "Enabled hifiberry-dac + amp-enable (BCM25) in $boot — reboot to activate."
        echo "After the reboot: pick 'Built-in' in the PWA (or POST /output)."
      fi
      # older runs of this command lacked the amp-enable line
      if ! grep -q '^gpio=25=op,dh' "$boot"; then
        sed -i '/^dtoverlay=hifiberry-dac/a gpio=25=op,dh' "$boot"
        echo "Added the missing amp-enable line (gpio=25=op,dh) — reboot to apply."
      fi
    else
      sed -i '/^# Vibb HAT speaker/d; /^dtoverlay=hifiberry-dac/d; /^gpio=25=op,dh/d' "$boot"
      echo "Disabled the HAT audio overlay — reboot to apply."
    fi
    ;;
  curve)
    # Apply the calibrated Vibb battery curve (measured 2026-07-05 on a
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
    # the clock is now real (not fake-hwclock's "when we last ran"), so
    # cross-reboot age comparisons — the resume session — may trust it
    : > /run/vibb-clock-ok 2>/dev/null || true
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
  _logloop)  # internal: run by vibb-batlog.service
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
  _followloop)  # internal: run by vibb-chargefollow.service
    # Charger-follow (owner 2026-07-29): the 600MHz powersave park
    # exists FOR the battery — on wall power it is pure sluggishness
    # (slow menus, glacial syncs/installs). Follow the plug: ondemand
    # on charger, powersave on battery. STANDALONE on purpose: plugged
    # state comes straight from pisugar-server, so this must never
    # depend on the opt-in battery logger. Guards: act only on a
    # definite reading (a hiccuping pisugar-server skips the tick),
    # only on real transitions (quiet journal), and never while a
    # vibb-extra runs — its wrapper owns the governor then. Note:
    # 'vibb-power perf' on BATTERY is re-parked within a tick by
    # design; plug in for sustained performance.
    while true; do
      plugged="$(pisugar_get battery_power_plugged || true)"
      # The extras guard reads the wrapper's governor-snapshot marker,
      # NOT systemctl: probing a dead transient unit made systemd log
      # 'Failed to open /run/systemd/transient/...' twice per minute
      # forever (field 2026-07-29). The marker exists exactly while an
      # extra owns the governor — which is precisely the question.
      if [[ "$plugged" == true || "$plugged" == false ]] \
          && [[ ! -e "${VIBB_RUN:-/run}/vibb-extra-governor" ]]; then
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
    # RAM-ring btmon capture (vibb-btsnoop.service, installed disabled
    # by install.sh) — the layer-attribution evidence for hci0 crashes
    if [[ ! -e /etc/systemd/system/vibb-btsnoop.service ]]; then
      echo "vibb-btsnoop.service missing — run pi/install.sh first"
      exit 1
    fi
    systemctl enable --now vibb-btsnoop
    echo "btmon capture ON — ring segments in /run/vibb-btsnoop (RAM!)"
    echo "NOTE: copy segments OFF the box before rebooting — a reboot"
    echo "      (the instinctive crash response) evaporates the evidence"
    ;;
  btsnoop-off)
    systemctl disable --now vibb-btsnoop 2>/dev/null || true
    echo "btmon capture OFF (any saved segments in /run are kept until reboot)"
    ;;
  *)
    sed -n '4,24p' "$0"
    exit 1
    ;;
esac
