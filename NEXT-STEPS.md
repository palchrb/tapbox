# TapBox — mulige neste steg (backlog)

Ideer som er vurdert men ikke besluttet/bygget. Ikke en forpliktelse — en
liste å plukke fra. Nyeste øverst. Hver post: hva, hvorfor, hvordan,
kostnad/risiko, anbefaling.

---

## Stabile per-høyttaler PCM-navn (i stedet for ett omdefinert alias)

**Hva:** Én uforanderlig ALSA-blokk per paret høyttaler
(`pcm.tapbox_bt_2cfdb35b1cba` osv.) i stedet for dagens ene `tapbox_bt`
som får definisjonen sin omskrevet ved hvert bytte. Å bytte høyttaler =
åpne et annet *navn*, ikke endre hva et navn betyr. `tapbox_bt` kan bestå
som kompat-alias.

**Hvorfor:** Dagens mønster koder «hvilken høyttaler» som global,
muterbar tilstand — det var det som ga stillhets-buggen 2026-07-27
(prosess-cachet ALSA-konfig løste `tapbox_bt` til den forlatte bilens
MAC; total stillhet til manuell restart). Fork-fiksen
(snd_config_update_free_global i setupPcm, v0.1.5) reparerer symptomet,
men med stabile navn blir stale-oppløsning *strukturelt* umulig, to
prosesser kan aldri være uenige om hva som gjelder, og bytter mellom
kjente enheter virker på hvilken som helst binær — ekte
belt-and-suspenders. Foreslått av librespot-fork-agenten; enig.

**Hvordan (designkravet som avgjør om det virker):** Blokkene må
akkumuleres **ved paring**, ikke ved bytte. Et navn som legges til i
asound.conf etter at go-librespot startet finnes ikke i prosessens
config-snapshot — å åpne det ukjente navnet feiler like hardt som
stale-aliaset. Skrevet ved paring ligger alle kjente høyttalere i
boot-snapshotet, og bytte mellom dem trenger aldri refresh; kun
første-gangs-paring av en helt ny enhet trenger restart/refresh (og
paring er uansett en tung, sjelden flyt). `forget` prunser blokken.

**Berører:** `bt.py` (rewrite → akkumulering + prune), `output.py`
(pcm-navn per MAC), OUT_FILE-konsumentene, btwatchd-announce,
mpv-retargeting (`alsa/<navn>`), testene deres.

**Kostnad/risiko:** Middels — det er nøyaktig rørleggingen som nettopp
har vært gjennom stillhets-bug + swap-guard + fork-fiks. Å endre
arkitekturen der før v0.1.5-tilstanden er felt-verifisert blander to
eksperimenter: er noe stille i bilen etterpå, vet vi ikke hvilken
endring som gjorde det.

**Anbefaling:** Gjør det — men først etter at v0.1.5 er ute, swap-guarden
er slettet og Skoda↔JBL-bytte er verifisert i felt noen dager (ny MAC i
`Getting BlueALSA PCM`-loggen uten restart). Da som egen, rolig endring.

---

## ✅ Rename BT-enheter fra PWA-en — LEVERT (bygget via Alias, som foreslått)

Bygget: `POST /bt/rename {mac, name}` → `bt.py rename` → `btbus.set_alias`
(BlueZ `Device1.Alias`); «Rename»-knapp per enhet i PWA-en; navnet vises i
PWA-lista og på device-skjermen uten visningsendringer; tomt navn
tilbakestiller til fabrikknavnet; navnet saniteres (printbart, én linje,
maks 64). Gated av `tests/bt_rename.py`. Detaljene under står igjen som
referanse for hvordan/hvorfor.

**Hva:** La brukeren gi en BT-høyttaler/headset et eget navn (f.eks. «Bilen»,
«Barnerommet») i PWA-en. Navnet skal vises både i PWA-lista og på
device-settings-skjermen.

**Hvorfor:** Fabrikknavn («JBL JR310BT», «Car Multimedia») er kryptiske. Et
eget navn gjør det tydelig hvilken høyttaler man velger — spesielt når
flere er paret.

**Hvordan (anbefalt: BlueZ `Alias`):**
Alle navn i systemet kommer allerede fra BlueZ `Alias` (faller tilbake til
`Name`) — se `btbus.py` (`Alias or mac`) og `bt.py`. `Device1.Alias` er en
*skrivbar* D-Bus-property. Setter vi den, flyter det egendefinerte navnet
automatisk til **både** `GET /bt` → PWA-lista **og** device-settings-skjermen
(`ui.py:1012/1015` leser samme `d["name"]`) — null endring i visningskoden.
- `btbus.py`: `set_alias(mac, name)` → skriv `org.bluez.Device1.Alias`.
- `daemon.py`: `POST /bt/rename {"mac", "name"}` (tom streng = tilbakestill;
  BlueZ setter da `Alias` tilbake til enhetens ekte `Name` av seg selv).
- `pi/web/`: en liten «rediger navn»-affordans per enhetsrad (`#bt-devices`).
- Persistens: `Alias` lagres i `/var/lib/bluetooth` og overlever reboot.

**Kostnad/risiko:**
- Rename *skjer* kun fra PWA (der man kan skrive). Device-skjermen med 4
  knapper **viser** det nye navnet, men egner seg ikke til tekstinntasting —
  hold rename PWA-only.
- CLI-backenden (`bluetoothctl`) har ingen god `set-alias`-kommando; skriv
  alltid via D-Bus (btbus) uansett listing-backend. Auto-backend er dbus, så
  dette er greit i praksis.
- Hvis BT-storage tørkes (re-paring/re-install) kan aliaset gå tapt — mindre
  irritasjon, ikke tap av funksjon.

**Alternativ (backend-agnostisk):** egen `bt-names.json` (mac→navn) som
overlays i API/UI. Uavhengig av BlueZ-skriving og helt under vår kontroll,
men må flettes inn på hvert listing-punkt og mister BlueZ sin «tom =
tilbakestill»-oppførsel. Passer tapbox-filosofien «eig din egen state», men
er litt mer kode enn Alias-veien.

**Anbefaling:** Bygg via `Alias` — minst kode, navnet vises overalt gratis.
Lite prosjekt (~en økt). God kandidat å ta neste gang.

---

## Sømløst bytte BT ↔ built-in uten å restarte go-librespot — *stort*

**Hva:** Bytte lyd-utgang mellom Bluetooth og innebygd høyttaler uten den
2–4 s pausen som restart av go-librespot gir i dag.

**Hvorfor:** go-librespot sin `audio_device` er en *oppstartsinnstilling* —
den re-åpner aldri en annen enhet i drift. Å flytte utgang krever derfor
config-rewrite + restart, og restarten dropper hele Spotify Connect-sesjonen
(re-auth, last spor på nytt, resume fra bookmark) — *det* er det man merker.
mpv-siden er allerede sømløs fordi mpv støtter `set_property audio-device`
live; asymmetrien er ren go-librespot-begrensning.

**Hvordan:** Legg et *rutingslag* mellom go-librespot og de fysiske
enhetene, så go-librespot alltid peker på én fast mellomstasjon og vi bytter
hva den sender til — uten at go-librespot merker noe:
- **snd-aloop + alsaloop-bro (lettest, ingen full lydserver):**
  go-librespot → ALSA loopback; en liten kopi-prosess flytter lyden fra
  loopbacken til gjeldende ekte enhet (bluealsa PCM eller built-in). Bytte =
  re-pek den billige broen, ikke restart go-librespot. Sesjonen lever.
- **PipeWire/PulseAudio virtuell sink:** go-librespot → virtuell sink;
  `move-sink-input` til BT/built-in live. «Den normale» Linux-måten.

**Kostnad/risiko:** Begge legger til en komponent i en bevisst minimal
stack på Zero 2 W (direkte ALSA + bluealsa ble valgt for robusthet). Mer
CPU, litt latency, xrun-risiko under last, enda en prosess å overvåke og
reparere. Reell skjørhetskostnad på akkurat denne boksen.

**Anbefaling:** *Utsett.* Output-bytte er en sjelden, bevisst handling
(~2–4 s), og den ufrivillige varianten (flappende høyttaler → restart-storm)
er allerede dempet (commit `4bb107b`/`b81aada`). Ikke verdt permanent mer
skjørhet for å gjøre et sjeldent bytte sømløst — med mindre bruk viser at
man bytter ofte. Da er aloop-broen det letteste førstevalget; skisser den
før bygging.

---

## go-librespot-forken: ikke-blokkerende API — *middels, målrettet*

**Hva:** Forkens HTTP-API blokkerer mens spilleren laster et spor (audio-key
+ CDN + PCM-åpning): /status, /player/next og put-state svarer ikke før
lastingen er ferdig (1–19 s målt i felt 2026-07-18). Hele klassen av
kontroll-timeouts, «tom sesjon»-blipper og treg skip-kvittering stammer fra
dette ene kvelepunktet.

**Hvordan:** I forken (Go): server /status fra minnetilstand (siste kjente
spor + en `loading`-markør) og legg kontrollkommandoer i kø med umiddelbar
kvittering, i stedet for å la HTTP-handleren vente på spilleløkka.

**Gevinst:** Skip kvitterer øyeblikkelig uansett CDN-vær; tapbox-lagets
timeout-vern (busy-drops, timeout-hold, empty-recheck) blir sovende
sikkerhetsnett i stedet for daglig brukte krykker.

**Kostnad/risiko:** Go-endring i forken + rebase-vedlikehold. Middels.
Verdt det når skip-følelsen er neste prioritet.

---

## Extras-krok: generisk «start eget script»-meny (RetroPie-caset) — *BYGGET (d84e6b7)*

Shippet 2026-07-28 med eierens valg: X+Y-chord (ikke settings-rad),
ingen timeout, daemon oppe under extras. Gjenstår kun felttest på
boksen + at eieren skriver sitt RetroPie-script etter docs/extras.md.

**Hva:** `/etc/tapbox/extras/` (root-eid; install.sh oppretter, rører aldri
innholdet). En extra = kjørbar, root-eid fil (UI nekter andre — barne-
skrivbar fil ville vært rett-til-root); navn fra `# tapbox-name:`-header.
Settings viser «Extras»-rad KUN når katalogen har innhold, bak confirm-gate.
Ingen HTTP-rute — SSH legger inn, fysisk skjerm starter (SECURITY-linjen:
maksimal overlevering skal ikke kunne fjernutløses).

**Overlevering:** ui.py eier SPI+GPIO og må selv dø — extraen startes derfor
som transient systemd-enhet (`systemd-run`) med wrapper
`/usr/local/bin/tapbox-extra`: stopp tapbox-ui/-idle/-buttons + avspilling
(frigjør I2S), kjør scriptet, gjenopprett. Daemon/API blir stående.
**Retur-garanti (QA-blokker): gjenoppretting i `ExecStopPost=` på den
transiente enheten** (kjører uansett hvordan wrapperen dør — SIGKILL
inkludert), trap som belte. Lavbatteri-poweroff forblir aktiv; PiSugar-
knappen er nødutgang. Ingen hard timeout (avklart med eier? — åpen).

**Gate-tester før shipping:** wrapper-modell gjennom exit-0/krasj/aldri-
avslutter/drept-wrapper (alle må gjenopprette + slippe idle-stoppen),
scope-vakt (ingen rute eller opplastingsendelse når /etc/tapbox/extras),
install-idempotens over full katalog, UI: skjult ved tom katalog + confirm.

**Docs (aldri produktkode):** docs/extras.md med RetroPie-eksempelscript —
fbcp-speiling for ST7789 (ikke en framebuffer), USB-gamepad via OTG
(4 knapper holder ikke), ALSA-kortvalg, opprydding.

**Åpne eierbeslutninger:** hard timeout (anbef. nei), daemon oppe under
extras (anbef. ja), confirm-gate vs hold-A-barnesikring.
