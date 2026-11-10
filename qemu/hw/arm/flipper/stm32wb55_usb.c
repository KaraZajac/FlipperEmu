/*
 * STM32WB55 USB full-speed device controller, with a built-in virtual host
 *
 * Device side (RM0434, "Universal serial bus full-speed device interface"),
 * modelled as far as the firmware's driver depends on it:
 * lib/libusb_stm32/src/usbd_stm32wb55_devfs.c in the Flipper tree.
 *  - EPnR: STAT_RX/STAT_TX/DTOG_RX/DTOG_TX toggle when written with 1,
 *    CTR_RX/CTR_TX are rc_w0, SETUP is read-only, EA/EP_TYPE/EP_KIND are
 *    plain fields.  A bus reset clears everything but the CTR bits.
 *  - ISTR: CTR, DIR and EP_ID come from the endpoint registers, the event
 *    flags are rc_w0, and CNTR masks all of it onto USB_LP.
 *  - BTABLE and 1 KiB of packet memory.  The buffer table holds {ADDR_TX,
 *    COUNT_TX, ADDR_RX, COUNT_RX} per endpoint as 16-bit words.
 *  - Double-buffered bulk endpoints: the USB side uses the buffer that DTOG
 *    selects and software the one SW_BUF selects; the endpoint NAKs while
 *    the two are equal.  On an IN endpoint STAT_TX reads NAK in that state
 *    and VALID otherwise, which ep_write() depends on.  An OUT endpoint
 *    keeps STAT_RX VALID, which ep_read() depends on.
 *  - BCDR: charger detection reports a standard PC port while the virtual
 *    cable is attached.  DPPU is the D+ pull-up, i.e. "connect".
 *
 * Two liberties keep the host (which runs in QEMU's main loop) from racing
 * the firmware's read-modify-write sequences.  The CPU clears a CTR or ISTR
 * event bit only if its last read of that register showed the bit set,
 * which makes the RMW atomic.  The host also leaves an endpoint alone while
 * one of its CTR bits is pending.
 *
 * Host side: QEMU has no USB wire leaving the machine, so a small host
 * lives here.  When the firmware pulls D+ up while the virtual cable is
 * attached, it signals suspend, resume and bus reset the way a PC does.  It
 * then enumerates whatever the firmware presents (descriptors, SET_ADDRESS,
 * SET_CONFIGURATION).  For a CDC ACM function (the Flipper CLI and protobuf
 * RPC) it bridges the bulk endpoints to a character device, and raises DTR
 * while a client is connected to that chardev.  For a HID function (Bad USB,
 * the USB remote) it plays the PC: it polls the interrupt endpoint and writes
 * what the keyboard types to the same chardev, as text.
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/cutils.h"
#include "qemu/log.h"
#include "qemu/timer.h"
#include "qemu/error-report.h"
#include "qapi/error.h"
#include "hw/core/cpu.h"
#include "hw/core/irq.h"
#include "hw/core/qdev-properties.h"
#include "hw/core/qdev-properties-system.h"
#include "wb55.h"

/* ---- EPnR */
#define EP_CTR_RX   0x8000
#define EP_DTOG_RX  0x4000
#define EP_STAT_RX  0x3000
#define EP_SETUP    0x0800
#define EP_TYPE     0x0600
#define EP_KIND     0x0100
#define EP_CTR_TX   0x0080
#define EP_DTOG_TX  0x0040
#define EP_STAT_TX  0x0030
#define EP_EA       0x000F
#define EP_CTR      (EP_CTR_RX | EP_CTR_TX)
#define EP_TOGGLE   (EP_DTOG_RX | EP_STAT_RX | EP_DTOG_TX | EP_STAT_TX)
#define EP_SWBUF_TX EP_DTOG_RX      /* double-buffered IN:  SW_BUF */
#define EP_SWBUF_RX EP_DTOG_TX      /* double-buffered OUT: SW_BUF */

#define EPT_BULK    0x0000
#define EPT_CONTROL 0x0200
#define EPT_ISO     0x0400

enum { STAT_DIS = 0, STAT_STALL = 1, STAT_NAK = 2, STAT_VALID = 3 };
#define STAT_RX(r)  (((r) >> 12) & 3)
#define STAT_TX(r)  (((r) >> 4) & 3)
#define IS_DBLBULK(r) (((r) & (EP_TYPE | EP_KIND)) == (EPT_BULK | EP_KIND))

/* ---- other registers */
#define R_CNTR      0x40
#define R_ISTR      0x44
#define R_FNR       0x48
#define R_DADDR     0x4C
#define R_BTABLE    0x50
#define R_LPMCSR    0x54
#define R_BCDR      0x58

#define CNTR_FRES   0x0001
#define CNTR_PDWN   0x0002
#define CNTR_MASKS  0xFF80
#define ISTR_CTR    0x8000
#define ISTR_EVENTS 0x7F80          /* PMAOVR ERR WKUP SUSP RESET SOF ESOF L1REQ */
#define ISTR_WKUP   0x1000
#define ISTR_SUSP   0x0800
#define ISTR_RESET  0x0400
#define ISTR_DIR    0x0010
#define FNR_LCK     0x2000
#define FNR_RXDP    0x8000
#define DADDR_EF    0x80
#define BCDR_DPPU   0x8000
#define BCDR_DCDET  0x0010
#define BCDR_DCDEN  0x0002
#define BCDR_BCDEN  0x0001

/* ---- host timing (virtual time) */
#define MS              (1000 * 1000LL)
#define KICK_NS         (20 * 1000)     /* react 20 us after a CPU write */
#define POLL_NS         (1 * MS)        /* fallback poll while a transfer waits */
#define IDLE_POLL_NS    (10 * MS)
#define CLOSE_WAIT_NS   (500 * MS)  /* a closed port's last bytes: how long */
#define REOPEN_NS       (10 * MS)   /* from a port's hang-up to its next use */
#define CTL_TIMEOUT_NS  (500 * MS)
#define CTL_GAP_NS      (50 * 1000)     /* between control transfers */
#define EP0_SETTLE_NS   (50 * MS)       /* then send the SETUP regardless */

enum { H_DETACHED, H_DEBOUNCE, H_SUSPENDED, H_RESET, H_ENUM, H_RUNNING,
       H_FAILED };
enum { CTL_SETUP, CTL_DATA_IN, CTL_DATA_OUT, CTL_STATUS_IN, CTL_STATUS_OUT };
enum { E_DEV8, E_SETADDR, E_DEV18, E_CFG9, E_CFG, E_LANG, E_STR_MANUF,
       E_STR_PROD, E_STR_SERIAL, E_SETCFG, E_LINECODING, E_DONE };
typedef enum { TX_ACK, TX_NAK, TX_STALL, TX_NORESP } UsbTx;

#define HOST_ADDR   1               /* the address we hand out */

static void G_GNUC_PRINTF(2, 3) usb_dbg(WB55UsbState *s, const char *fmt, ...)
{
    va_list ap;

    if (!s->debug) {
        return;
    }
    va_start(ap, fmt);
    g_autofree char *msg = g_strdup_vprintf(fmt, ap);
    va_end(ap);
    info_report("wb55-usb: %s", msg);
}

static int64_t now_ns(void)
{
    return qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
}

/* ===================================================== device controller */

static uint16_t pma_r16(WB55UsbState *s, uint32_t a)
{
    a &= 0x3FE;
    return s->pma[a] | (s->pma[a + 1] << 8);
}

static void pma_w16(WB55UsbState *s, uint32_t a, uint16_t v)
{
    a &= 0x3FE;
    s->pma[a] = v;
    s->pma[a + 1] = v >> 8;
}

/* endpoint with the highest interrupt priority that has a CTR pending:
 * isochronous and double-buffered bulk first, then the lowest number */
static int usb_ctr_ep(WB55UsbState *s)
{
    for (int pass = 0; pass < 2; pass++) {
        for (int n = 0; n < 8; n++) {
            uint16_t r = s->epr[n];
            bool hp = (r & EP_TYPE) == EPT_ISO || IS_DBLBULK(r);

            if ((r & EP_CTR) && (pass || hp)) {
                return n;
            }
        }
    }
    return -1;
}

static uint16_t usb_istr(WB55UsbState *s)
{
    uint16_t v = s->istr & ISTR_EVENTS;
    int n = usb_ctr_ep(s);

    if (n >= 0) {
        v |= ISTR_CTR | n;
        if (s->epr[n] & EP_CTR_RX) {
            v |= ISTR_DIR;
        }
    }
    return v;
}

static void usb_update_irq(WB55UsbState *s)
{
    qemu_set_irq(s->irq_lp, (usb_istr(s) & s->cntr & CNTR_MASKS) != 0);
}

static void host_wake(WB55UsbState *s, int64_t when);
static void host_kick(WB55UsbState *s);

/*
 * Bus time.  A full-speed bulk transaction (token, data with its PID and
 * CRC, handshake, and the gaps between them) takes about 103 + 8n bit
 * times at 12 Mbit/s: 51 us for 64 bytes.  The host completes one no
 * sooner than that after the CPU armed the endpoint, or after the bus last
 * carried a transaction.  (Interrupt endpoints wait for the host's poll
 * every bInterval instead: Bad USB drops a report that is not taken within
 * two intervals.)
 *
 * Firmware also relies on being fast next to the bus.  The CLI's CDC
 * service (cli_vcp.c) queues an event per completed packet in an 8-entry
 * queue, from USB_LP, and its thread takes one event per pass; but the
 * same thread re-arms the endpoint from other callbacks too (the shell
 * read from its pipe; the shell wrote more).  A packet that completes
 * between those and the next pass leaves one more event queued, and nine
 * end in furi_check() in USB_LP.  On a real STM32 the pass takes a few
 * microseconds against the 51 us packet; an emulated CPU can be a hundred
 * times slower than that, and slower still on a busy host.
 *
 * So a bulk endpoint gets its next transaction when the firmware has caught
 * up with the last one: the USB interrupt is not pending and the CPU
 * sleeps in WFI, which the idle thread does when every other thread waits
 * (that service's with its queue empty).  Nothing tells the host when that
 * comes about, and every look costs the emulated CPU time when it is still
 * at work (the main loop takes the lock that its register accesses need).
 * So the host looks as long after the re-arm as that took lately (ep_idle:
 * a look that found the CPU at work lengthens it, one that found it idle
 * shortens it as much, so that it comes to the middle of them), and again
 * a little later.  At the latest the transaction comes when the firmware
 * has had twice its own recent turnaround (a packet's completion to the CPU
 * re-arming that endpoint) since the re-arm, and BUSY_MIN_NS at least: that
 * is what firmware that does not sleep gets (a thread that polls, a busy
 * loop).
 */
#define TURN_MAX_NS     (2 * MS)        /* a turnaround sample's ceiling */
#define BUSY_MIN_NS     (1 * MS)        /* the least a CPU at work is given */
#define IDLE_FIRST_NS   (200 * 1000)    /* re-arm to idle: the first guess */
#define IDLE_MIN_NS     (20 * 1000)
#define LOOK_AGAIN_NS   (50 * 1000)     /* the least between two looks */
#define LOOK_SLACK_NS   (150 * 1000)    /* a look later than this says nothing */

static int64_t usb_wire_ns(int len)
{
    return ((103 + 8 * (int64_t)len) * 1000 + 11) / 12;
}

static bool usb_is_bulk(WB55UsbState *s, int n)
{
    return (s->epr[n] & EP_TYPE) == EPT_BULK;
}

/* The firmware has dealt with all that this bus gave it */
static bool usb_caught_up(WB55UsbState *s)
{
    return !(usb_istr(s) & s->cntr & CNTR_MASKS) &&
           qatomic_read(&first_cpu->halted);
}

/* when EPn gets its transaction, whatever the CPU does */
static int64_t usb_due(WB55UsbState *s, int n)
{
    return s->ep_armed[n] + MAX(2 * s->turn_ns, BUSY_MIN_NS);
}

/* true (and the host woken when it may proceed) while the bus is busy */
static bool usb_paced(WB55UsbState *s, int n, int len)
{
    int64_t now, bus, due, look;
    bool idle;

    if (!usb_is_bulk(s, n)) {
        return false;
    }
    now = now_ns();
    bus = MAX(s->ep_armed[n], s->bus_last) + usb_wire_ns(len);
    due = MAX(bus, usb_due(s, n));
    look = MIN(MAX(bus, s->ep_look[n]), due);
    if (now < look) {
        host_wake(s, look);
        return true;
    }
    idle = usb_caught_up(s);
    /* a look that came when it was planned says how good the plan was */
    if (s->ep_timed[n] && now - look < LOOK_SLACK_NS) {
        int64_t step = s->ep_idle[n] / 8;

        s->ep_idle[n] = idle ? MAX(s->ep_idle[n] - step, IDLE_MIN_NS)
                             : MIN(s->ep_idle[n] + step, TURN_MAX_NS);
    }
    s->ep_timed[n] = false;
    if (idle || now >= due) {
        return false;
    }
    s->ep_look[n] = now + MAX(s->ep_idle[n] / 4, LOOK_AGAIN_NS);
    host_wake(s, MIN(s->ep_look[n], due));
    return true;
}

/* EPn may get a transaction from now on: when to look for an idle CPU */
static void usb_plan(WB55UsbState *s, int n, int64_t now)
{
    s->ep_armed[n] = now;
    s->ep_look[n] = now + s->ep_idle[n];
    s->ep_timed[n] = true;
}

/* a transaction on EPn completed */
static void usb_moved(WB55UsbState *s, int n)
{
    if (usb_is_bulk(s, n)) {
        int64_t now = now_ns();

        s->bus_last = s->ep_done[n] = now;
        usb_plan(s, n, now);
    }
}

/* the CPU armed EPn: after a transaction, that measures its turnaround */
static void usb_armed(WB55UsbState *s, int n)
{
    int64_t now = now_ns();

    if (s->ep_done[n]) {
        int64_t t = MIN(now - s->ep_done[n], TURN_MAX_NS);

        s->turn_ns = s->turn_ns ? (3 * s->turn_ns + t) / 4 : t;
        s->ep_done[n] = 0;
    }
    usb_plan(s, n, now);
}

/*
 * The CPU wrote a bulk endpoint's register while the host runs the device.
 * The host has nothing to do about most of these (a CTR bit cleared, a
 * buffer not yet given back); when the endpoint can take a transaction
 * after it, the host comes for it when the pacing first lets it.
 */
static void usb_bulk_written(WB55UsbState *s, int n)
{
    uint16_t r = s->epr[n];
    bool rx = STAT_RX(r) == STAT_VALID &&
              (!IS_DBLBULK(r) || !(r & EP_DTOG_RX) != !(r & EP_SWBUF_RX));

    if (STAT_RX(r) == STAT_STALL || STAT_TX(r) == STAT_STALL) {
        host_kick(s);
    } else if (!(r & EP_CTR) && (rx || STAT_TX(r) == STAT_VALID)) {
        host_wake(s, MAX(MIN(s->ep_look[n], usb_due(s, n)),
                         now_ns() + KICK_NS));
    }
}

/* A double-buffered IN endpoint reads NAK while software owns the buffer
 * the USB side would send next (DTOG_TX == SW_BUF), VALID otherwise. */
static void usb_dbl_tx_stat(WB55UsbState *s, int n)
{
    uint16_t r = s->epr[n];
    int st = STAT_TX(r);

    if (!IS_DBLBULK(r) || (st != STAT_NAK && st != STAT_VALID)) {
        return;
    }
    st = !(r & EP_DTOG_TX) == !(r & EP_SWBUF_TX) ? STAT_NAK : STAT_VALID;
    s->epr[n] = (r & ~EP_STAT_TX) | (st << 4);
}

static void usb_epr_write(WB55UsbState *s, int n, uint16_t val)
{
    uint16_t old = s->epr[n];
    uint16_t clr = ~val & s->epr_seen[n] & EP_CTR;

    s->epr[n] = (val & (EP_TYPE | EP_KIND | EP_EA))
              | ((old ^ val) & EP_TOGGLE)
              | (old & EP_CTR & ~clr)
              | (old & EP_SETUP);
    s->epr_seen[n] &= ~clr;
    usb_dbl_tx_stat(s, n);
    if (val & EP_TOGGLE) {
        usb_armed(s, n);
    }
}

static uint16_t usb_fnr(WB55UsbState *s)
{
    uint16_t fn = ((now_ns() - s->frame_base_ns) / MS) & 0x7FF;

    return fn | FNR_RXDP | (s->hstate >= H_ENUM ? FNR_LCK : 0);
}

static uint16_t usb_bcdr(WB55UsbState *s)
{
    uint16_t v = s->bcdr;

    /* data contact detection; primary/secondary detection then report a
     * standard downstream port (a PC), so PDET = SDET = PS2DET = 0 */
    if ((v & BCDR_BCDEN) && (v & BCDR_DCDEN) && s->attached) {
        v |= BCDR_DCDET;
    }
    return v;
}

static void host_pullup(WB55UsbState *s, bool on);

static uint64_t usb_read(void *opaque, hwaddr addr, unsigned size)
{
    WB55UsbState *s = opaque;
    uint16_t v;

    if (addr & 2) {
        return 0;
    }
    if (addr < 0x20) {
        int n = addr >> 2;

        s->epr_seen[n] = s->epr[n] & EP_CTR;
        return s->epr[n];
    }
    switch (addr) {
    case R_CNTR:
        return s->cntr;
    case R_ISTR:
        v = usb_istr(s);
        s->istr_seen = v & ISTR_EVENTS;
        return v;
    case R_FNR:
        return usb_fnr(s);
    case R_DADDR:
        return s->daddr;
    case R_BTABLE:
        return s->btable;
    case R_LPMCSR:
        return s->lpmcsr;
    case R_BCDR:
        return usb_bcdr(s);
    }
    qemu_log_mask(LOG_GUEST_ERROR, "stm32wb55-usb: read of 0x%02" HWADDR_PRIx
                  "\n", addr);
    return 0;
}

static void usb_write(void *opaque, hwaddr addr, uint64_t val64,
                      unsigned size)
{
    WB55UsbState *s = opaque;
    uint16_t val = val64, clr;
    bool was;

    if (addr & 2) {
        return;
    }
    if (addr < 0x20) {
        int n = addr >> 2;

        usb_epr_write(s, n, val);
        if (s->hstate == H_RUNNING && usb_is_bulk(s, n)) {
            usb_update_irq(s);
            usb_bulk_written(s, n);
            return;
        }
    } else {
        switch (addr) {
        case R_CNTR:
            s->cntr = val & 0xFFBF;
            break;
        case R_ISTR:
            clr = ~val & s->istr_seen & ISTR_EVENTS;
            s->istr &= ~clr;
            s->istr_seen &= ~clr;
            break;
        case R_DADDR:
            s->daddr = val & 0xFF;
            break;
        case R_BTABLE:
            s->btable = val & 0xFFF8;
            break;
        case R_LPMCSR:
            s->lpmcsr = val & 0x3;
            break;
        case R_BCDR:
            was = s->bcdr & BCDR_DPPU;
            s->bcdr = val & (BCDR_DPPU | 0xF);
            if (was != !!(s->bcdr & BCDR_DPPU)) {
                host_pullup(s, !was);
            }
            break;
        default:
            qemu_log_mask(LOG_GUEST_ERROR, "stm32wb55-usb: write of 0x%02"
                          HWADDR_PRIx "\n", addr);
            return;
        }
    }
    usb_update_irq(s);
    host_kick(s);
}

static const MemoryRegionOps usb_ops = {
    .read = usb_read,
    .write = usb_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 2,
    .valid.max_access_size = 8,     /* LDRD/STRD: split into two words */
    .impl.min_access_size = 2,
    .impl.max_access_size = 4,
};

static uint64_t pma_read(void *opaque, hwaddr addr, unsigned size)
{
    WB55UsbState *s = opaque;

    return ldn_le_p(&s->pma[addr], size);
}

static void pma_write(void *opaque, hwaddr addr, uint64_t val, unsigned size)
{
    WB55UsbState *s = opaque;

    stn_le_p(&s->pma[addr], size, val);
}

static const MemoryRegionOps pma_ops = {
    .read = pma_read,
    .write = pma_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 8,     /* ep_deconfig() clears an entry with STRD */
    .valid.unaligned = false,
    .impl.min_access_size = 1,
    .impl.max_access_size = 4,
};

/* ========================================================== the USB bus */

/* Is the device answering tokens sent to the host's current address? */
static bool usb_listening(WB55UsbState *s)
{
    return (s->bcdr & BCDR_DPPU) && !(s->cntr & (CNTR_FRES | CNTR_PDWN)) &&
           (s->daddr & DADDR_EF) && (s->daddr & 0x7F) == s->addr;
}

static int usb_find_ep(WB55UsbState *s, int ea)
{
    for (int n = 0; n < 8; n++) {
        if ((s->epr[n] & EP_EA) == ea) {
            return n;
        }
    }
    return -1;
}

/* buffer table slot 0 = ADDR_TX/COUNT_TX, slot 1 = ADDR_RX/COUNT_RX */
static bool usb_rx_store(WB55UsbState *s, int n, int slot,
                         const uint8_t *data, int len)
{
    uint32_t ent = s->btable + n * 8 + slot * 4;
    uint32_t addr = pma_r16(s, ent) & 0x3FE;
    uint16_t cnt = pma_r16(s, ent + 2);
    int blocks = (cnt >> 10) & 0x1F;
    int size = (cnt & 0x8000) ? (blocks + 1) * 32 : blocks * 2;

    if (len > size) {
        qemu_log_mask(LOG_GUEST_ERROR, "stm32wb55-usb: EP%d: %d-byte packet "
                      "for a %d-byte buffer\n", n, len, size);
        return false;
    }
    for (int i = 0; i < len; i++) {
        s->pma[(addr + i) & 0x3FF] = data[i];
    }
    pma_w16(s, ent + 2, (cnt & 0xFC00) | len);
    return true;
}

static int usb_tx_count(WB55UsbState *s, int n, int slot)
{
    return MIN(pma_r16(s, s->btable + n * 8 + slot * 4 + 2) & 0x3FF, 64);
}

static int usb_tx_fetch(WB55UsbState *s, int n, int slot, uint8_t *buf)
{
    uint32_t ent = s->btable + n * 8 + slot * 4;
    uint32_t addr = pma_r16(s, ent) & 0x3FE;
    int len = MIN(pma_r16(s, ent + 2) & 0x3FF, 64);

    for (int i = 0; i < len; i++) {
        buf[i] = s->pma[(addr + i) & 0x3FF];
    }
    return len;
}

/* SETUP is never NAKed or stalled by the device; it is dropped (and the
 * host retries) only while the endpoint still has an event pending. */
static UsbTx usb_setup(WB55UsbState *s, const uint8_t *pkt)
{
    int n = usb_listening(s) ? usb_find_ep(s, 0) : -1;
    uint16_t r;

    if (n < 0) {
        return TX_NORESP;
    }
    r = s->epr[n];
    if ((r & EP_TYPE) != EPT_CONTROL || STAT_RX(r) == STAT_DIS ||
        (r & EP_CTR) || !usb_rx_store(s, n, 1, pkt, 8)) {
        return TX_NORESP;
    }
    r &= ~(EP_STAT_RX | EP_STAT_TX);
    r |= (STAT_NAK << 12) | (STAT_NAK << 4) | EP_DTOG_RX | EP_DTOG_TX;
    s->epr[n] = r | EP_SETUP | EP_CTR_RX;
    usb_update_irq(s);
    return TX_ACK;
}

static UsbTx usb_out(WB55UsbState *s, int ea, const uint8_t *data, int len)
{
    int n = usb_listening(s) ? usb_find_ep(s, ea) : -1;
    uint16_t r;
    int st;

    if (n < 0) {
        return TX_NORESP;
    }
    r = s->epr[n];
    st = STAT_RX(r);
    if (st == STAT_DIS || (r & EP_TYPE) == EPT_ISO) {
        return TX_NORESP;
    }
    if (r & EP_CTR) {
        return TX_NAK;
    }
    if (st == STAT_STALL) {
        return TX_STALL;
    }
    if (st != STAT_VALID) {
        return TX_NAK;
    }
    if (IS_DBLBULK(r)) {
        bool dtog = r & EP_DTOG_RX;

        if (dtog == !!(r & EP_SWBUF_RX)) {
            return TX_NAK;           /* both buffers belong to software */
        }
        if (usb_paced(s, n, len)) {
            return TX_NAK;
        }
        if (!usb_rx_store(s, n, dtog, data, len)) {
            return TX_NORESP;
        }
        r ^= EP_DTOG_RX;
        usb_moved(s, n);
    } else {
        bool ctl = (r & EP_TYPE) == EPT_CONTROL;

        if (!ctl && usb_paced(s, n, len)) {
            return TX_NAK;
        }
        if (!usb_rx_store(s, n, 1, data, len)) {
            return TX_NORESP;
        }
        r = ((r & ~EP_STAT_RX) | (STAT_NAK << 12)) ^ EP_DTOG_RX;
        if (ctl) {
            r &= ~EP_SETUP;
        } else {
            usb_moved(s, n);
        }
    }
    s->epr[n] = r | EP_CTR_RX;
    usb_update_irq(s);
    return TX_ACK;
}

/* the device has a packet for the host on this endpoint */
static bool usb_in_armed(WB55UsbState *s, int ea)
{
    int n = usb_listening(s) ? usb_find_ep(s, ea) : -1;
    uint16_t r;

    if (n < 0) {
        return false;
    }
    r = s->epr[n];
    if (STAT_TX(r) != STAT_VALID || (r & EP_TYPE) == EPT_ISO) {
        return false;
    }
    return !IS_DBLBULK(r) || !!(r & EP_DTOG_TX) != !!(r & EP_SWBUF_TX);
}

static UsbTx usb_in(WB55UsbState *s, int ea, uint8_t *buf, int *plen)
{
    int n = usb_listening(s) ? usb_find_ep(s, ea) : -1;
    uint16_t r;
    int st;

    if (n < 0) {
        return TX_NORESP;
    }
    r = s->epr[n];
    st = STAT_TX(r);
    if (st == STAT_DIS || (r & EP_TYPE) == EPT_ISO) {
        return TX_NORESP;
    }
    if (r & EP_CTR) {
        return TX_NAK;
    }
    if (st == STAT_STALL) {
        return TX_STALL;
    }
    if (st != STAT_VALID) {
        return TX_NAK;
    }
    if (IS_DBLBULK(r)) {
        bool dtog = r & EP_DTOG_TX;

        if (dtog == !!(r & EP_SWBUF_TX)) {
            return TX_NAK;
        }
        if (usb_paced(s, n, usb_tx_count(s, n, dtog))) {
            return TX_NAK;
        }
        usb_moved(s, n);
        *plen = usb_tx_fetch(s, n, dtog, buf);
        s->epr[n] = (r ^ EP_DTOG_TX) | EP_CTR_TX;
        usb_dbl_tx_stat(s, n);
    } else {
        if ((r & EP_TYPE) != EPT_CONTROL) {
            if (usb_paced(s, n, usb_tx_count(s, n, 0))) {
                return TX_NAK;
            }
            usb_moved(s, n);
        }
        *plen = usb_tx_fetch(s, n, 0, buf);
        r = ((r & ~EP_STAT_TX) | (STAT_NAK << 4)) ^ EP_DTOG_TX;
        s->epr[n] = r | EP_CTR_TX;
    }
    usb_update_irq(s);
    return TX_ACK;
}

/* RESET signalling: endpoint registers (except CTR) and the address are
 * cleared; the device was suspended, so it also sees a wakeup. */
static void usb_bus_reset(WB55UsbState *s)
{
    for (int n = 0; n < 8; n++) {
        s->epr[n] &= EP_CTR;
    }
    s->daddr = 0;
    s->addr = 0;
    s->frame_base_ns = now_ns();
    s->istr |= ISTR_RESET | ISTR_WKUP;
    usb_update_irq(s);
}

/* ============================================================ the host */

static void host_wake(WB55UsbState *s, int64_t when)
{
    if (s->timer) {
        timer_mod_anticipate(s->timer, when);
    }
}

static void host_kick(WB55UsbState *s)
{
    if (s->hstate == H_ENUM || s->hstate == H_RUNNING) {
        host_wake(s, now_ns() + KICK_NS);
    }
}

static void host_detach(WB55UsbState *s)
{
    if (s->hstate != H_DETACHED) {
        usb_dbg(s, "detached");
    }
    if (s->usbip_imported) {
        /* the firmware left the bus: unplug it from the PC as well */
        s->usbip_imported = false;
        qemu_chr_fe_disconnect(&s->usbip_chr);
    }
    s->hstate = H_DETACHED;
    s->ctl.active = false;
    s->cdc = false;
    s->hup = false;                         /* the port went with the device */
    fifo8_reset(&s->to_dev);
    s->addr = 0;
    s->failures = 0;
    if (s->timer) {
        timer_del(s->timer);
    }
}

static void host_pullup(WB55UsbState *s, bool on)
{
    if (!on) {
        host_detach(s);
    } else if (s->attached && s->hstate == H_DETACHED) {
        usb_dbg(s, "D+ pull-up: attaching");
        s->hstate = H_DEBOUNCE;
        s->wait_until = now_ns() + 10 * MS;
        host_wake(s, s->wait_until);
    }
}

static void ctl_start(WB55UsbState *s, int64_t now, uint8_t type, uint8_t req,
                      uint16_t value, uint16_t index, uint16_t len,
                      const void *data)
{
    WB55UsbCtl *c = &s->ctl;

    c->setup[0] = type;
    c->setup[1] = req;
    stw_le_p(&c->setup[2], value);
    stw_le_p(&c->setup[4], index);
    stw_le_p(&c->setup[6], len);
    c->len = MIN(len, sizeof(c->buf));
    c->done = 0;
    c->stage = CTL_SETUP;
    if (data) {
        memcpy(c->buf, data, c->len);
    }
    /* a real host leaves at least a few microseconds between transfers */
    c->not_before = MAX(c->not_before, now + CTL_GAP_NS);
    c->deadline = c->not_before + CTL_TIMEOUT_NS;
    c->active = true;
}

/*
 * After every control transfer the firmware re-arms EP0 reception
 * (STAT_RX = VALID, a read-modify-write of EP0R) once it has cleared the
 * CTR flag.  A SETUP that lands between those two accesses is lost: the
 * firmware then reads the endpoint as VALID, finds no SETUP to read and
 * stalls.  Silicon has the same window, but no real host is that fast, so
 * wait until EP0 has settled (or a stall shows the firmware gave up).
 */
static bool ep0_settled(WB55UsbState *s)
{
    int n = usb_find_ep(s, 0);
    uint16_t r;

    if (n < 0) {
        return true;
    }
    r = s->epr[n];
    return !(r & EP_CTR) &&
           (STAT_RX(r) == STAT_VALID || STAT_RX(r) == STAT_STALL);
}

static void parse_config(WB55UsbState *s)
{
    const uint8_t *d = s->cfg_desc;
    int cls = -1, comm_if = -1, ncdc = 0, nhid = 0, nmsc = 0, nother = 0;
    GString *f = g_string_new(NULL);

    s->cdc_in = s->cdc_out = 0;
    s->hid_in = 0;
    s->nif = 0;
    for (int dir = 0; dir < 2; dir++) {
        for (int ep = 0; ep < 16; ep++) {
            s->ep_mps[dir][ep] = 64;
        }
    }
    for (int off = 0; off + 2 <= s->cfg_len; off += d[off]) {
        int len = d[off], type = d[off + 1];

        if (len < 2 || off + len > s->cfg_len) {
            break;
        }
        if (type == 5 && len >= 7) {                /* any endpoint: its size */
            s->ep_mps[d[off + 2] >> 7][d[off + 2] & 0xF] =
                MAX(lduw_le_p(&d[off + 4]) & 0x7FF, 1);
        }
        if (type == 4 && len >= 9) {                /* interface */
            if (d[off + 3]) {
                continue;                           /* alternate setting */
            }
            if (s->nif < ARRAY_SIZE(s->if_class)) {
                memcpy(s->if_class[s->nif++], &d[off + 5], 3);
            }
            cls = d[off + 5];
            if (cls == 0x02 && d[off + 6] == 0x02) {
                ncdc++;
                if (comm_if < 0) {
                    comm_if = d[off + 2];
                }
            } else if (cls == 0x03) {
                nhid++;
            } else if (cls == 0x08) {
                nmsc++;
            } else if (cls != 0x0A) {
                nother++;
            }
        } else if (type == 5 && len >= 7 && cls == 0x03 &&
                   (d[off + 3] & 3) == 3 && (d[off + 2] & 0x80) &&
                   !s->hid_in) {                    /* HID interrupt IN */
            s->hid_in = d[off + 2] & 0xF;
            s->hid_interval = MAX(d[off + 6], 1);
        } else if (type == 5 && len >= 7 && cls == 0x0A &&
                   (d[off + 3] & 3) == 2) {         /* CDC data, bulk */
            uint8_t ea = d[off + 2];

            if ((ea & 0x80) && !s->cdc_in) {
                s->cdc_in = ea & 0xF;
            } else if (!(ea & 0x80) && !s->cdc_out) {
                s->cdc_out = ea & 0xF;
                s->cdc_out_mps = MIN(lduw_le_p(&d[off + 4]), 64);
            }
        }
    }
    s->cdc = comm_if >= 0 && s->cdc_in && s->cdc_out;
    s->cdc_if = MAX(comm_if, 0);
    s->hid = !s->cdc && nhid && s->hid_in;
    if (ncdc) {
        g_string_append_printf(f, "%sCDC ACM%s", f->len ? ", " : "",
                               ncdc > 1 ? " x2" : "");
    }
    if (nhid) {
        g_string_append_printf(f, "%sHID", f->len ? ", " : "");
    }
    if (nmsc) {
        g_string_append_printf(f, "%smass storage", f->len ? ", " : "");
    }
    if (nother) {
        g_string_append_printf(f, "%svendor", f->len ? ", " : "");
    }
    pstrcpy(s->functions, sizeof(s->functions), f->str);
    g_string_free(f, true);
}

static void decode_string(WB55UsbState *s, int which)
{
    const uint8_t *b = s->ctl.buf;
    int n = MIN(s->ctl.done, b[0]);
    char *out = s->strings[which];
    int o = 0;

    for (int i = 2; i + 1 < n && o < 63; i += 2) {
        uint16_t c = lduw_le_p(&b[i]);
        out[o++] = (c >= 0x20 && c < 0x7F) ? c : '?';
    }
    out[o] = 0;
}

static void enum_next(WB55UsbState *s, int64_t now);

static void enum_report(WB55UsbState *s)
{
    GString *m = g_string_new(NULL);
    bool chr = qemu_chr_fe_backend_connected(&s->chr);

    g_string_printf(m, "flipper-zero: USB device %04x:%04x",
                    lduw_le_p(&s->dev_desc[8]), lduw_le_p(&s->dev_desc[10]));
    for (int i = 0; i < 2; i++) {
        if (s->strings[i][0]) {
            g_string_append_printf(m, " \"%s\"", s->strings[i]);
        }
    }
    g_string_append_printf(m, ": %s", s->functions[0] ? s->functions : "?");
    if (chr && s->cdc) {
        g_string_append(m, " (CLI/RPC on the USB chardev)");
    } else if (chr && s->hid) {
        g_string_append(m, " (what it types appears on the USB chardev)");
    }
    info_report("%s", m->str);
    g_string_free(m, true);
}

static void enum_fail(WB55UsbState *s, int64_t now, const char *what)
{
    usb_dbg(s, "enumeration failed: %s", what);
    s->ctl.active = false;
    if (++s->failures < 3) {
        s->hstate = H_SUSPENDED;            /* reset the bus and retry */
        s->wait_until = now + 100 * MS;
        host_wake(s, s->wait_until);
    } else {
        s->hstate = H_FAILED;
        warn_report("flipper-zero: the USB device did not enumerate (%s); "
                    "waiting for it to reconnect", what);
    }
}

static void enum_done(WB55UsbState *s, int64_t now, bool ok)
{
    WB55UsbCtl *c = &s->ctl;
    static const char *const step_name[] = {
        "device descriptor", "SET_ADDRESS", "device descriptor",
        "configuration descriptor", "configuration descriptor",
        "string descriptor", "string descriptor", "string descriptor",
        "string descriptor", "SET_CONFIGURATION", "SET_LINE_CODING",
    };

    if (!ok) {
        if (s->enum_step >= E_LANG && s->enum_step <= E_STR_SERIAL) {
            s->enum_step++;                 /* strings are optional */
            enum_next(s, now);
        } else {
            enum_fail(s, now, step_name[s->enum_step]);
        }
        return;
    }
    switch (s->enum_step) {
    case E_DEV8:
        s->ep0_mps = c->buf[7];
        if (s->ep0_mps != 8 && s->ep0_mps != 16 && s->ep0_mps != 32 &&
            s->ep0_mps != 64) {
            s->ep0_mps = 8;
        }
        break;
    case E_SETADDR:
        s->addr = HOST_ADDR;
        c->not_before = now + 2 * MS;       /* SetAddress recovery */
        break;
    case E_DEV18:
        memcpy(s->dev_desc, c->buf, sizeof(s->dev_desc));
        break;
    case E_CFG9:
        s->cfg_len = MIN(lduw_le_p(&c->buf[2]), sizeof(s->cfg_desc));
        if (c->done < 9 || s->cfg_len < 9) {
            enum_fail(s, now, "configuration descriptor");
            return;
        }
        break;
    case E_CFG:
        s->cfg_len = MIN(s->cfg_len, c->done);
        memcpy(s->cfg_desc, c->buf, s->cfg_len);
        parse_config(s);
        break;
    case E_LANG:
        s->langid = c->done >= 4 ? lduw_le_p(&c->buf[2]) : 0x0409;
        break;
    case E_STR_MANUF:
    case E_STR_PROD:
    case E_STR_SERIAL:
        decode_string(s, s->enum_step - E_STR_MANUF);
        break;
    }
    s->enum_step++;
    enum_next(s, now);
}

static void enum_next(WB55UsbState *s, int64_t now)
{
    static const uint8_t line_coding[7] = {
        0x00, 0xC2, 0x01, 0x00,             /* 115200 baud */
        0, 0, 8,                            /* 1 stop bit, no parity, 8 bits */
    };
    int idx;

    for (;;) {
        switch (s->enum_step) {
        case E_DEV8:
            ctl_start(s, now, 0x80, 6, 0x0100, 0, 8, NULL);
            return;
        case E_SETADDR:
            ctl_start(s, now, 0x00, 5, HOST_ADDR, 0, 0, NULL);
            return;
        case E_DEV18:
            ctl_start(s, now, 0x80, 6, 0x0100, 0, 18, NULL);
            return;
        case E_CFG9:
            ctl_start(s, now, 0x80, 6, 0x0200, 0, 9, NULL);
            return;
        case E_CFG:
            ctl_start(s, now, 0x80, 6, 0x0200, 0, s->cfg_len, NULL);
            return;
        case E_LANG:
            ctl_start(s, now, 0x80, 6, 0x0300, 0, 255, NULL);
            return;
        case E_STR_MANUF:
        case E_STR_PROD:
        case E_STR_SERIAL:
            idx = s->dev_desc[14 + s->enum_step - E_STR_MANUF];
            s->strings[s->enum_step - E_STR_MANUF][0] = 0;
            if (!idx) {
                s->enum_step++;
                continue;
            }
            ctl_start(s, now, 0x80, 6, 0x0300 | idx, s->langid, 255, NULL);
            return;
        case E_SETCFG:
            ctl_start(s, now, 0x00, 9, s->cfg_desc[5], 0, 0, NULL);
            return;
        case E_LINECODING:
            if (!s->cdc) {
                s->enum_step++;
                continue;
            }
            ctl_start(s, now, 0x21, 0x20, 0, s->cdc_if, 7, line_coding);
            return;
        default:
            s->hstate = H_RUNNING;
            s->failures = 0;
            s->dtr_sent = false;
            s->hup = s->stale = false;
            s->kb_mods = s->kb_lone_mods = s->mouse_btn = 0;
            s->alt_code = s->alt_digits = 0;
            memset(s->kb_keys, 0, sizeof(s->kb_keys));
            memset(s->consumer, 0, sizeof(s->consumer));
            s->hid_next_poll = now;
            enum_report(s);
            qemu_chr_fe_accept_input(&s->chr);
            return;
        }
    }
}

static void running_ctl_done(WB55UsbState *s, bool ok)
{
    /* the only control transfer while running: SET_CONTROL_LINE_STATE */
    if (!ok) {
        usb_dbg(s, "SET_CONTROL_LINE_STATE failed");
    }
    s->dtr_sent = s->dtr_pending;
}

static void usbip_ctl_done(WB55UsbState *s, bool ok);

static int ctl_finish(WB55UsbState *s, int64_t now, bool ok)
{
    s->ctl.active = false;
    if (s->ctl_urb) {
        usbip_ctl_done(s, ok);
    } else if (s->hstate == H_ENUM) {
        enum_done(s, now, ok);
    } else if (s->hstate == H_RUNNING) {
        running_ctl_done(s, ok);
    }
    return 1;
}

/* one step of the current control transfer: 1 progress, 0 blocked */
static int ctl_step(WB55UsbState *s, int64_t now)
{
    WB55UsbCtl *c = &s->ctl;
    uint8_t pkt[64];
    int n = 0;
    UsbTx r;

    if (now < c->not_before) {
        host_wake(s, c->not_before);
        return 0;
    }
    if (now >= c->deadline) {
        usb_dbg(s, "control transfer %02x %02x timed out in stage %d",
                c->setup[0], c->setup[1], c->stage);
        return ctl_finish(s, now, false);
    }
    switch (c->stage) {
    case CTL_SETUP:
        if (!ep0_settled(s) &&
            now < c->deadline - CTL_TIMEOUT_NS + EP0_SETTLE_NS) {
            r = TX_NAK;
            break;
        }
        r = usb_setup(s, c->setup);
        if (r == TX_ACK) {
            c->stage = !c->len ? CTL_STATUS_IN :
                       (c->setup[0] & 0x80) ? CTL_DATA_IN : CTL_DATA_OUT;
            return 1;
        }
        break;
    case CTL_DATA_IN:
        r = usb_in(s, 0, pkt, &n);
        if (r == TX_ACK) {
            int take = MIN(n, c->len - c->done);

            memcpy(c->buf + c->done, pkt, take);
            c->done += take;
            if (n < s->ep0_mps || c->done >= c->len) {
                c->stage = CTL_STATUS_OUT;
            }
            return 1;
        }
        break;
    case CTL_DATA_OUT:
        n = MIN(s->ep0_mps, c->len - c->done);
        r = usb_out(s, 0, c->buf + c->done, n);
        if (r == TX_ACK) {
            c->done += n;
            if (c->done >= c->len) {
                c->stage = CTL_STATUS_IN;
            }
            return 1;
        }
        break;
    case CTL_STATUS_IN:
        r = usb_in(s, 0, pkt, &n);
        if (r == TX_ACK) {
            return ctl_finish(s, now, true);
        }
        break;
    default:
        r = usb_out(s, 0, NULL, 0);
        if (r == TX_ACK) {
            return ctl_finish(s, now, true);
        }
        break;
    }
    if (r == TX_STALL) {
        usb_dbg(s, "control transfer %02x %02x stalled in stage %d",
                c->setup[0], c->setup[1], c->stage);
        return ctl_finish(s, now, false);
    }
    host_wake(s, MIN(c->deadline, now + POLL_NS));
    return 0;
}

/* ---- CDC ACM bridge */

static void usb_chr_flush(WB55UsbState *s);

static gboolean usb_chr_writable(void *do_not_use, GIOCondition cond,
                                 void *opaque)
{
    WB55UsbState *s = opaque;

    s->watch = 0;
    usb_chr_flush(s);
    host_kick(s);
    return G_SOURCE_REMOVE;
}

static void usb_chr_flush(WB55UsbState *s)
{
    while (!fifo8_is_empty(&s->to_chr)) {
        uint32_t n;
        const uint8_t *p = fifo8_peek_bufptr(&s->to_chr,
                                             fifo8_num_used(&s->to_chr), &n);
        int w = qemu_chr_fe_write(&s->chr, p, n);

        if (w <= 0) {
            if (!qemu_chr_fe_backend_connected(&s->chr)) {
                fifo8_reset(&s->to_chr);
            } else if (!s->watch) {
                s->watch = qemu_chr_fe_add_watch(&s->chr, G_IO_OUT | G_IO_HUP,
                                                 usb_chr_writable, s);
            }
            return;
        }
        fifo8_drop(&s->to_chr, w);
    }
}

static int cdc_step(WB55UsbState *s, int64_t now)
{
    uint8_t pkt[64];
    int n, progress = 0;

    if (!s->cdc) {
        return -1;
    }
    if (s->hup) {
        /*
         * The port was closed.  As a computer does it then: what the program
         * wrote still goes out, for as long as the device takes it; then DTR
         * and RTS drop, and what is left is gone.  Nothing is sent to a
         * device whose port is closed: the firmware's CLI does not read a
         * packet that comes when it has no session, and no later one either.
         */
        if (!fifo8_is_empty(&s->to_dev) && now < s->hup_by && !s->dtr_sent) {
            /* written and closed before the port's DTR was up: that first */
            s->dtr_pending = true;
            ctl_start(s, now, 0x21, 0x22, 0x0003, s->cdc_if, 0, NULL);
            return 1;
        }
        if (fifo8_is_empty(&s->to_dev) || now >= s->hup_by) {
            fifo8_reset(&s->to_dev);
            s->hup = false;
            qemu_chr_fe_accept_input(&s->chr);
            if (s->dtr_sent) {
                s->dtr_pending = false;
                s->stale = true;
                s->reopen_at = now + REOPEN_NS;
                ctl_start(s, now, 0x21, 0x22, 0, s->cdc_if, 0, NULL);
            }
            return 1;
        }
    } else if (s->chr_open != s->dtr_sent) {
        /* DTR and RTS follow the chardev connection, as with a terminal */
        if (s->chr_open && s->stale) {
            /*
             * The device may still hold a packet for the program that left
             * (one at most: nobody took it, and the firmware waits for
             * that).  It is not for the program that came: that one would
             * read the end of somebody else's answer as the start of its
             * own.  A computer hands it on; a careful program then throws
             * away what it finds in a port it opens, and this does it for
             * all of them.
             */
            if (now < s->reopen_at) {
                host_wake(s, s->reopen_at);
                return 0;
            }
            if (!usb_in_armed(s, s->cdc_in)) {
                s->stale = false;
            } else if (usb_in(s, s->cdc_in, pkt, &n) == TX_ACK) {
                s->stale = false;
                return 1;
            } else {
                host_wake(s, now + IDLE_POLL_NS);
                return 0;
            }
        }
        s->dtr_pending = s->chr_open;
        ctl_start(s, now, 0x21, 0x22, s->chr_open ? 0x0003 : 0, s->cdc_if, 0,
                  NULL);
        return 1;
    }
    if (!fifo8_is_empty(&s->to_dev)) {
        uint32_t len;
        const uint8_t *p = fifo8_peek_bufptr(&s->to_dev,
                MIN(s->cdc_out_mps, fifo8_num_used(&s->to_dev)), &len);

        if (usb_out(s, s->cdc_out, p, len) == TX_ACK) {
            fifo8_drop(&s->to_dev, len);
            qemu_chr_fe_accept_input(&s->chr);
            progress = 1;
        }
    }
    /*
     * The host reads the bulk IN endpoint only while the port is open; and
     * while a closed one sends its last bytes, with nobody left to read
     * what comes back.
     */
    if ((s->chr_open || s->hup) && fifo8_num_free(&s->to_chr) >= sizeof(pkt) &&
        usb_in(s, s->cdc_in, pkt, &n) == TX_ACK) {
        if (n && !s->hup) {
            fifo8_push_all(&s->to_chr, pkt, n);
            usb_chr_flush(s);
        }
        progress = 1;
    }
    if (!progress && (s->chr_open || !fifo8_is_empty(&s->to_dev))) {
        host_wake(s, now + IDLE_POLL_NS);   /* safety net; writes kick us */
    }
    return progress;
}

/* ---- HID: the host as a PC with a US keyboard layout */

static void hid_emit(WB55UsbState *s, const char *txt)
{
    uint32_t n = MIN(strlen(txt), fifo8_num_free(&s->to_chr));

    if (!s->chr_open || !n) {
        return;
    }
    fifo8_push_all(&s->to_chr, (const uint8_t *)txt, n);
    usb_chr_flush(s);
}

static const char kb_plain[0x39] =
    "\0\0\0\0abcdefghijklmnopqrstuvwxyz1234567890\0\0\0\0 -=[]\\#;'`,./";
static const char kb_shift[0x39] =
    "\0\0\0\0ABCDEFGHIJKLMNOPQRSTUVWXYZ!@#$%^&*()\0\0\0\0 _+{}|~:\"~<>?";

static const char *kb_name(uint8_t k, char *buf, size_t len)
{
    static const char *const names[] = {
        [0x28] = "ENTER", [0x29] = "ESC", [0x2A] = "BACKSPACE",
        [0x2B] = "TAB", [0x2C] = "SPACE", [0x39] = "CAPSLOCK",
        [0x46] = "PRINTSCREEN", [0x47] = "SCROLLLOCK", [0x48] = "PAUSE",
        [0x49] = "INSERT", [0x4A] = "HOME", [0x4B] = "PAGEUP",
        [0x4C] = "DELETE", [0x4D] = "END", [0x4E] = "PAGEDOWN",
        [0x4F] = "RIGHT", [0x50] = "LEFT", [0x51] = "DOWN", [0x52] = "UP",
        [0x53] = "NUMLOCK", [0x65] = "MENU",
    };

    if (k < ARRAY_SIZE(names) && names[k]) {
        return names[k];
    }
    if (k >= 0x3A && k <= 0x45) {
        snprintf(buf, len, "F%d", k - 0x39);
    } else if (k < sizeof(kb_plain) && kb_plain[k]) {
        snprintf(buf, len, "%c", kb_plain[k]);
    } else {
        snprintf(buf, len, "0x%02x", k);
    }
    return buf;
}

static void kb_mods_name(uint8_t mods, char *buf, size_t len)
{
    static const char *const names[4] = { "CTRL", "SHIFT", "ALT", "GUI" };

    buf[0] = 0;
    for (int i = 0; i < 4; i++) {
        if (mods & (0x11 << i)) {
            g_strlcat(buf, names[i], len);
            g_strlcat(buf, " ", len);
        }
    }
}

static void kb_press(WB55UsbState *s, uint8_t mods, uint8_t k)
{
    static const char keypad[] = "/*-+\n1234567890.";
    char name[16], m[32], out[64];
    bool shift = mods & 0x22;

    if (k == 0x83) {
        return;                         /* locking Num Lock: ALTCHAR noise */
    }
    if (k >= 0x59 && k <= 0x62 && (mods & 0x44) && !(mods & 0x99)) {
        s->alt_code = s->alt_code * 10 + (k - 0x58) % 10;   /* Alt+Numpad */
        s->alt_digits++;
        return;
    }
    if (k >= 0x54 && k <= 0x63 && !(mods & 0xDD)) {
        char c[3] = { keypad[k - 0x54], 0, 0 };
        if (c[0] == '\n') {
            c[0] = '\r';
            c[1] = '\n';
        }
        hid_emit(s, c);
        return;
    }
    if (!(mods & 0xDD)) {               /* no CTRL/ALT/GUI: plain typing */
        if (k < sizeof(kb_plain) && kb_plain[k]) {
            char c[2] = { shift ? kb_shift[k] : kb_plain[k], 0 };
            hid_emit(s, c);
            return;
        }
        if (!shift) {
            switch (k) {
            case 0x28:
                hid_emit(s, "\r\n");
                return;
            case 0x2A:
                hid_emit(s, "\b \b");
                return;
            case 0x2B:
                hid_emit(s, "\t");
                return;
            }
        }
    }
    kb_mods_name(mods, m, sizeof(m));
    snprintf(out, sizeof(out), "<%s%s>", m, kb_name(k, name, sizeof(name)));
    hid_emit(s, out);
}

static void hid_keyboard(WB55UsbState *s, uint8_t mods, const uint8_t *keys)
{
    char m[32], out[40];
    uint8_t released = s->kb_mods & ~mods;

    s->kb_lone_mods |= mods & ~s->kb_mods;
    for (int i = 0; i < 6; i++) {
        uint8_t k = keys[i];

        if (k < 4 || memchr(s->kb_keys, k, sizeof(s->kb_keys))) {
            continue;                   /* empty, rollover error or held */
        }
        s->kb_lone_mods = 0;            /* the modifiers were for this key */
        kb_press(s, mods, k);
    }
    if ((released & 0x44) && !(mods & 0x44) && s->alt_digits) {
        uint8_t c = s->alt_code;        /* Alt released: the code is a char */

        if (c >= 0x20 && c < 0x7F) {
            snprintf(out, sizeof(out), "%c", c);
        } else if (c == '\r' || c == '\n') {
            snprintf(out, sizeof(out), "\r\n");
        } else {
            snprintf(out, sizeof(out), "<ALT %u>", s->alt_code);
        }
        hid_emit(s, out);
        s->alt_code = s->alt_digits = 0;
    }
    if (released & s->kb_lone_mods) {   /* e.g. a bare GUI tap */
        kb_mods_name(released & s->kb_lone_mods, m, sizeof(m));
        m[strlen(m) - 1] = 0;
        snprintf(out, sizeof(out), "<%s>", m);
        hid_emit(s, out);
    }
    s->kb_lone_mods &= mods;
    s->kb_mods = mods;
    memcpy(s->kb_keys, keys, sizeof(s->kb_keys));
}

static void hid_report(WB55UsbState *s, const uint8_t *r, int n)
{
    static const char *const btn[3] = { "LEFT", "RIGHT", "MIDDLE" };
    char out[40];

    if (n == 8) {                       /* boot protocol keyboard */
        hid_keyboard(s, r[0], &r[2]);
    } else if (r[0] == 1 && n >= 9) {   /* Flipper report IDs: 1 keyboard */
        hid_keyboard(s, r[1], &r[3]);
    } else if (r[0] == 2 && n >= 2) {   /* 2 mouse: report clicks only */
        for (int i = 0; i < 3; i++) {
            if ((r[1] & ~s->mouse_btn) & (1 << i)) {
                snprintf(out, sizeof(out), "<MOUSE %s>", btn[i]);
                hid_emit(s, out);
            }
        }
        s->mouse_btn = r[1];
    } else if (r[0] == 3 && n >= 5) {   /* 3 consumer control */
        for (int i = 0; i < 2; i++) {
            uint16_t u = lduw_le_p(&r[1 + 2 * i]);

            if (u && u != s->consumer[0] && u != s->consumer[1]) {
                snprintf(out, sizeof(out), "<CONSUMER 0x%03x>", u);
                hid_emit(s, out);
            }
        }
        s->consumer[0] = lduw_le_p(&r[1]);
        s->consumer[1] = lduw_le_p(&r[3]);
    }
}

/* poll the interrupt endpoint every bInterval frames, like a PC does */
static int hid_step(WB55UsbState *s, int64_t now)
{
    uint8_t pkt[64];
    int n;

    if (now < s->hid_next_poll) {
        host_wake(s, s->hid_next_poll);
        return 0;
    }
    s->hid_next_poll = now + s->hid_interval * MS;
    if (usb_in(s, s->hid_in, pkt, &n) == TX_ACK && n) {
        hid_report(s, pkt, n);
    }
    host_wake(s, s->hid_next_poll);
    return 0;
}

/* ============================================================ USB/IP */
/*
 * The device can be exported over USB/IP (Linux's usbip protocol, TCP 3240)
 * so the PC's own kernel becomes its host: `usbip attach` (or
 * scripts/usbip_attach.py) makes it a real USB device there, cdc_acm binds
 * /dev/ttyACM*, and qFlipper or any other tool finds a Flipper by VID/PID.
 *
 * The built-in host keeps doing what only the bus can do (reset, address);
 * vhci-hcd handles SET_ADDRESS on its side and forwards everything else.
 * Each CMD_SUBMIT becomes transactions on the emulated controller: control
 * URBs through the control-transfer engine, bulk and interrupt URBs per
 * endpoint, completing when the firmware supplies or takes the data.
 */
#define USBIP_VERSION       0x0111
#define USBIP_BUSID         "1-1"
#define USBIP_BUSNUM        1
#define USBIP_DEVNUM        2
#define USBIP_CMD_SUBMIT    1
#define USBIP_CMD_UNLINK    2
#define USBIP_RET_SUBMIT    3
#define USBIP_RET_UNLINK    4
#define URB_ZERO_PACKET     0x0040
#define ERR_EPIPE           (-32)       /* Linux errno values: the client is Linux */
#define ERR_EOVERFLOW       (-75)
#define ERR_ECONNRESET      (-104)
#define ERR_ESHUTDOWN       (-108)

static void usbip_flush(WB55UsbState *s);

static gboolean usbip_writable(void *do_not_use, GIOCondition cond,
                               void *opaque)
{
    WB55UsbState *s = opaque;

    s->usbip_watch = 0;
    usbip_flush(s);
    return G_SOURCE_REMOVE;
}

static void usbip_flush(WB55UsbState *s)
{
    while (s->usbip_tx->len) {
        int w = qemu_chr_fe_write(&s->usbip_chr, s->usbip_tx->data,
                                  s->usbip_tx->len);

        if (w <= 0) {
            if (!s->usbip_open) {
                g_byte_array_set_size(s->usbip_tx, 0);
            } else if (!s->usbip_watch) {
                s->usbip_watch = qemu_chr_fe_add_watch(&s->usbip_chr,
                                     G_IO_OUT | G_IO_HUP, usbip_writable, s);
            }
            return;
        }
        g_byte_array_remove_range(s->usbip_tx, 0, w);
    }
}

static void put_be16(GByteArray *b, uint16_t v)
{
    uint8_t x[2];

    stw_be_p(x, v);
    g_byte_array_append(b, x, 2);
}

static void put_be32(GByteArray *b, uint32_t v)
{
    uint8_t x[4];

    stl_be_p(x, v);
    g_byte_array_append(b, x, 4);
}

static void put_zero(GByteArray *b, int n)
{
    static const uint8_t z[256];

    g_byte_array_append(b, z, n);
}

/* the 0x138-byte device block of OP_REP_DEVLIST / OP_REP_IMPORT */
static void usbip_put_device(WB55UsbState *s, GByteArray *b)
{
    char path[256] = "/sys/devices/platform/flipperemu/usb1/" USBIP_BUSID;
    char busid[32] = USBIP_BUSID;

    g_byte_array_append(b, (uint8_t *)path, sizeof(path));
    g_byte_array_append(b, (uint8_t *)busid, sizeof(busid));
    put_be32(b, USBIP_BUSNUM);
    put_be32(b, USBIP_DEVNUM);
    put_be32(b, 2);                                 /* USB_SPEED_FULL */
    put_be16(b, lduw_le_p(&s->dev_desc[8]));        /* idVendor */
    put_be16(b, lduw_le_p(&s->dev_desc[10]));       /* idProduct */
    put_be16(b, lduw_le_p(&s->dev_desc[12]));       /* bcdDevice */
    g_byte_array_append(b, &s->dev_desc[4], 3);     /* class, sub, proto */
    g_byte_array_append(b, &s->cfg_desc[5], 1);     /* bConfigurationValue */
    g_byte_array_append(b, &s->dev_desc[17], 1);    /* bNumConfigurations */
    g_byte_array_append(b, &s->nif, 1);             /* bNumInterfaces */
}

/* ready to be exported: enumerated by the built-in host */
static bool usbip_ready(WB55UsbState *s)
{
    return s->hstate == H_RUNNING && s->cfg_len >= 9;
}

static void usbip_free_urb(WB55UsbState *s, WB55Urb *u)
{
    QTAILQ_REMOVE(&s->urbs, u, next);
    g_free(u->data);
    g_free(u);
}

static void usbip_complete(WB55UsbState *s, WB55Urb *u, int32_t status)
{
    GByteArray *b = s->usbip_tx;
    uint32_t actual = u->dir ? u->done : (status ? u->done : u->len);

    if (!u->unlinked && s->usbip_imported) {
        put_be32(b, USBIP_RET_SUBMIT);
        put_be32(b, u->seqnum);
        put_be32(b, 0);                         /* devid, direction, ep: 0 */
        put_be32(b, 0);
        put_be32(b, 0);
        put_be32(b, status);
        put_be32(b, actual);
        put_be32(b, u->start_frame);            /* echo what the client sent */
        put_be32(b, u->npackets);
        put_be32(b, 0);                         /* error_count */
        put_zero(b, 8);
        if (u->dir) {
            g_byte_array_append(b, u->data, u->done);
        }
        usbip_flush(s);
    }
    if (s->ctl_urb == u) {
        s->ctl_urb = NULL;
    }
    usbip_free_urb(s, u);
}

static void usbip_ctl_done(WB55UsbState *s, bool ok)
{
    WB55Urb *u = s->ctl_urb;

    if (u->dir) {
        u->done = MIN(s->ctl.done, u->len);
        memcpy(u->data, s->ctl.buf, u->done);
    } else {
        u->done = ok ? u->len : 0;
    }
    usbip_complete(s, u, ok ? 0 : ERR_EPIPE);
}

/* drop every URB, e.g. when the client goes away or the device leaves */
static void usbip_drop_urbs(WB55UsbState *s)
{
    WB55Urb *u, *tmp;

    QTAILQ_FOREACH_SAFE(u, &s->urbs, next, tmp) {
        if (u == s->ctl_urb) {
            s->ctl.active = false;
            s->ctl_urb = NULL;
        }
        usbip_free_urb(s, u);
    }
}

/* one bulk/interrupt transaction for the URB at the head of its endpoint */
static int usbip_transact(WB55UsbState *s, WB55Urb *u)
{
    uint16_t mps = s->ep_mps[u->dir][u->ep];
    uint8_t pkt[64];
    int n;
    UsbTx r;

    if (u->dir) {
        r = usb_in(s, u->ep, pkt, &n);
        if (r == TX_ACK) {
            int take = MIN(n, (int)(u->len - u->done));

            memcpy(u->data + u->done, pkt, take);
            u->done += take;
            if (n > take) {
                usbip_complete(s, u, ERR_EOVERFLOW);        /* babble */
            } else if (n < mps || u->done >= u->len) {
                usbip_complete(s, u, 0);
            }
            return 1;
        }
    } else {
        bool want_zlp = (u->flags & URB_ZERO_PACKET) && u->len &&
                        !(u->len % mps);

        n = MIN(mps, u->len - u->done);
        r = usb_out(s, u->ep, u->data + u->done, n);
        if (r == TX_ACK) {
            u->done += n;
            if (!n) {
                u->zlp_done = true;
            }
            if (u->done >= u->len && (!want_zlp || u->zlp_done || !n) &&
                (u->len || u->zlp_done)) {
                usbip_complete(s, u, 0);
            }
            return 1;
        }
    }
    if (r == TX_STALL) {
        usbip_complete(s, u, ERR_EPIPE);
        return 1;
    }
    return 0;
}

static int usbip_step(WB55UsbState *s, int64_t now)
{
    WB55Urb *u, *tmp;
    uint32_t busy = 0;
    int progress = 0;
    bool waiting = false;

    if (!s->ctl.active) {                   /* control: in order, one at a time */
        QTAILQ_FOREACH(u, &s->urbs, next) {
            if (u->ep == 0 && !u->started) {
                u->started = true;
                s->ctl_urb = u;
                ctl_start(s, now, u->setup[0], u->setup[1],
                          lduw_le_p(&u->setup[2]), lduw_le_p(&u->setup[4]),
                          MIN(lduw_le_p(&u->setup[6]), u->len),
                          u->dir ? NULL : u->data);
                return 1;
            }
        }
    }
    QTAILQ_FOREACH_SAFE(u, &s->urbs, next, tmp) {
        uint32_t key = 1u << ((u->dir << 4) | u->ep);

        if (u->ep == 0 || (busy & key)) {
            continue;
        }
        busy |= key;
        progress |= usbip_transact(s, u);
        waiting = true;
    }
    if (!progress && waiting) {
        host_wake(s, now + POLL_NS);        /* safety net; writes kick us */
    }
    return progress;
}

static void usbip_reply_import(WB55UsbState *s, bool ok)
{
    GByteArray *b = s->usbip_tx;

    put_be16(b, USBIP_VERSION);
    put_be16(b, 0x0003);                    /* OP_REP_IMPORT */
    put_be32(b, ok ? 0 : 1);
    if (ok) {
        usbip_put_device(s, b);
    }
    usbip_flush(s);
}

static void usbip_import(WB55UsbState *s, const char *busid)
{
    if (strcmp(busid, USBIP_BUSID) || !usbip_ready(s)) {
        usbip_reply_import(s, false);
        return;
    }
    usbip_drop_urbs(s);
    s->usbip_imported = true;
    usbip_reply_import(s, true);
    info_report("flipper-zero: USB device %04x:%04x attached to the PC over "
                "USB/IP", lduw_le_p(&s->dev_desc[8]),
                lduw_le_p(&s->dev_desc[10]));
}

static void usbip_reply_devlist(WB55UsbState *s)
{
    GByteArray *b = s->usbip_tx;
    bool ready = usbip_ready(s);

    put_be16(b, USBIP_VERSION);
    put_be16(b, 0x0005);                    /* OP_REP_DEVLIST */
    put_be32(b, 0);
    put_be32(b, ready ? 1 : 0);
    if (ready) {
        usbip_put_device(s, b);
        for (int i = 0; i < s->nif; i++) {
            g_byte_array_append(b, s->if_class[i], 3);
            put_zero(b, 1);
        }
    }
    usbip_flush(s);
}

static void usbip_unlink(WB55UsbState *s, uint32_t seqnum, uint32_t target)
{
    GByteArray *b = s->usbip_tx;
    int32_t status = 0;
    WB55Urb *u;

    QTAILQ_FOREACH(u, &s->urbs, next) {
        if (u->seqnum == target && !u->unlinked) {
            status = ERR_ECONNRESET;
            if (u == s->ctl_urb) {
                u->unlinked = true;         /* finishes on the bus, unreported */
            } else {
                usbip_free_urb(s, u);
            }
            break;
        }
    }
    put_be32(b, USBIP_RET_UNLINK);
    put_be32(b, seqnum);
    put_zero(b, 12);
    put_be32(b, status);
    put_zero(b, 24);
    usbip_flush(s);
}

/* parse what the client sent; false on a protocol error */
static bool usbip_parse(WB55UsbState *s)
{
    GByteArray *in = s->usbip_rx;

    while (in->len >= 8) {
        const uint8_t *p = in->data;

        if (!s->usbip_imported) {
            uint16_t code = lduw_be_p(p + 2);

            if (code == 0x8005) {           /* OP_REQ_DEVLIST */
                g_byte_array_remove_range(in, 0, 8);
                usbip_reply_devlist(s);
            } else if (code == 0x8003) {    /* OP_REQ_IMPORT */
                char busid[33];

                if (in->len < 40) {
                    return true;
                }
                memcpy(busid, p + 8, 32);
                busid[32] = 0;
                g_byte_array_remove_range(in, 0, 40);
                usbip_import(s, busid);
            } else {
                return false;
            }
            continue;
        }
        if (in->len < 48) {
            return true;
        }
        uint32_t cmd = ldl_be_p(p), seqnum = ldl_be_p(p + 4);
        if (cmd == USBIP_CMD_SUBMIT) {
            uint32_t dir = ldl_be_p(p + 12), len = ldl_be_p(p + 24);
            WB55Urb *u;

            if (len > 1024 * 1024) {
                return false;
            }
            if (!dir && in->len < 48 + len) {
                return true;                /* wait for the OUT payload */
            }
            u = g_new0(WB55Urb, 1);
            u->seqnum = seqnum;
            u->dir = dir ? 1 : 0;
            u->ep = ldl_be_p(p + 16) & 0xF;
            u->flags = ldl_be_p(p + 20);
            u->len = len;
            u->start_frame = ldl_be_p(p + 28);
            u->npackets = ldl_be_p(p + 32);
            memcpy(u->setup, p + 40, 8);
            u->data = g_malloc0(MAX(len, 1));
            if (!dir) {
                memcpy(u->data, p + 48, len);
            }
            g_byte_array_remove_range(in, 0, 48 + (dir ? 0 : len));
            QTAILQ_INSERT_TAIL(&s->urbs, u, next);
            host_kick(s);
        } else if (cmd == USBIP_CMD_UNLINK) {
            uint32_t target = ldl_be_p(p + 20);

            g_byte_array_remove_range(in, 0, 48);
            usbip_unlink(s, seqnum, target);
        } else {
            return false;
        }
    }
    return true;
}

static int usbip_can_receive(void *opaque)
{
    WB55UsbState *s = opaque;

    return s->usbip_rx->len < 2 * 1024 * 1024 ? 64 * 1024 : 0;
}

static void usbip_receive(void *opaque, const uint8_t *buf, int size)
{
    WB55UsbState *s = opaque;

    g_byte_array_append(s->usbip_rx, buf, size);
    if (!usbip_parse(s)) {
        warn_report("flipper-zero: USB/IP protocol error, closing");
        qemu_chr_fe_disconnect(&s->usbip_chr);
    }
}

static void usbip_end(WB55UsbState *s)
{
    bool was = s->usbip_imported;

    s->usbip_imported = false;
    usbip_drop_urbs(s);
    g_byte_array_set_size(s->usbip_rx, 0);
    if (was) {
        info_report("flipper-zero: USB/IP client detached");
        /* hand the device back to the built-in host with a fresh reset */
        if (s->hstate != H_DETACHED) {
            host_detach(s);
            host_pullup(s, true);
        }
    }
}

static void usbip_event(void *opaque, QEMUChrEvent event)
{
    WB55UsbState *s = opaque;

    switch (event) {
    case CHR_EVENT_OPENED:
        s->usbip_open = true;
        g_byte_array_set_size(s->usbip_rx, 0);
        g_byte_array_set_size(s->usbip_tx, 0);
        break;
    case CHR_EVENT_CLOSED:
        s->usbip_open = false;
        g_byte_array_set_size(s->usbip_tx, 0);
        usbip_end(s);
        break;
    default:
        break;
    }
}

/* advance the host by one step: 1 progress, 0 blocked, -1 nothing to do */
static int host_step(WB55UsbState *s, int64_t now)
{
    switch (s->hstate) {
    case H_DEBOUNCE:
        if (now < s->wait_until) {
            host_wake(s, s->wait_until);
            return 0;
        }
        s->istr |= ISTR_SUSP;               /* idle bus: suspend */
        usb_update_irq(s);
        s->hstate = H_SUSPENDED;
        s->wait_until = now + 5 * MS;
        return 1;
    case H_SUSPENDED:
        if (now < s->wait_until) {
            host_wake(s, s->wait_until);
            return 0;
        }
        usb_dbg(s, "bus reset");
        usb_bus_reset(s);
        s->hstate = H_RESET;
        s->wait_until = now + 10 * MS;
        return 1;
    case H_RESET:
        if (now < s->wait_until) {
            host_wake(s, s->wait_until);
            return 0;
        }
        s->hstate = H_ENUM;
        s->enum_step = E_DEV8;
        s->ep0_mps = 8;
        s->ctl.not_before = 0;
        s->cdc = false;
        enum_next(s, now);
        return 1;
    case H_ENUM:
    case H_RUNNING:
        if (s->ctl.active) {
            return ctl_step(s, now);
        }
        if (s->hstate != H_RUNNING) {
            return -1;
        }
        if (s->usbip_imported) {
            return usbip_step(s, now);
        }
        return s->cdc ? cdc_step(s, now) : s->hid ? hid_step(s, now) : -1;
    default:
        return -1;
    }
}

static void host_timer(void *opaque)
{
    WB55UsbState *s = opaque;
    int64_t now = now_ns();

    for (int budget = 64; budget; budget--) {
        if (host_step(s, now) <= 0) {
            return;
        }
    }
    host_wake(s, now + KICK_NS);            /* more to do: yield first */
}

/* ---- chardev side */

static int usb_chr_can_receive(void *opaque)
{
    WB55UsbState *s = opaque;

    if (s->hstate != H_RUNNING || s->usbip_imported) {
        return 0;                           /* keep it in the chardev */
    }
    if (s->cdc && s->hup) {
        return 0;                           /* after the hang-up */
    }
    return s->cdc ? fifo8_num_free(&s->to_dev) : 256;
}

static void usb_chr_receive(void *opaque, const uint8_t *buf, int size)
{
    WB55UsbState *s = opaque;

    if (!s->cdc) {
        return;                             /* no CDC function: dropped */
    }
    fifo8_push_all(&s->to_dev, buf, MIN(size, fifo8_num_free(&s->to_dev)));
    host_kick(s);
}

static void usb_chr_event(void *opaque, QEMUChrEvent event)
{
    WB55UsbState *s = opaque;

    switch (event) {
    case CHR_EVENT_OPENED:
        s->chr_open = true;
        break;
    case CHR_EVENT_CLOSED:
        s->chr_open = false;
        fifo8_reset(&s->to_chr);
        s->hup = true;
        s->hup_by = now_ns() + CLOSE_WAIT_NS;
        break;
    default:
        return;
    }
    usb_dbg(s, "chardev %s", s->chr_open ? "opened" : "closed");
    host_kick(s);
}

/* ---- QOM: the virtual cable */

static bool usb_get_attached(Object *obj, Error **errp)
{
    return WB55_USB(obj)->attached;
}

static void usb_set_attached(Object *obj, bool value, Error **errp)
{
    WB55UsbState *s = WB55_USB(obj);

    if (s->attached == value) {
        return;
    }
    s->attached = value;
    if (s->vbus_changed) {
        s->vbus_changed(s->vbus_opaque, value);
    }
    if (!s->timer) {
        return;                             /* not realized yet */
    }
    if (value) {
        if (s->bcdr & BCDR_DPPU) {
            host_pullup(s, true);
        }
    } else if (s->hstate != H_DETACHED) {
        host_detach(s);
        s->istr |= ISTR_SUSP;               /* the bus went quiet */
        usb_update_irq(s);
    }
}

/* what the host at the other end of the cable makes of the Flipper */
static char *usb_get_device(Object *obj, Error **errp)
{
    WB55UsbState *s = WB55_USB(obj);

    if (s->usbip_imported) {
        return g_strdup("usbip");           /* a PC has it: ask that */
    }
    switch (s->hstate) {
    case H_DETACHED:
        return g_strdup("none");
    case H_RUNNING:
        return g_strdup_printf("%04x:%04x %s: %s",
                               lduw_le_p(&s->dev_desc[8]),
                               lduw_le_p(&s->dev_desc[10]), s->strings[1],
                               s->functions[0] ? s->functions : "?");
    case H_FAILED:
        return g_strdup("failed");
    default:
        return g_strdup("enumerating");
    }
}

/* ---- device boilerplate */

static void usb_reset_hold(Object *obj, ResetType type)
{
    WB55UsbState *s = WB55_USB(obj);

    memset(s->epr, 0, sizeof(s->epr));
    memset(s->epr_seen, 0, sizeof(s->epr_seen));
    memset(s->ep_armed, 0, sizeof(s->ep_armed));
    memset(s->ep_done, 0, sizeof(s->ep_done));
    memset(s->ep_look, 0, sizeof(s->ep_look));
    memset(s->ep_timed, 0, sizeof(s->ep_timed));
    for (int n = 0; n < 8; n++) {
        s->ep_idle[n] = IDLE_FIRST_NS;
    }
    s->bus_last = s->turn_ns = 0;
    s->cntr = CNTR_FRES | CNTR_PDWN;
    s->istr = s->istr_seen = 0;
    s->daddr = s->btable = s->lpmcsr = s->bcdr = 0;
    host_detach(s);
    fifo8_reset(&s->to_dev);
    fifo8_reset(&s->to_chr);
}

static void usb_reset_exit(Object *obj, ResetType type)
{
    usb_update_irq(WB55_USB(obj));
}

static void usb_init(Object *obj)
{
    WB55UsbState *s = WB55_USB(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);

    memory_region_init_io(&s->mmio, obj, &usb_ops, s, "stm32wb55-usb", 0x400);
    memory_region_init_io(&s->pma_mmio, obj, &pma_ops, s, "stm32wb55-usb-pma",
                          sizeof(s->pma));
    sysbus_init_mmio(sbd, &s->mmio);
    sysbus_init_mmio(sbd, &s->pma_mmio);
    sysbus_init_irq(sbd, &s->irq_lp);
    sysbus_init_irq(sbd, &s->irq_hp);
    fifo8_create(&s->to_dev, 1024);
    fifo8_create(&s->to_chr, 4096);
    QTAILQ_INIT(&s->urbs);
    s->usbip_rx = g_byte_array_new();
    s->usbip_tx = g_byte_array_new();
    object_property_add_bool(obj, "attached", usb_get_attached,
                             usb_set_attached);
    object_property_set_description(obj, "attached",
        "Virtual USB cable to the built-in host (VBUS, enumeration, CDC)");
    object_property_add_str(obj, "device", usb_get_device, NULL);
    object_property_set_description(obj, "device",
        "What the host sees on the bus: none, enumerating, failed, usbip "
        "(a PC attached it over USB/IP) or \"vid:pid product: functions\"");
}

static void usb_realize(DeviceState *dev, Error **errp)
{
    WB55UsbState *s = WB55_USB(dev);

    s->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, host_timer, s);
    qemu_chr_fe_set_handlers(&s->chr, usb_chr_can_receive, usb_chr_receive,
                             usb_chr_event, NULL, s, NULL, true);
    qemu_chr_fe_set_handlers(&s->usbip_chr, usbip_can_receive, usbip_receive,
                             usbip_event, NULL, s, NULL, true);
}

static const Property usb_props[] = {
    DEFINE_PROP_CHR("chardev", WB55UsbState, chr),
    DEFINE_PROP_CHR("usbip", WB55UsbState, usbip_chr),
    DEFINE_PROP_BOOL("debug", WB55UsbState, debug, false),
};

static void usb_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    ResettableClass *rc = RESETTABLE_CLASS(klass);

    dc->realize = usb_realize;
    dc->user_creatable = false;
    rc->phases.hold = usb_reset_hold;
    rc->phases.exit = usb_reset_exit;
    device_class_set_props(dc, usb_props);
}

static const TypeInfo usb_types[] = {
    {
        .name = TYPE_WB55_USB,
        .parent = TYPE_SYS_BUS_DEVICE,
        .instance_size = sizeof(WB55UsbState),
        .instance_init = usb_init,
        .class_init = usb_class_init,
    },
};
DEFINE_TYPES(usb_types)
