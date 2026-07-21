# clickwheel — wheel reader

`click.c` reads the original Apple click wheel on Pi GPIO and forwards
events to `../src/podui.py` over UDP `127.0.0.1:9090`.

**This is a DRAFT** — untested against a real wheel. The protocol and pins
are researched ([../RESEARCH.md](../RESEARCH.md) §1, [../HARDWARE.md](../HARDWARE.md)),
but bit order / edge / button bitfield must be confirmed on your actual
wheel (Synaptics vs Cypress differ — see §1).

## Build
```
sudo apt install pigpio
gcc -Wall -pthread -o click click.c -lpigpio -lrt
```

## Run / debug
```
# terminal 1: watch raw events (idx, state, pos) as they arrive
python3 - <<'PY'
import socket
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.bind(("127.0.0.1",9090))
while True:
    d,_=s.recvfrom(8); print(list(d))
PY

# terminal 2: run the reader (DMA needs root)
sudo ./click
```
Turn the wheel slowly: you should see `[255, 0, <pos>]` scroll events with
`pos` walking 0x00..0xBE. Press buttons: `[idx, 1, pos]` on press,
`[idx, 0, pos]` on release.

## Event wire format
3 bytes per UDP datagram:
| byte | meaning |
|---|---|
| 0 | button index (0=Center 1=Menu 2=Play 3=Prev 4=Next) or `0xFF` = scroll |
| 1 | state: 1=press, 0=release (0 for scroll) |
| 2 | wheel position `0x00..0xBE` |

## If you read garbage
1. Flip `BIT_ORDER_LSB` (Cypress LSB-first ↔ Synaptics MSB-first).
2. Change the sampled edge in `on_clock_edge` (falling ↔ rising).
3. Confirm `FRAME_HEADER` (`0x35` Cypress; Synaptics frames are
   `0x1a`-delimited — you may need to resync on that byte instead).
4. Check the FFC seating and that DATA has a pull-up (open-drain line).

See RESEARCH §1 for the two controller lineages and their framing.
