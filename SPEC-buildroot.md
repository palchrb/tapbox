# SPEC — vibb på Buildroot

Diskusjonsgrunnlag, ikke beslutning. Målet er å flytte boksen fra
Raspberry Pi OS (Trixie) til et eget Buildroot-image som booter raskt,
trekker mindre strøm og — viktigst — er *mer* selvhelbredende enn i dag.
Alle påstander under er forankret i kodelesing (fil:linje) og verifisert
mot Buildroot master; det som er usikkert står som åpent spørsmål (§10),
ikke som fakta.

---

## 1. Mål og ikke-mål

Eierens prioriteringer, i rekkefølge, styrer alt:

1. **Stabil drift.** Self-healing hele veien (watchdogs, `Restart=`,
   recovery etter strømkutt og SD-korrupsjon), trivielt brukbar for et
   lite barn. BT og WiFi skal være robuste — reconnects, roaming,
   stale-state-recovery (kjente feilmoduser i NEXT-STEPS.md og
   REVIEW-2026-07-18.md). **Raskere boot kjøpes aldri på bekostning av
   dette.** Konkret: `hci_uart` forblir modul selv om innebygd er
   raskere (§1b), journald beholdes selv om den koster boot-tid, mpv
   beholder sine pinned resample-flagg selv om de "ser unødvendige ut".
2. **Batterilevetid.** ~5 t avspilling i dag på PiSugar 3 (~0,8 W).
   Færre daemons, HDMI/DRM helt ut av kjernen, LED av i firmware,
   WiFi-powersave og governor som i dag. Kvantifisert der grunnlag
   finnes (§1c), ellers merket **må måles**.
3. **Betydelig raskere boot** — delt i to tall som ikke må blandes:
   - **boot-to-UI**: skjerm levende, kortleser klar (det barnet ser).
     Mål: **~8–10 s** (målt baseline i dag: **20,3 s** til READY,
     felt 2026-08-14 — se §8.1).
   - **boot-to-audio**: avhenger av sti — lokal cachet lyd ~+1–3 s
     etter UI; BT-høyttaler og Spotify domineres av radio/nett og lar
     seg ikke "buildroote bort" (§8).

**Ikke-mål (bevisst utenfor scope):**

- Ikke busybox-init eller OpenRC. Appen *bruker* systemd som
  selvhelingsmekanisme (30+ `systemctl`-kall, drop-ins, timers,
  transient units) — en omskriving bryter prioritet 1 for ~1–2 s
  boot-gevinst. `BR2_INIT_SYSTEMD`, ferdig diskutert.
- Ikke bytte NetworkManager mot iwd/wpa_supplicant-direkte i fase 1.
  Det er den eneste utskiftingen som er verdt å *vurdere* senere (§6.2),
  men den nullstiller måneder med feltmodning på nettopp koden
  prioritet 1 bryr seg mest om.
- Ikke PipeWire, ikke ny lydarkitektur. ALSA + bluez-alsa som i dag.
- Ikke funksjonsendringer i appen. Fase 1 er funksjonell paritet.
- Ikke YouTube-støtte: yt-dlp droppes fra imaget (§4, verifisert død
  vekt), gjeninnføring er et eget, senere valg (§7).
- Ikke offentlig distribusjon av ferdige images i første omgang
  (lisens/ToS-notat i §9).

## 1b. Stabilitet og self-healing — hva Buildroot-flyttingen faktisk gir

Dette kapittelet er begrunnelsen for hele prosjektet, ikke et vedheng.

**Nye gevinster som ikke finnes i dag:**

- **Hardware-watchdog.** Repoet har i dag ingen (verifisert — alle
  "watchdog"-treff er applikasjonsnivå: stall-, wifi-, hotspot-vakter).
  Kernel-heng, I2C-lås og SD-stall overlever dermed alle dagens
  healere. Buildroot-imaget får `CONFIG_BCM2835_WDT` +
  `RuntimeWatchdogSec=15` i systemd — null kodeendring, dekker en hel
  feilklasse. Valgfri påbygging: `WatchdogSec=` + sd_notify-ping fra
  vibb-daemon (stdlib-implementerbart).
- **Read-only rootfs (squashfs).** OS-et kan per konstruksjon ikke
  korrumperes av strømkutt — toddler-strømkutt blir en ikke-hendelse
  for alt utenom `/data`. Dette er primært en *stabilitets*-gevinst;
  at komprimert squashfs også leser raskere fra treg SD er bonus.
- **`CONFIG_MEMCG` på.** Backup-unitens `MemoryMax=200M`
  (install.sh:846–899) blir endelig håndhevet — i dag bare en WARNING
  på Pi OS (install.sh:851–856). OOM-dreping av musikken var
  dimensjonerende i backup-analysen.
- **Færre bevegelige deler.** Ingen apt-timere, ingen cloud-init,
  ingen pakker som endrer seg under boksen. Alt i imaget er pinnet og
  testet sammen.

**Det som MÅ overleve flyttingen uendret (regresjonsflate):**

- **BT-recovery-stigen** i `pi/vibb/bt.py`:
  journalctl-krasjsignatur (`bt.py:136–168`, dmesg-fallback finnes) →
  `systemctl stop/start bluetooth` + `try-restart bluealsa` →
  serdev unbind/bind (`bt.py:187–223` — dette er stien som virker i
  felt; `systemctl restart hciuart` er allerede en no-op på Bookworm+)
  → tier 3 `modprobe -r/​ hci_uart`. Krav til imaget: RPi-kernelfork
  (defconfigene bruker den), serdev-attach via DT som i dag, og
  **`hci_uart`/`btbcm` som moduler (=m)** — bygges de inn, dør tier 3
  stille. TX-teller-zombiesjekken trenger `hciconfig`
  (`BR2_PACKAGE_BLUEZ5_UTILS_DEPRECATED`).
- **rfkill-unblock før bluetoothd** (drop-in, install.sh:435–442) og
  `Restart=on-failure` på bluetooth.service — bakes inn i egne units.
- **bluez-alsa-flaggene**: `--keep-alive=120 --loglevel=warning`
  (install.sh:464–512). Keep-alive er ikke pynt — AVDTP-renegotiering
  per pause var en firmware-krasjtrigger. Buildroot shipper egen unit;
  vår erstatter den. Unit-navnet settes til `bluealsa.service` så
  `bt.py:244` (som prøver `bluealsa|bluealsad`) treffer.
- **Persistent journal** (64M-tak, install.sh:310–321) → `/data`.
  RAM-journal ville spart SD-slitasje men mistet kryss-boot-forensikk
  som BT-krasjdeteksjonen leser. Beholdes.
- **btwatchd/mpris/btbus D-Bus-laget** (dbus-python + PyGObject) —
  uten det faller vi til bash-poll (60 s reconnect-latens) og mister
  Agent1-pairing og MPRIS-registreringen som hindrer
  Invalid-Player-ID-loopen (kjent firmware-krasjtrigger mot bilanlegg).
  Shippes. Kill-switchen `VIBB_BT_BACKEND=cli` + poll-scriptet
  `vibb-bt-reconnect-poll` (install.sh:552–588) må også inn i overlayet,
  ellers er fallbacken fiksjon.
- **fsync-disiplinen på `/data`**: appens skrivemønster er allerede
  gjennomgående atomisk (tmp + fsync + `os.replace` i `token._write`,
  `bookmarks.save_state`, `bt._route_alsa`, `bt._persist_mac`,
  `backup._write_secret`). ext4 `data=ordered` holder; `noatime` og
  ev. `commit=30` for SD-slitasje. fsck ved boot + en
  reformater-hvis-umountbar fallback-unit (mkfs + factory-seed) gjør
  at boksen *alltid* kommer opp; restic-restore henter konfig,
  hemmeligheter og bokmerker tilbake — nøyaktig det `backup.py` ble
  bygget for.
- **WiFi-recovery**: auto-join etter rfkill-unblock er NM-semantikk
  (`netmgmt.py:342–372`), fresh-box-hotspot-vaktbikkja
  (`netmgmt.py:283`), captive-portal-onboardingen. Alt beholdes ved å
  beholde NM — men se §6.1: Buildroots NM-pakke trekker IKKE inn alt
  den trenger, det må konfigureres eksplisitt.
- **Klokke-tillit**: `paths.py:93` leser
  `/run/systemd/timesync/synchronized` — behold
  `BR2_PACKAGE_SYSTEMD_TIMESYNCD` (ikke chrony; `power.sh:314` gater
  RTC-write-back på `timedatectl show -p NTPSynchronized`).
  `go-librespot.service` beholder `After=vibb-rtc` (TLS mot
  1970-klokke) og DNS-ExecStartPre — men `getent` finnes ikke på
  Buildroot (verken busybox eller glibc-utils installerer den);
  gaten omskrives til `python3 -c "import socket;
  socket.getaddrinfo('apresolve.spotify.com', 443)"`.

**Verifikasjonsgates før fase 1 erklæres ferdig** (gjenbruk
riggsjekklistene i PLAN-bt-dbus.md/PLAN-bt-b2-pairing.md):
charger-pull-heal < 40 s, speaker-off→on < 5 s, høyttalerbytte uten
restart, 16 kHz-lydbok hørbar over A2DP (stillhets-regresjonen),
captive-portal-onboarding på fabrikkfersk boks, strømkutt ×20 under
skriving til `/data` uten tap, `VIBB_BT_BACKEND=cli` kjørbar.

## 1c. Batteribudsjett

Ærlig ramme: det meste av strømoptimaliseringen er *allerede gjort* i
appen (bgscan strippet, WiFi-powersave-governor i daemon.py:5505+,
charger-follow-governor, LED-styring, PN532-power-gating i rfid.py).
Buildroot-gevinsten er reell men moderat:

| Tiltak | Estimat | Status |
|---|---|---|
| DRM/vc4/HDMI helt ut av kjernen (i dag finnes ingen KMS — tomt `/sys/class/drm`, power.sh:122–128 — men Pi OS-kjernen bærer stacken) | ~0,02–0,05 W | må måles |
| Færre daemons (cloud-init borte, apt-timere borte, ModemManager o.l. aldri inn, trimmet systemd) | ~0,02–0,05 W idle | må måles |
| ACT-LED av i firmware (`dtparam=act_led_trigger=none`) i stedet for runtime `set_leds` | ~0,01 W | polaritet på Zero 2 W må verifiseres i felt — feil vei = LED fast PÅ; behold dagens runtime-mekanisme som fallback |
| `gpu_mem=16` + `start_cd.elf` | mer RAM, ikke strøm | sikker |
| `maxcpus=2` i cmdline (CPU-hotplug finnes ikke i Pi-kjernen, power.sh:111–117) | ukjent, mulig negativ (race-to-idle) | må måles A/B |
| WiFi-powersave, governor | uendret — appen eier dette allerede | ingen endring |

Sum realistisk: kanskje **0,05–0,1 W** av ~0,8 W → i størrelsesorden
en halvtime ekstra spilletid. Verdt å ta, ikke verdt å love. Målemetode:
PiSugar-strømlesing over natt-idle + spilletime, Pi OS vs Buildroot,
samme innhold.

---

## 2. Dagens system (kort)

Raspberry Pi OS Trixie gir oss: RPi-kernel med all HW-støtte, apt
(sikkerhetsoppdateringer for alt, install.sh:114–121 verdsetter dette
eksplisitt), Blinka/pip-økosystemet rett fra hylla, og velkjent
feilsøking. Det koster: ~25–35 s til UI (35 s frossen skjerm var
utgangspunktet før DefaultDependencies=no-fiksen, install.sh:936–945;
cloud-init alene kostet ~6 s, install.sh:763–767; network-online-venting
kostet ~18 s, install.sh:1063–1072), skrivbar rot som kan korrumperes av
strømkutt, ~800 av install.sh sine 1260 linjer som runtime-reparerer
distroen (drop-ins, unit-omskrivinger, config-migreringer), og en
patologisk hale ("Startup finished in 1min 17s" ved dårlig WiFi,
power.sh:160–167). To python-tolker (system + `/opt/vibb/venv`) er en
ren Pi OS-artefakt.

---

## 3. Målbilde

### Partisjonslayout (fase 1)

```
p1  boot   FAT32   ~128M   firmware, kernel, DTB+overlays, config.txt, cmdline.txt   (ro-montert)
p2  rootfs squashfs ~150–250M  OS + app, zstd                                        (ro per konstruksjon)
p3  data   ext4    resten   ALT muterbart                                            (rw, noatime)
```

Fase 2 utvider til A/B (bootA/bootB + rootA/rootB + data) med RAUC over
Pi-firmwarens `tryboot` — uten U-Boot (§7).

### /data-innmat

`etc/vibb/` (hele dagens /etc/vibb: settings, cards, token, bt-headset,
rfid.conf, storytel, spotify-api, library, extras, pending-map),
`state/`, `cache/`, `spotify-cache/` (20 GB-budsjettet,
install.sh:269 — dimensjonerer partisjonen), `media/` (uerstattelig
brukeropplastet innhold), `art/`, `go-librespot/` (config.yml +
credentials + state — muterbar i drift via output.py),
`system/` (asound-bt.conf, boksnavn), `network/` (NM-keyfiles),
`bluetooth/` (bond-nøkler — kritisk: nøkler som ikke overlever
strømkutt gjør boksen ubrukelig), `ssh/` (vertsnøkler),
`log/journal/`, `restic-cache/`, `app/` (§7), `machine-id`
(bind-mount før systemd — tom fil trigger first-boot-semantikk).
Factory-seed via systemd-tmpfiles fra `/usr/share/vibb/factory/`
(malen finnes: install.sh:220–246 skriver allerede placeholder-asound
med dummy-MAC).

### Init og oversikt

`BR2_INIT_SYSTEMD` (glibc-toolchain, som også go-librespot-binærene og
NSS forutsetter). Trimmet: uten logind-setekompleksitet; timesyncd PÅ;
dbus (ev. dbus-broker — verifiser at bluez/bluealsa-policyfiler plukkes
opp). Ingen U-Boot, ingen initramfs — Pi-firmwaren er bootloaderen:

```
ROM → bootcode.bin (SD) → start_cd.elf leser config.txt, flater DT
    → kernel (monolittisk der det er trygt; hci_uart/btbcm =m)
    → systemd → sysinit → local-fs (/data, RequiresMountsFor på lyd-units)
        ├── vibb-ui        (DefaultDependencies=no — skjerm først)
        ├── vibb-daemon    (basic.target, aldri network-online)
        ├── bluetoothd ← rfkill-unblock ExecStartPre; bluealsa --keep-alive=120
        ├── NetworkManager → wpa_supplicant (D-Bus) → auto-join / hotspot+dnsmasq+iptables
        ├── go-librespot   (After=vibb-rtc, DNS-gate via python-oneliner)
        └── vibb-rfid / mpris / btwatchd / idle / backup.timer / rtc / pisugar
```

Lærdommen som må videreføres ordrett: **nettet gater aldri boot** —
unit-orderingen fra install.sh (to måneders feltfikser) migreres linje
for linje, ikke fra minnet.

---

## 4. Avhengighetskartet

Verifisert mot Buildroot master. Kolonnen "status": ✓ = finnes,
⚠ = finnes men krever konfig/forbehold, ✗ = må bygges/vendors,
✂ = droppes.

| Behov (kilde i repo) | Buildroot | Status |
|---|---|---|
| systemd, journald, timesyncd, timers, systemd-run (30+ call sites; ui.py:2806 transient extras) | `BR2_INIT_SYSTEMD`, `BR2_PACKAGE_SYSTEMD_TIMESYNCD` | ✓ |
| mpv (`player.py:111–146` pinned argv, prewarm daemon.py:5395) | `BR2_PACKAGE_MPV` — headless (ingen X/GL-pakker valgt), ingen Lua; krever GCC≥10/C++ toolchain | ⚠ |
| ffmpeg/ffprobe (HLS→m4a-cache content.py:942, cover-extract daemon.py:3942, probe) | `BR2_PACKAGE_FFMPEG` + `_FFMPEG`/`_FFPROBE`. Default er "all" (fett) — jobben er å *slanke*, ikke frykte manglende demuxere. **TLS: gnutls, eller openssl uten GPL-konflikt (`FFMPEG_GPL` force-disabler openssl uten `_NONFREE`) — gate på `--enable-openssl/gnutls` i configure-loggen.** lavfi/sine må med (prewarm-fallback daemon.py:5408) | ⚠ |
| yt-dlp (kun apt-liste install.sh:106; mpv kjører `--no-ytdl --load-scripts=no`; NRK løses native via psapi) | — | ✂ droppes. Verifisert død vekt; "ukjent URL"-passthrough (content.py:33,1083) produseres ikke av kort/bibliotek-flyten |
| NetworkManager + nmcli (`netmgmt.py:192` `_nmcli()`, 10 funksjoner) | `BR2_PACKAGE_NETWORK_MANAGER` **+ `_CLI`** (nmcli er subopsjon — uten den er netmgmt.py stum) | ⚠ |
| wpa_supplicant (NM-backend) | `BR2_PACKAGE_WPA_SUPPLICANT` + **`_DBUS`** + `_NL80211` + **`_AP_SUPPORT`** — NM selecter den IKKE; uten disse har imaget ingen WiFi/hotspot i det hele tatt | ⚠ |
| Captive portal (nmcli shared mode; `address=/#/10.42.0.1` install.sh:834; portal-redirect daemon.py:5348+) | **`BR2_PACKAGE_DNSMASQ` + `BR2_PACKAGE_IPTABLES`** (NM .mk hardkoder iptables-sti; nft kun med NFTABLES=y) + kernel-NAT (`CONFIG_NF_NAT`, masquerade). NM selecter INGEN av delene | ⚠ eksplisitt arbeid |
| avahi (`<navn>.local`; mdns_host netmgmt.py:126–155 med gethostname-fallback) | `BR2_PACKAGE_AVAHI` + `_DAEMON`, `publish-workstation=no` bakt inn | ✓ |
| BlueZ 5.86 (btbus verifisert mot 5.82-API) | `BR2_PACKAGE_BLUEZ5_UTILS` + `_CLIENT` (bluetoothctl-fallback) + `_DEPRECATED` (hciconfig/TX-teller) + `_MONITOR` (btmon, btsnoop.sh) + `_PLUGINS_AUDIO` | ✓ |
| bluez-alsa 4.3.1 (aplay bygges ubetinget; btbus.py:383 trenger den) | `BR2_PACKAGE_BLUEZ_ALSA`; egen unit m/ `--keep-alive=120 --loglevel=warning`, a2dp-source-profil, D-Bus-policy verifisert | ⚠ unit-arbeid |
| dbus-python + PyGObject (btwatchd.py:73–75, mpris.py, btbus-agent) | `BR2_PACKAGE_DBUS_PYTHON`, `BR2_PACKAGE_PYTHON_GOBJECT` (gobject-introspection bruker host-qemu user-mode i bygget + krever target-python3 — virker, men er den mest byggesære biten) | ⚠ |
| Pillow + numpy (ui-rendering; RGB565-push ui.py:702) | `BR2_PACKAGE_PYTHON_PILLOW` (auto-detekterer freetype/jpeg/zlib/webp — velg dem), `BR2_PACKAGE_PYTHON_NUMPY`. **freetype m/ libpng for CBDT-emoji** | ⚠ |
| DejaVu-fonter (hardkodet sti ui.py:625) | `BR2_PACKAGE_DEJAVU` — installerer til `/usr/share/fonts/dejavu/`, ui.py forventer `.../truetype/dejavu/` → symlink i overlay | ⚠ |
| NotoColorEmoji.ttf (ui.py:208; scrub-fallback finnes) | vendor TTF i overlay (OFL) | ✗ |
| st7789 + spidev (ui.py:718–724, SPI0 CS1, 80 MHz) | `BR2_PACKAGE_PYTHON_SPIDEV` ✓; **st7789 vendors** — pin versjon OG GPIO-backend (driver DC/reset selv) | ✗ delvis |
| gpiozero + lgpio (knapper/PWM-backlight; lgpio-krav pga kernel 6.x-edge, install.sh:650–656) | `BR2_PACKAGE_PYTHON_GPIOZERO` ✓ men `depends on BR2_arm` (32-bit; triviell patch om 64-bit velges); **lgpio: egen pakke** (C-lib `lg` + swig-binding) — ikke valgfri, LgpioInput prøves først | ✗ delvis |
| PN532 (rfid.py:145–174: UID-polling + MOSFET-power-gate + presence-pin) | Blinka-treet vendors IKKE — **omskriv driveren over `BR2_PACKAGE_PYTHON_SMBUS2`** (~200–250 linjer; GPIO-delene dekkes av lgpio) | ✗ omskriving |
| evdev, qrcode | `BR2_PACKAGE_PYTHON_EVDEV`, `BR2_PACKAGE_PYTHON_QRCODE` | ✓ |
| soco + requests (sonosd.py — egen opt-in-unit) | ikke i Buildroot; vendor som pakker ELLER nedskop Sonos i v1 | ✗ / beslutning |
| go-librespot-fork (palchrb v0.2.0; CGO: alsa-lib, libogg, libvorbis, flac, mpg123; Go ≥1.25 — master har 1.26.6) | egen `package/go-librespot/` med golang-infraen, pin ≥v0.1.5 (snd_config_update_free_global — stillhets-buggen) | ✗ egen pakke |
| restic + rclone (backup.py:57–58, env-overstyrbare) | ikke i Buildroot; to egne golang-pakker (alt.: statiske release-binærer i overlay — grei bootstrap, dårlig endestasjon) | ✗ egne pakker |
| pisugar-server (TCP :8423; sysinfo.py:146, power.sh) | ikke i Buildroot (Rust); se §6.5 | ✗ |
| vcgencmd (bt.py:238 undervolt-evidens; power.sh) | **`BR2_PACKAGE_RPI_USERLAND` er fjernet fra master** — egen liten cmake-pakke av raspberrypi/utils, eller sysfs-fallback i koden. Best-effort-guardet allerede | ✗ / kodefiks |
| openssl CLI (storytel.py:224–235 AES) | `BR2_PACKAGE_LIBOPENSSL_BIN` (libopenssl er om bord uansett) | ✓ |
| iw, rfkill (JSON: netmgmt.py:29), ss (idle.py:129 ssh-hold), taskset | `BR2_PACKAGE_IW`, `BR2_PACKAGE_UTIL_LINUX_RFKILL`, `BR2_PACKAGE_IPROUTE2`, `BR2_PACKAGE_UTIL_LINUX_SCHEDUTILS` | ✓ |
| nc -q1 (power.sh:69,73,337 → pisugar) | `BR2_PACKAGE_NETCAT_OPENBSD` — eller port helperne til python (sysinfo.pisugar_get finnes) | ✓ |
| sshd (idle.py holder auto-off på etablert :22; ClientAlive-drop-in) | `BR2_PACKAGE_OPENSSH` (ikke dropbear — ss/ClientAlive-logikken) | ✓ |
| bash (power.sh, extra.sh, poll-scriptet: arrays, `[[`) | `BR2_PACKAGE_BASH` | ✓ |
| curl, jq, aplay, i2c-tools (operatørverktøy) | `BR2_PACKAGE_LIBCURL_CURL`, `BR2_PACKAGE_JQ`, `BR2_PACKAGE_ALSA_UTILS`, `BR2_PACKAGE_I2C_TOOLS` | ✓ |
| python3-stdlib-gates | `BR2_PACKAGE_PYTHON3_SSL`, `_ZLIB`, `_PYEXPAT` (xml.etree i content.py), `BR2_PACKAGE_CA_CERTIFICATES`. (sqlite3/unicodedata brukes IKKE — tidligere antakelse, avkreftet) | ✓ |
| WiFi/BT-firmware Zero 2 W (BCM43436/43430B0) | `BR2_PACKAGE_BRCMFMAC_SDIO_FIRMWARE_RPI` + `_WIFI` + `_BT` — **verifiser at bundelen faktisk shipper `brcmfmac43436-sdio.*` + matchende `.hcd`** (hjelpteksten nevner Pi 3/Zero W); ellers vendor RPi firmware-nonfree/bluez-firmware. Manglende .hcd = stille "ingen BT etter boot" | ⚠ |
| tailscaled (power.sh:133,146, guarded `\|\| true`) | droppes i v1 (debug-VPN) | ✂ |
| getent (go-librespot ExecStartPre) | finnes ikke — omskriv gaten (§1b) | ✂ kodefiks |

Kernel-obligasjoner (RPi-fork, egen defconfig): `MMC_BCM2835` (SD) +
**`MMC_SDHCI_IPROC`** (WiFi-SDIO sitter på den andre kontrolleren —
uten den finnes ikke wlan0; farligste enkelthullet), brcmfmac/cfg80211,
`BT_HCIUART_BCM` **=m**, I2S/hifiberry-dac/pcm5102a (ALSA-navnet
`hw:sndrpihifiberry` er kontrakt), `GPIO_CDEV`, `SPI_BCM2835`+`SPI_SPIDEV`,
`I2C_BCM2835`+`I2C_CHARDEV`, `INPUT_EVDEV`+`UINPUT`, `MEMCG`,
`BCM2835_WDT`, `HW_RANDOM_BCM2835` (entropi — crng-stall kan ellers
koste sekunder ved boot), squashfs+zstd, ext4, NF_NAT/masquerade,
cpufreq ondemand+powersave (+ eksplisitt valg av default-governor —
`initial_turbo` i config.txt er bare meningsfull hvis default ikke er
ondemand; install.sh:747–750 målte den til ~null på Pi OS).

## 5. Kodeendringer som kreves i pi/

Prinsipp: nesten alt er allerede env-styrbart (`VIBB_*` i paths.py m.fl.)
— endringene er få og kan gjøres **på Pi OS først** (fase 0, §9).

| # | Fil | Endring | Estimat |
|---|---|---|---|
| 1 | `pi/rfid.py:44–45` | `CARDS_FILE` og `PENDING_FILE` hardkodet til /etc/vibb — gi dem env-knapper som resten (backup.py følger allerede `VIBB_ETC`; asymmetrien er en latent bug i dag) | 30 min |
| 2 | `pi/vibb/netmgmt.py:48` + `pi/token.sh:51` | `hostname -I` er Debianisme (busybox har den ikke) — erstatt med stdlib-socket-triks eller `ip -j addr`. Mater pairing-QR-en (ui.py:3045) — må ikke glippe | 1 t |
| 3 | Alle units | Én felles `EnvironmentFile=/etc/vibb/env` (statisk i imaget) som peker alle `VIBB_*` mot /data — inkl. **`VIBB_GO_CONFIG`** (defaulter til tom streng, output.py:13 — usatt no-op-er alle config-skrivene stille) og `VIBB_ASOUND=/data/system/asound-bt.conf`. NB: backup kjører in-process i daemonen — env må stå på vibb-daemon-uniten, ikke bare backup-uniten | 2–4 t |
| 4 | Entry-scriptenes bootstrap | `sys.path.insert(0, ...)`-headerne (rfid.py:36–41, bt.py:49–54, daemon/ui/player tilsv.) slår PYTHONPATH — må respektere `VIBB_APP` først for at app-kanalen (§7) skal virke. Kanalen må dekke entry-scripts + `vibb/`-pakken + `web/` (`VIBB_WEB`, daemon.py:287) | 2–4 t |
| 5 | `pi/power.sh` | `boot-on`/`log-on`/`idle-on` genererer units i runtime (power.sh:157–203) → statiske units + `/data`-flagg (`ConditionPathExists=`). `taps-on/off` skriver `/usr/local/bin`-stier inn i pisugar-config — stiene består (vi beholder layouten), men verifiseres. `hat-audio-on/off` (config.txt-redigering, power.sh:251–275) → beslutning §10 | 0,5–1 dag |
| 6 | `pi/vibb/netmgmt.py` `mdns_host()` | Les boksnavn fra `/data/system/boxname` (boot-unit setter hostname + avahi-drop-in derfra). NB: hotspot-SSID `Vibb-<hostname>` (netmgmt.py:162) arver samme kilde. Boksnavnet MÅ overleve reflash — PWA-token er per-origin (`<navn>.local`) | 2–4 t |
| 7 | go-librespot DNS-gate | `getent` → python-oneliner i ExecStartPre (unit-fil, ikke appkode) | 15 min |
| 8 | PN532-driver | Omskriv fra Blinka til smbus2 + lgpio (UID-polling, power-gate BCM-pin, presence-pin — rfid.py:145–180). retry-forever-strukturen består | 2–4 dager inkl. riggtest |
| 9 | venv-splitten | Bortfaller — én python, alle pip-deps til system-site-packages, ~14 units får ny `ExecStart`-sti (mekanisk, del av overlayet) | del av #3 |
| 10 | Valgfritt | `GPIOZERO_PIN_FACTORY=lgpio` i vibb-ui-uniten (sparer backend-probing i PWMLED-init — del av 2,2 s panel-init, ui.py:712–716). Virker på Pi OS i dag også | 5 min |
| 11 | Valgfritt, anbefalt | Stabile per-høyttaler PCM-navn (NEXT-STEPS.md øverst) — akkumuleres ved *paring*; løser ro-rootfs-ALSA og stale-config-klassen med samme grep (§6.4) | 2–4 dager |

install.sh pensjoneres som mekanisme men beholdes som fasit: hver
drop-in og hvert unit-triks bærer et felt-lært hvorfor. Migreres linje
for linje til `br2-vibb/`-overlayet, med diff-sjekk mot install.sh per
app-release til den er død.

## 6. De harde problemene

### 6.1 NetworkManager vs alternativer

**Alternativer:** (a) behold NM; (b) iwd (erstatter NM + wpa_supplicant,
innebygd auto-connect/DHCP/AP-modus, D-Bus-API); (c) rå wpa_supplicant +
dhcpcd; (d) connman.

**Anbefaling: (a) i fase 1.** netmgmt.py (538 linjer) er skrevet mot
nmcli-semantikk gjennom ett chokepoint (`_nmcli()`, med
FileNotFoundError-gren — forfatteren har allerede forberedt en swap).
Captive-portalen, bgscan/IPv6-per-profil-tuningen og
auto-join-kontrakten er feltmodnet. Men Buildroot-NM er ikke
plug-and-play: `_CLI`-subopsjonen, wpa_supplicant med `_DBUS`/`_AP_SUPPORT`,
dnsmasq, iptables og kernel-NAT må alle velges eksplisitt (§4) og
hotspot-aktivering må boot-testes. (b) er den eneste kredible
utskiftingen: ~300–400 linjer netmgmt-omskriving + egenbygd
captive-portal (iwd har ingen wildcard-DNS) for ~1–2 s boot og
~15–20 MB RAM — fase 2-eksperiment bak den eksisterende sømmen.
(c) og (d) forkastes.

### 6.2 go-librespot-forken — cross-compile

CGO (linker alsa-lib, libogg, libvorbis, flac, mpg123 — MP3-dekoderen
kom i v0.1.8, mange Spotify-podkaster er MP3). To veier:
**(a, anbefalt)** egen `package/go-librespot/` på golang-infraen,
CGO mot staging-libs, versjon pinnet i defconfig (i dag pinnes den i
install.sh:141 + `.go-librespot.version`). Krever host-go ≥1.25 —
oppfylt i master (1.26.6), sjekkpunkt ved valg av eldre LTS.
**(b)** prebuilt release-binær i overlay — matcher glibc og 32/64-valget
(`armv6_rpi` vs `arm64`-asset, install.sh:200–207), grei bootstrap,
manuell hash-bump per fork-release. Minimum fork-versjon **v0.1.5**
(snd_config_update_free_global) — ellers kommer stillhets-buggen
2026-07-27 tilbake. I praksis: pin v0.2.0.

### 6.3 yt-dlp-oppdateringer på ro-rootfs

Omformulert etter kodelesing: **problemet finnes ikke i dag** — yt-dlp
kjøres aldri (mpv: `--no-ytdl`; NRK: native psapi). Den *reelle*
månedlige driften er vibbs egen kode (content.py psapi-drift,
storytel.py, spotify_web.py). Svaret er derfor ikke "yt-dlp på /data",
men **app-oppdateringskanalen i §7** — frikoblet fra image-releaser.
Gjeninnføres yt-dlp (YouTube-ønske) legges zipapp-binæren i
`/data/opt/yt-dlp` med vibb-styrt, sha256-verifisert nedlasting — aldri
i rotfs. Kompatibilitetsflaten er bare python ≥3.9.

### 6.4 asound.conf-mutasjon på ro-rootfs

`bt.py:_route_alsa` (430–463) omskriver `/etc/asound.conf` ved
høyttalerbytte — dødt på ro rot. Løsning i to trinn:
**(1)** Statisk `/etc/asound.conf` i imaget med `@hooks load` av
`/data/system/asound-bt.conf` (+ `pcm.vibb_local` statisk);
kodeendringen er én env (`VIBB_ASOUND`, kroken finnes, bt.py:72).
Factory-seed garanterer at fila alltid finnes (dagens
placeholder-mønster, install.sh:220–246). alsa-libs
dobbeltdefinisjonssemantikk (hook-lastet vinner) verifiseres på rigg,
antas ikke. v0.1.5-mekanikken i forken leser den hook-lastede fila på
nytt — live-bytte overlever. **(2, anbefalt samtidig)** Stabile
per-høyttaler PCM-navn fra NEXT-STEPS-backloggen: bytte mellom kjente
høyttalere blir et rent navnevalg uten config-mutasjon — ro-rootfs og
stale-config-klassen løses av samme grep. Backloggens forbehold
("etter at v0.1.5 er feltverifisert") er oppfylt.

### 6.5 PiSugar

Rust-daemon, ikke i Buildroot. **(a, anbefalt fase 1)** egen
cargo-pakke av pisugar-power-manager-rs — power.sh/sysinfo/idle
beholder TCP-protokollen (:8423) og den kalibrerte batterikurven
(sugar-config.txt) uendret. **(b)** vendor arm-binæren fra release-.deb.
**(c, fase 2-kandidat)** liten python-modul rett mot PiSugar-I2C med
:8423-linjeprotokoll som shim — fjerner en daemon (prioritet 2) men
re-implementerer kurvelogikken; boksen bruker bare batteri%, spenning,
strøm, plugged, RTC og tap-knapp-konfig. Diskusjonspunkt (§10).

### 6.6 Utvikler-/recovery-tilgang på headless boks

Buildroot-planen dropper HDMI-stacken, PL011-UART-en tilhører BT, og
er WiFi-koden ødelagt er boksen unåelig. Behov: sshd (openssh, §4) +
en siste nødvei. Kandidater: USB-gadget serial/ether (`dwc2` +
`g_serial`/`g_ether` — Zero 2 W er OTG; koster litt kernel, må ikke
stå på i drift) eller en boot-partisjons-hook ("legg
wifi-credentials-fil på FAT-partisjonen, importeres ved boot").
Anbefaling: boot-partisjons-hooken (null strømkost, null angrepsflate
utover fysisk tilgang som allerede er trust-ankeret) + `g_serial`
bak et config.txt-flagg for verkstedsbruk. Diskuteres (§10).

## 7. Oppdaterings- og utviklerhistorien

**To kanaler, bevisst frikoblet:**

**App-kanalen (hyppig, liten):** imaget shipper appen i rotfs som i
dag; units kjører med `VIBB_APP`-oppløsning der
`/data/app/current/` vinner om den finnes (krever bootstrap-fiksen,
§5.4, og at kanalen dekker entry-scripts + `vibb/` + `web/`).
Oppdatering = signert tar (minisign/ed25519, nøkkel i imaget) via
PWA-opplasting eller URL, utpakket til `/data/app/<versjon>/`, atomisk
symlink-swap, restart av vibb-units (ikke go-librespot/BT).
**Selvheling:** krasjer daemonen N ganger på en app-versjon
(`StartLimitAction`/ExecStartPre-guard) fjernes `current`-symlinken og
image-kopien tar over — en dårlig app-oppdatering kan aldri ta boksen.

**Image-kanalen (sjelden, stor):** fase 1 = ren SD-reflash, `/data`
overlever (genimage-oppsett i `br2-vibb/`). Fase 2 = **RAUC A/B over
Pi-firmwarens tryboot** (`BR2_PACKAGE_RAUC` + `BR2_PACKAGE_HOST_RAUC`):
atomisk slot-bytte med automatisk rollback når ny slot ikke markeres
god (helsesjekk: daemon svarer + lyd-smoketest). Kernel/DT ligger
per-slot → også kernel-oppgraderinger blir atomiske. Ærlig forbehold:
RAUC-manualen dokumenterer custom-backend-*grensesnittet*;
Pi-tryboot-integrasjonen er community-oppskrift, DIY-kode som må
strømkutt-testes hardt. swupdate er teknisk likeverdig — velg én,
ikke diskuter lenge.

**Dev-arbeidsflyt:** `br2-vibb/` BR2_EXTERNAL-tre i dette repoet
(configs/vibb_defconfig, board/vibb/ med genimage + overlay +
post-image, package/go-librespot, package/restic, package/rclone,
package/pisugar-server, package/python-*). Daglig iterasjon uendret i
tempo: rsync til `/data/app/dev/` + `systemctl restart` — sekunder;
dev-kanalen ER prod-kanalen og testes dermed kontinuerlig. En
`make push`-target erstatter `git pull + sudo ./install.sh`.
Systemendringer: inkrementell Buildroot-rebuild (minutter m/ ccache;
kaldt bygg 1–3 t → trenger CI/builder, §9). Behold én Pi OS-rigg som
referanse under overgangen. `tests/` (~150 regresjonstester,
host-kjørbare via env-knappene) kjøres i CI under *Buildrootens*
python-versjon. Per-variant defconfig-fragmenter så `pipod/` kan bli
en andre defconfig senere i stedet for en fork.

**Backup/restore:** `backup.py` flytter praktisk talt uendret (leser
samme `VIBB_*`-env). Restore blir *viktigere*: offisiell vei tilbake
etter /data-reformat og SD-bytte — det mangler bare UI-flyten i
setup-portalen (wifi → lim inn rclone-conf + repo-passord →
`restore_snapshot("latest")` → reboot). Whitelisten dekker ikke
NM-profiler, BT-linkkeys, SSH-nøkler eller boksnavn i dag (§10).
**Factory reset** finnes ikke i dag og blir triviell på ro rot:
markørfil → mkfs /data → factory-seed → fresh-box-vaktbikkja reiser
setup-hotspoten selv.

## 8. Boot-tid-budsjett

### 8.0 systemd-analyze — hvor de 20 sekundene faktisk ligger (felt 2026-08-18)

```
Startup finished in 3.940s (kernel) + 18.023s (userspace) = 21.964s

sysinit.target @7.815s
└─systemd-tmpfiles-setup.service @7.469s +278ms
  └─local-fs.target @7.391s
    └─boot-firmware.mount @7.132s +249ms
      └─systemd-fsck@...partuuid-9773a0ea-01.service @5.836s +1.277s
        └─dev-disk-by-partuuid-9773a0ea-01.device @5.811s

7.646s NetworkManager.service      1.526s NetworkManager-wait-online
4.385s vibb-rtc.service            1.277s systemd-fsck@boot-firmware
2.411s dev-mmcblk0p2.device        1.210s user@1000.service
1.750s bluealsa.service             922ms avahi-daemon.service
1.645s bluetooth.service            694ms systemd-udev-trigger.service
```

**To antakelser i tidligere versjoner av denne spec-en var feil:**

1. **`initramfs er allerede av.`** Ingen `(initrd)`-ledd i utskriften.
   `auto_initramfs=0` er altså ikke et gjenstående tiltak — det er
   gjort. (Bare ikke fanget i install.sh; se §5.)
2. **Kjernen er 3,94 s, ikke 8–12 s.** Estimatet var mer enn dobbelt
   for høyt. Kjernen er nær gulvet for en Zero 2 W, og
   **kjernetrimming er derfor ikke den store Buildroot-gevinsten**
   spec-en antok. Maksimalt ~1,5–2 s ligger der.

**Hele ventetiden ligger i userspace — og den er dominert av lagring.**
Kjeden over sier det presist: `sysinit.target` kan ikke fullføre før
`local-fs.target`, som venter på at `/boot/firmware` skal monteres, som
venter på 1,28 s fsck av FAT-partisjonen, som venter på at
partisjons-noden i det hele tatt dukker opp (`@5.811s`;
`dev-mmcblk0p2.device` bruker 2,41 s). Det er SD-enumerering, ikke CPU.

**Fork-tidspunktet er målt** (`systemctl show vibb-ui -p
ExecMainStartTimestampMonotonic` = 11845869 µs = uptime 11,85 s). Det
fester to ting: `@`-tidene i critical-chain er userspace-relative
(sysinit fullfører på uptime 3,94 + 7,82 = 11,76 s), og systemd forker
`vibb-ui` 90 ms etter det. Gapet til første logglinje (uptime 13,8 s) er
altså **~2,0 s Python-tolkeroppstart**, ikke systemd-venting.

Fullt regnskap for kaldstarten 17:12 (19,8 s til READY):

| Blokk | Tid | Andel |
|---|---|---|
| Kjerne | 3,94 s | 20 % |
| **Userspace → sysinit (lagringskjeden)** | **7,82 s** | **39 %** |
| Fork → første logglinje (Python-oppstart) | 1,95 s | 10 % |
| Tunge importer (PIL m.m.) | 2,3 s | 12 % |
| Panel 1,7 + backlight 0,5 | 2,2 s | 11 % |
| Splash → READY | 1,2 s | 6 % |

Boot-til-boot-varians er ~2 s (booten 18:34 ga READY 22,0 s og
NetworkManager 9,99 s mot 7,65 s) — **A/B-testing krever minst tre
kalde booter per variant.**

**Og `vibb-ui` står i den køen uten å ha bruk for den.** Uniten er
`After=local-fs.target sysinit.target` (install.sh:936–945). Rot-fs-en
er montert av kjernen før systemd i det hele tatt starter; alt UI-en
leser (venv, fonter, bibliotek, `/dev/spidev0.1`) er tilgjengelig lenge
før FAT-partisjonen er funnet og sjekket. Skjermen venter altså på en
partisjon den aldri åpner. Se §8.3 pkt. 1.

### 8.1 Målt baseline — Pi OS, kaldstart (felt 2026-08-14)

Dette er **ekte tall**, ikke estimater: fra vibb-ui sin egen
instrumentering (`pi/ui.py:4177–4184` og `3837`), forankret i
`/proc/uptime` — den ene klokka på boksen som ikke hopper når
PiSugar-RTC-en lander midt i booten (`ui.py:57–68`).

| Fase | Tid | Kumulativt |
|---|---|---|
| Firmware + kjerne + local-fs + sysinit — **før UI-prosessen kjører** | ~14,1 s | boot+14,1 s |
| `imports took` (PIL m.m., kald SD) | 2,7 s | boot+16,8 s |
| `panel 1,8 s + backlight 0,5 s` | 2,3 s | — |
| Splash tent | — | boot+19,3 s |
| **READY (karusellen oppe)** | — | **boot+20,3 s** (6,2 s inne i UI-en) |

Andre kaldstart samme dag gir samme bilde: imports 2,5 s, splash
boot+18,0, READY boot+19,1 (6,0 s i UI-en).

**Varm restart** (2026-08-15) til sammenligning: imports **0,6 s**,
READY 3,4 s i UI-en. Importene faller 2,6 → 0,6 s med varm sidecache —
altså er *kalde SD-lesninger* hoveddelen av UI-prosessens tid, ikke CPU.

**Backlight varierer 0,2 / 0,4 / 0,5 / 1,1 s** mellom boots. Det er
signaturen til gpiozero som prober GPIO-backends i tur — nøyaktig det
`ui.py:712–716` beskriver. `GPIOZERO_PIN_FACTORY=lgpio` fjerner
variansen, og i verste fall er det et helt sekund.

Til historikk: `~35 s frossen skjerm` (install.sh:939–941) var
utgangspunktet *før* `DefaultDependencies=no`-fiksen. De 20 sekundene
over er altså etter at den boot-runden allerede er høstet.

### 8.2 Hva baselinjen betyr

1. **~14 av 20 sekunder er før UI-prosessen i det hele tatt starter.**
   To tredjedeler av ventetiden. Ingen av app-tiltakene (§8.3) rører
   dem.
2. **Hele UI-prosessen er ~6 s.** Taket for app-siden er derfor ~5 s,
   realistisk 2–3 s. 20 s → ~17 s er hele den historien.
3. **Den 14,1 s-blokka er ikke oppsplittet ennå — og det er den ene
   gjenstående målingen som flytter beslutningen.** `vibb-ui` er
   `DefaultDependencies=no` + `After=local-fs.target sysinit.target`
   (install.sh:936–954), så blokka er firmware + kjerne + local-fs +
   **hele sysinit.target** — inkludert udev coldplug, modullasting og
   eventuell fsck. En del av det er angripelig på Pi OS; resten er ekte
   kjernetid og dermed Buildroot-eksklusivt. Splitt den før valget tas:

   ```
   systemd-analyze time                          # kernel / initrd / userspace
   systemd-analyze critical-chain sysinit.target # hva inne i sysinit brenner
   ```

   `systemd-analyze time` svarer samtidig på om `auto_initramfs` faktisk
   er av på boksen (et `(initrd)`-ledd betyr at den kjørte).

### 8.3 Tiltak, sortert etter hvor de treffer

| Tiltak | Treffer | Anslag | Krever Buildroot? |
|---|---|---|---|
| `auto_initramfs=0` i config.txt | 14,1 s-blokka | 1,5–3 s (M) | nei |
| `quiet loglevel=3`, drop `console=serial0,115200` | 14,1 s-blokka | 1–3 s (M) | nei |
| udev/sysinit-trimming | 14,1 s-blokka | ukjent til §8.2 pkt. 3 er målt | delvis |
| Kjernetrimming (monolittisk, få moduler) | 14,1 s-blokka | den store posten | **ja** (eller egen kjerne på Pi OS) |
| `GPIOZERO_PIN_FACTORY=lgpio` | backlight 0,5–1,1 s | 0,3–1 s | nei |
| `.pyc`-prekompilering + ett python-tre | imports 2,7 s | 0,5–1 s (M) | nei |
| Splash via `spidev` før PIL importeres | flytter splash tidligere | opplevd, ~2 s | nei |
| Raskere A2-SD-kort | imports **og** trolig sysinit | (M) — varm/kald-gapet sier den finnes | nei |

### 8.4 Buildroot-mål

| Fase | Pi OS (målt) | Buildroot-mål | Kommentar |
|---|---|---|---|
| Kjerne | **3,94 s (målt)** | ~2–2,5 s (M) | nær gulvet allerede — liten gevinst |
| Userspace til sysinit | **7,8 s (målt)** | ~2–3 s (M) | ingen FAT-fsck i kjeden, færre units |
| vibb-ui til READY | 6,2 s (målt) | ~3–4 s (M) | pin factory, .pyc, squashfs-zstd |
| **Sum boot-to-UI** | **~20 s (målt)** | **~7–12 s** | ~2–3× — ikke 3–4× som tidligere anslått |

**Boot-to-audio (tillegg etter UI — radio/nett dominerer, uansett OS):**

| Sti | Tillegg | Kommentar |
|---|---|---|
| Innebygd høyttaler + cachet innhold | +1–3 s | ingen nett; daemon resumer cachet uten network |
| BT-høyttaler + cachet | +4–15 s (M) | bluetoothd+bluealsa ~3–5 s; page/reconnect er enhetsavhengig |
| Spotify | +6–15 s (M) | fw-last, assoc+DHCP, DNS-gate, sesjon — uendret av Buildroot |

Ærlig konklusjon, revidert mot målingene: boksen er allerede raskere enn
spec-ens første anslag (20 s, ikke 25–35 s), og app-tiltakene alene tar
den til ~17 s. Den store posten ligger i 14,1 s-blokka før UI-en — og
**hvor mye av den som er Buildroot-eksklusiv er ennå ikke målt** (§8.2
pkt. 3). Det er det ene tallet som avgjør om dette er et
app-optimaliseringsprosjekt eller et OS-prosjekt.
## 9. Migrering og risiko

**Fase 0 — på Pi OS, før Buildroot bygges (kan starte nå, lav risiko):**
kodeendringene §5.1–5.7 + felles env-fil + `/data`-layout som bind
mounts på dagens OS. Hele tilstandsflyttingen felt-verifiseres i
produksjon *før* OS-byttet — Buildroot-migreringen blir da et rent
OS-bytte, ikke OS + tilstandsflytting samtidig. Også nyttig i seg selv
(rfid-hardkodingen er en latent backup-bug i dag).

**Fase 1 — Buildroot-image, funksjonell paritet (diskuterbar/bygbar
alene):** `br2-vibb/`-treet, 32-bit `raspberrypizero2w_defconfig`-basis
(RAM-argumentet: ~430 MB brukbart, OOM dreper musikk — men se §10.1),
trimmet systemd, ro squashfs + /data, egen kernel-defconfig (§4-listen),
NM beholdt med full eksplisitt pakkeliste, pisugar-server
krysskompilert, go-librespot som egen pakke (ev. prebuilt som
bootstrap), restic/rclone-pakker, PN532-omskriving, HW-watchdog PÅ,
app-kanal, factory reset, reflash som oppdateringsvei. Gates fra §1b
må passere før Pi OS-riggen pensjoneres.

**Fase 2:** RAUC A/B over tryboot, restore-flyt i setup-portalen,
ev. pisugar-python-shim, ev. iwd-eksperimentet, ev. Sonos-pakking om
nedskopet i fase 1.

**Risikoregister (topp 5):**
1. install.sh-avskriften — 1260 linjer felt-lærte quirks; størst
   regresjonsflate for BT/ALSA-feilmodusene. Mitigering: linje-for-
   linje-migrering + gatene i §1b + Pi OS-referanserigg.
2. Firmware-dekning for BCM43436 (.hcd!) — stille "ingen BT"-klasse.
   Verifiseres først av alt i fase 1.
3. PyGObject/gobject-introspection-bygget (host-qemu-mekanikken) —
   uten den mister vi btwatchd/Agent1/MPRIS. Prioriteres tidlig;
   fallback er dbus-fast-port (2–4 dager, må re-passere bt_parity).
4. Lydkjede-paritet (hifiberry-navn, bluealsa-flagg, mpv-flagg mot
   16 kHz-bøker) — nøyaktig rørene som ga stillhets-buggen 2026-07-27.
   Felttestes, antas ikke.
5. tryboot/RAUC-backends DIY-natur (fase 2) — strømkutt midt i
   slot-bytte må testes mekanisk mange ganger.

**Lisens/juss-notat:** repoet er MIT, men et flashbart image
distribuerer GPL/LGPL-komponenter — `make legal-info` inn i
release-flyten, og ffmpeg-flagg holdes GPL-kompatible (§4s TLS-regel).
storytel.py bærer en innebygd AES-nøkkel og go-librespot er
Spotify-ToS-gråsone: images holdes private/til egne bokser, eller
publiseres app-frie.

## 10. Åpne spørsmål til diskusjon

1. **32 vs 64 bit?** Anbefalt 32-bit (RAM: 512 MB er dimensjonerende;
   32-bit krymper python-heaps og Go-binærer). Motargument: all
   felterfaring er samlet på arm64, og gpiozero-pakken må patches for
   64-bit (`depends on BR2_arm` — triviell). `raspberrypizero2w_64_defconfig`
   finnes. Valget låser også hvilke prebuilt-binærer (go-librespot-asset)
   som passer.
2. **PiSugar: cargo-pakke (a) eller python-I2C-shim (c)?** (a) er
   konservativ og anbefalt for fase 1; (c) fjerner en daemon
   (prioritet 2) men re-implementerer kalibrert kurve. Bytte senere er
   billig siden :8423-protokollen er sømmen.
3. **Sonos i fase 1-imaget?** soco+requests må vendors (~flere pakker)
   for en opt-in-sidecar. Nedskopes den til fase 2, eller er Sonos
   i daglig bruk?
4. **`hat-audio-on/off`** (config.txt-toggle for HAT-DAC): bake to
   image-varianter, eller beholde toggle med kort
   `mount -o remount,rw /boot`? (Sistnevnte er minst arbeid og
   FAT-skrivevinduet er lite.)
5. **Skal `media/` (uerstattelig brukeropplastet innhold) inn i
   backupen** som opt-in restic-tag, eller forbli "forelderen eier
   originalen"? Størrelse vs uerstattelighet.
6. **Backup-schema 2:** boksnavn (sterkt ønskelig — token-per-origin)
   og ev. wifi-profiler inn i restore-whitelisten?
7. **RAUC eller swupdate** for fase 2? (Anbefalt: RAUC; beslutt raskt,
   ikke utred lenge.)
8. **Recovery-nødvei:** boot-partisjons-hook for wifi-credentials +
   `g_serial` bak flagg — nok? Eller vil eieren ha g_ether alltid
   tilgjengelig (koster litt kernel + angrepsflate)?
9. **yt-dlp/YouTube:** bekreft at bortfall av "ukjent URL"-passthrough
   er akseptabelt i v1 (ingenting i kort/bibliotek-flyten produserer
   slike i dag).
10. **vcgencmd:** egen rpi-utils-pakke, eller sysfs-omskriving av de
    to bruksstedene (bt.py-undervolt-evidens + power.sh-status)?
11. **Brukermodell:** i dag root for vibb-units + `pi`-bruker for
    go-librespot (audio,bluetooth-grupper). Buildroot har ingen
    pi-bruker — root-only, eller dedikert bruker via
    `BR2_ROOTFS_USERS_TABLES`? (Avgjør eierskap på /data/go-librespot
    og bluealsa D-Bus-policy — feil her gir "Spotify stum, mpv virker".)
12. **maxcpus=2 og ACT-LED-polaritet:** begge er må-måles på rigg før
    de bakes inn (maxcpus kan være strøm-negativ via race-to-idle;
    feil LED-polaritet = LED fast på).
13. **CI/builder:** hvor bygges imaget (1–3 t kaldt)? GitHub Actions
    m/ ccache-cache, eller self-hosted? Uten en produsent finnes ingen
    app-kanal-artefakter heller. (Repoet har ingen `.github/` i dag.)
14. **openssl-CLI vs ~40 linjer ren-python AES** i storytel.py —
    CLI-en er gratis når `LIBOPENSSL_BIN` uansett vurderes; ren python
    fjerner en binæravhengighet. Lav prioritet.
15. **Buildroot-release-pinning:** LTS (eldre host-go, rpi-userland
    finnes ennå) vs master-nær (go 1.26, men rpi-userland borte)?
    Valget påvirker §10.10 og go-librespot-bygget.
