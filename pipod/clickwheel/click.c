/*
 * pipod click wheel reader — DRAFT (untested against real hardware).
 *
 * Reads an original Apple 4th-gen / iPod Photo click wheel directly on Pi
 * GPIO using pigpio's DMA-sampled edge callbacks, decodes the 32-bit
 * (4-byte) packet, and forwards scroll/button events to podui.py over UDP
 * localhost:9090 as 3 bytes: [button_idx, button_state, wheel_pos].
 *
 * Derived from the approach in dupontgu/retro-ipod-spotify-client
 * (clickwheel/click.c). See ../RESEARCH.md §1 for the protocol.
 *
 * Pins (BCM):  CLOCK=23  DATA=25  HAPTIC=26   (see ../HARDWARE.md)
 * Wheel frame (Cypress CY8C21434):
 *   byte0 = 0x35 header
 *   byte1 = button bitfield (Menu/Play/Prev/Next/Center)
 *   byte2 = wheel position 0x00..0xBE (96 steps around the ring)
 *   byte3 = touch flag (0x00 none / 0x80 finger present)
 * If your wheel is a Synaptics T1005 you'll see a different framing
 * (0x1a-delimited, MSB-first) — flip BIT_ORDER_LSB / the sample edge.
 *
 * Build:  gcc -Wall -pthread -o click click.c -lpigpio -lrt
 * Run:    sudo ./click        (DMA needs root, or a running pigpiod)
 */

#include <pigpio.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <arpa/inet.h>
#include <sys/socket.h>

#define PIN_CLOCK   23
#define PIN_DATA    25
#define PIN_HAPTIC  26

#define UDP_HOST    "127.0.0.1"
#define UDP_PORT    9090

#define FRAME_HEADER 0x35
#define BIT_ORDER_LSB 1   /* Cypress: LSB-first, read on falling edge.
                             Set 0 for a Synaptics (MSB-first) wheel. */

/* Button bit positions within byte1 (verify against your wheel). */
enum { BTN_CENTER = 0, BTN_MENU, BTN_PLAY, BTN_PREV, BTN_NEXT, BTN__N };

static int   udp_fd = -1;
static struct sockaddr_in udp_dst;

/* --- packet assembly state, updated inside the clock-edge callback --- */
static volatile uint32_t frame = 0;   /* shifting 32-bit packet */
static volatile int      nbits = 0;   /* bits seen this frame   */

static uint8_t last_buttons = 0;      /* for edge-detecting presses */
static uint8_t last_pos      = 0;
static int     have_pos      = 0;

static void send_event(uint8_t idx, uint8_t state, uint8_t pos) {
    uint8_t pkt[3] = { idx, state, pos };
    if (udp_fd >= 0)
        sendto(udp_fd, pkt, sizeof pkt, 0,
               (struct sockaddr *)&udp_dst, sizeof udp_dst);
}

/* Short haptic tick on a confirmed click. Best-effort. */
static void haptic_tick(void) {
    gpioWrite(PIN_HAPTIC, 1);
    gpioDelay(12000);              /* ~12 ms */
    gpioWrite(PIN_HAPTIC, 0);
}

static void decode_frame(uint32_t f) {
    uint8_t b0 =  f        & 0xFF;
    uint8_t b1 = (f >>  8) & 0xFF;
    uint8_t b2 = (f >> 16) & 0xFF;
    /* uint8_t b3 = (f >> 24) & 0xFF;  // touch flag, unused for now */

    if (b0 != FRAME_HEADER)        /* not a valid packet — resync */
        return;

    uint8_t buttons = b1;
    uint8_t pos     = b2;

    /* Button edges: report press (1) and release (0). */
    for (int i = 0; i < BTN__N; i++) {
        uint8_t now  = (buttons >> i) & 1;
        uint8_t prev = (last_buttons >> i) & 1;
        if (now != prev) {
            send_event((uint8_t)i, now, pos);
            if (now && i == BTN_CENTER) haptic_tick();
        }
    }
    last_buttons = buttons;

    /* Wheel motion: emit a synthetic "scroll" event (idx 0xFF) carrying the
       new absolute position; podui.py turns delta into up/down + accel. */
    if (!have_pos) { last_pos = pos; have_pos = 1; }
    else if (pos != last_pos) {
        send_event(0xFF, 0, pos);
        last_pos = pos;
    }
}

/* pigpio alert: fires on every sampled edge of the wheel's clock line.
   The wheel drives the clock; we sample DATA on the active edge. */
static void on_clock_edge(int gpio, int level, uint32_t tick) {
    (void)gpio; (void)tick;
    /* level: 0=falling, 1=rising, 2=watchdog(no edge -> frame gap) */
    if (level == 2) {              /* idle gap: a packet boundary */
        if (nbits == 32) decode_frame(frame);
        frame = 0; nbits = 0;
        return;
    }

    /* Cypress reads DATA on the falling edge (CPHA=0, CPOL=1). */
    int want = BIT_ORDER_LSB ? 0 /*falling*/ : 1 /*rising*/;
    if (level != want) return;

    int bit = gpioRead(PIN_DATA) & 1;
    if (BIT_ORDER_LSB)
        frame = (frame >> 1) | ((uint32_t)bit << 31);
    else
        frame = (frame << 1) | (uint32_t)bit;
    if (nbits < 32) nbits++;
}

int main(void) {
    /* 1 us DMA sample rate is the known fix for catching the wheel's
       ~9 us clock pulses reliably (RESEARCH §1). */
    gpioCfgClock(1 /*us*/, 1 /*PCM*/, 0);
    if (gpioInitialise() < 0) {
        fprintf(stderr, "click: pigpio init failed (run as root?)\n");
        return 1;
    }

    gpioSetMode(PIN_CLOCK, PI_INPUT);
    gpioSetMode(PIN_DATA,  PI_INPUT);
    gpioSetPullUpDown(PIN_CLOCK, PI_PUD_UP);
    gpioSetPullUpDown(PIN_DATA,  PI_PUD_UP);   /* DATA is open-drain */
    gpioSetMode(PIN_HAPTIC, PI_OUTPUT);
    gpioWrite(PIN_HAPTIC, 0);

    udp_fd = socket(AF_INET, SOCK_DGRAM, 0);
    memset(&udp_dst, 0, sizeof udp_dst);
    udp_dst.sin_family = AF_INET;
    udp_dst.sin_port   = htons(UDP_PORT);
    inet_pton(AF_INET, UDP_HOST, &udp_dst.sin_addr);

    /* Watchdog: if the clock is silent for 2 ms, treat it as a frame gap
       so a half-received packet doesn't merge into the next one. */
    gpioSetWatchdog(PIN_CLOCK, 2 /*ms*/);
    gpioSetAlertFunc(PIN_CLOCK, on_clock_edge);

    fprintf(stderr, "click: reading wheel on BCM%d/%d -> udp %s:%d\n",
            PIN_CLOCK, PIN_DATA, UDP_HOST, UDP_PORT);

    /* pigpio runs the callback on its own thread; idle here. */
    for (;;) gpioDelay(1000000);

    gpioTerminate();
    return 0;
}
