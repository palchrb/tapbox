# TapBox — mulige neste steg (backlog)

Ideer som er vurdert men ikke besluttet/bygget. Ikke en forpliktelse — en
liste å plukke fra. Nyeste øverst. Hver post: hva, hvorfor, hvordan,
kostnad/risiko, anbefaling.

---

## Rename BT-enheter fra PWA-en (egendefinert navn) — *lett*

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
