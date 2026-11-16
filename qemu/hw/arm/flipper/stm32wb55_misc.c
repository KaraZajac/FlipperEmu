/*
 * STM32WB55 small blocks: RNG, DWT cycle counter, DBGMCU, and a register file
 * for peripherals that only need read-back (with a few status-bit behaviours).
 *
 * DWT: QEMU's Cortex-M PPB reads zero at 0xE0001000, but furi_delay_us()
 * busy-waits on DWT->CYCCNT (furi_hal_cortex.c) - even before the scheduler
 * starts (main(): furi_delay_ms(100) for button sampling). CYCCNT here counts
 * HCLK cycles of the CPU's own time (the machine's, less what the host kept
 * the CPU's thread from a core: see dwt_steal_look) and follows clock changes
 * (MSI 4 MHz at reset, PLL 64 MHz after furi_hal_clock_init()).
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/atomic.h"
#include "qemu/log.h"
#include "qemu/main-loop.h"
#include "qemu/guest-random.h"
#include "hw/core/cpu.h"
#include "hw/core/irq.h"
#include "hw/core/qdev-clock.h"
#include "hw/core/qdev-properties.h"
#include "qemu/bitops.h"
#include "migration/vmstate.h"
#include "wb55.h"

/* ------------------------------------------------------------------- RNG */

static uint64_t wb55_rng_read(void *opaque, hwaddr addr, unsigned size)
{
    WB55RngState *s = opaque;
    uint32_t v = 0;

    switch (addr) {
    case 0x00:
        return s->cr;
    case 0x04:
        return (s->cr & (1u << 2)) ? 1 : 0;     /* DRDY while RNGEN */
    case 0x08:
        if (s->cr & (1u << 2)) {
            qemu_guest_getrandom_nofail(&v, sizeof(v));
        }
        return v;
    }
    return 0;
}

static void wb55_rng_write(void *opaque, hwaddr addr, uint64_t val,
                           unsigned size)
{
    WB55RngState *s = opaque;

    if (addr == 0x00) {
        s->cr = val & 0x2C;
        qemu_set_irq(s->irq, 0);
    }
}

static const MemoryRegionOps wb55_rng_ops = {
    .read = wb55_rng_read,
    .write = wb55_rng_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .impl.min_access_size = 4, .impl.max_access_size = 4,
    .valid.min_access_size = 4, .valid.max_access_size = 4,
};

static void wb55_rng_reset_hold(Object *obj, ResetType type)
{
    WB55_RNG(obj)->cr = 0;
}

static void wb55_rng_init(Object *obj)
{
    WB55RngState *s = WB55_RNG(obj);

    memory_region_init_io(&s->mmio, obj, &wb55_rng_ops, s, "rng", 0x400);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq);
}

static const VMStateDescription vmstate_wb55_rng = {
    .name = "stm32wb55-rng",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32(cr, WB55RngState),
        VMSTATE_UINT32(sr, WB55RngState),
        VMSTATE_END_OF_LIST()
    }
};

static void wb55_rng_class_init(ObjectClass *klass, const void *data)
{
    RESETTABLE_CLASS(klass)->phases.hold = wb55_rng_reset_hold;
    DEVICE_CLASS(klass)->vmsd = &vmstate_wb55_rng;
}

/* ------------------------------------------------------------------- DWT */

/*
 * The CPU's own time.  The machine's clock is the host's, and it goes on
 * while the host has the emulated CPU's thread waiting for a core: a wait
 * of the firmware's on CYCCNT (furi_delay_us) that the host interrupts ends
 * late by as much, and what the firmware times with such waits (a T5577's
 * bits, a 1-Wire slot) is as much too long.  A cycle counter counts what
 * the CPU does, so that time is taken out of it, as a virtual machine's
 * steal time is: the kernel has it for each thread (the second number of
 * /proc/thread-self/schedstat, the time spent waiting on a run queue).
 * Time that the CPU sleeps is not such time, and counts.
 *
 * It is taken out where it does harm, and looked up only there: the
 * firmware reads CYCCNT at the start and the end of every interrupt handler
 * and at every change of thread as well (its accounts of where the time
 * goes), and a look is a system call.  The CPU's thread does it itself: the
 * reads need no lock, and this must need none either.
 * - In a wait's loop: reads that follow each other within DWT_TIGHT_NS,
 *   from the third on.  When the loop then stands still for longer than
 *   DWT_BREAK_NS, what the kernel's count has grown by is taken out, and
 *   no more than the loop stood still (the count may have grown before
 *   the wait, which is no business of this wait's).  A wait that nothing
 *   interrupts costs no look.
 * - Between two changes of a line that the firmware times with such a
 *   wait: at the change itself (wb55_dwt_cpu_look) and at the first read
 *   after it, so that the time before the wait and behind it is the
 *   firmware's as well.  A look that is the first for a while only takes
 *   the count: that time was not this device's.
 */
#define DWT_TIGHT_NS (4 * SCALE_US)
#define DWT_BREAK_NS (10 * SCALE_US)
#define DWT_STALE_NS (20 * SCALE_MS)
enum { DWT_COUNT, DWT_COUNT_FRESH };

/* (most: no more than this is taken out) */
static void dwt_steal_look(WB55DwtState *s, int64_t now, int how, int64_t most)
{
#ifdef CONFIG_LINUX
    bool count = how == DWT_COUNT ||
                 (how == DWT_COUNT_FRESH && now - s->look_ns <= DWT_STALE_NS);
    unsigned long long ran, waited;
    char buf[80];
    ssize_t n;

    s->look_ns = now;
    if (s->steal_fd == -2) {            /* (the first look: this thread's) */
        s->steal_fd = open("/proc/thread-self/schedstat", O_RDONLY | O_CLOEXEC);
        s->run_delay = -1;
    }
    if (s->steal_fd < 0) {
        return;
    }
    n = pread(s->steal_fd, buf, sizeof(buf) - 1, 0);
    if (n <= 0) {
        return;
    }
    buf[n] = '\0';
    if (sscanf(buf, "%llu %llu", &ran, &waited) != 2) {
        return;
    }
    if (count && s->run_delay >= 0 && (int64_t)waited > s->run_delay) {
        qatomic_set(&s->steal_ns, s->steal_ns +
                    MIN((int64_t)waited - s->run_delay, most));
    }
    s->run_delay = waited;
#endif
}

int64_t wb55_cpu_ran_ns(void)
{
#ifdef CONFIG_LINUX
    struct timespec ts;
    clockid_t id;

    if (first_cpu && first_cpu->thread &&
        !pthread_getcpuclockid(first_cpu->thread->thread, &id) &&
        !clock_gettime(id, &ts)) {
        return ts.tv_sec * NANOSECONDS_PER_SECOND + ts.tv_nsec;
    }
#endif
    return -1;
}

bool wb55_cpu_asleep(void)
{
    return first_cpu && qatomic_read(&first_cpu->halted);
}

int64_t wb55_dwt_cpu_ns(WB55DwtState *s)
{
    return MAX(qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)
               - qatomic_read(&s->steal_ns), qatomic_read(&s->read_cpu));
}

int64_t wb55_dwt_cpu_look(WB55DwtState *s)
{
    if (first_cpu && qemu_cpu_is_self(first_cpu)) {
        dwt_steal_look(s, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL),
                       DWT_COUNT_FRESH, INT64_MAX);
    }
    return wb55_dwt_cpu_ns(s);
}

#define DWT_HEAD_NS (20 * SCALE_US)     /* a wait begins this soon, or is none */

void wb55_dwt_mark(WB55DwtState *s, int which)
{
    s->span_first[which] = -1;
    s->span_reads[which] = 0;
}

int64_t wb55_dwt_waited(WB55DwtState *s, int which, int64_t from, int64_t to,
                        int64_t tail)
{
    int64_t first = s->span_first[which], last = s->read_cpu;
    int reads = s->span_reads[which];

    wb55_dwt_mark(s, which);
    if (first < 0 || reads < 2 || last < first ||
        first - from > DWT_HEAD_NS || to - last > tail ||
        last - first > (int64_t)reads * DWT_TIGHT_NS) {
        return -1;
    }
    return last - first;
}

/* the count at a moment of the CPU's time */
static uint64_t dwt_cycles_at(WB55DwtState *s, int64_t when)
{
    if (!(s->ctrl & 1)) {
        return s->base_cycles;
    }
    return s->base_cycles + muldiv64(MAX(when - s->base_ns, 0),
                                     clock_get_hz(s->clk),
                                     NANOSECONDS_PER_SECOND);
}

/*
 * (when: the machine's time.  A series of stamps, an edge after an edge,
 * goes by the steal at its start: what the firmware takes of them is the
 * time between two, which is the edges' own then.)
 */
void wb55_dwt_stamp_next(WB55DwtState *s, int64_t when)
{
    if (when - s->stamp_last_ns > 10 * SCALE_MS || when < s->stamp_last_ns) {
        s->stamp_steal = qatomic_read(&s->steal_ns);
    }
    s->stamp_last_ns = when;
    s->stamp_ns = when - s->stamp_steal;
    qatomic_set(&s->stamped, true);
}

/*
 * CYCCNT is read without the big lock (see wb55_dwt_init), and that is the
 * vCPU's thread: what it reads here changes only in that thread (a write of
 * the firmware's to this block or to RCC) or while it stands still (a
 * reset).  The stamp is another device's, set under the lock.
 */
static uint32_t dwt_read_cyccnt(WB55DwtState *s)
{
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    int64_t gap = now - s->read_ns;
    int64_t cpu;

    if (s->span_first[0] < 0 || s->span_first[1] < 0) {
        /* the first read since a device's mark: the wait that it times */
        dwt_steal_look(s, now, DWT_COUNT_FRESH, INT64_MAX);
    }
    if (gap < DWT_TIGHT_NS) {
        s->tight += s->tight < 3;
    } else {
        if (s->tight == 3 && gap > DWT_BREAK_NS) {
            /* a wait's loop stood still: the host's doing, as far as the
             * kernel has such time for the thread */
            dwt_steal_look(s, now, DWT_COUNT, gap);
        }
        s->tight = 0;
    }
    cpu = MAX(now - s->steal_ns, s->read_cpu);
    for (int i = 0; i < WB55_DWT_SPANS; i++) {
        if (s->span_first[i] < 0) {
            s->span_first[i] = cpu;
        }
        s->span_reads[i]++;
    }
    qatomic_set(&s->read_cpu, cpu);
    qatomic_set(&s->read_ns, now);
    if (qatomic_read(&s->stamped)) {
        BQL_LOCK_GUARD();
        if (s->stamped) {
            s->stamped = false;
            return (uint32_t)dwt_cycles_at(s, s->stamp_ns);
        }
    }
    return (uint32_t)dwt_cycles_at(s, cpu);
}

static void dwt_rebase(WB55DwtState *s)
{
    int64_t cpu = wb55_dwt_cpu_ns(s);

    s->base_cycles = dwt_cycles_at(s, cpu);
    s->base_ns = cpu;
}

static void dwt_clk_cb(void *opaque, ClockEvent event)
{
    dwt_rebase(opaque);                 /* before HCLK changes */
}

static uint64_t wb55_dwt_read(void *opaque, hwaddr addr, unsigned size)
{
    WB55DwtState *s = opaque;
    static const uint8_t id[12] = {     /* PID4..7, PID0..3, CID0..3 */
        0x04, 0, 0, 0, 0x02, 0xB0, 0x3B, 0x00, 0x0D, 0xE0, 0x05, 0xB1,
    };

    switch (addr) {
    case 0x000:
        return s->ctrl | 0x40000000;    /* NUMCOMP = 4 */
    case 0x004:
        return dwt_read_cyccnt(s);
    }
    if (addr >= 0x020 && addr < 0x060) {
        return s->comp[(addr - 0x020) / 4];
    }
    if (addr >= 0xFD0) {
        return id[(addr - 0xFD0) / 4];
    }
    return 0;
}

static void wb55_dwt_write(void *opaque, hwaddr addr, uint64_t val,
                           unsigned size)
{
    WB55DwtState *s = opaque;

    switch (addr) {
    case 0x000:
        dwt_rebase(s);
        s->ctrl = val & 0x0FFFFFFF;
        return;
    case 0x004:
        s->base_cycles = (uint32_t)val;
        s->base_ns = wb55_dwt_cpu_ns(s);
        return;
    }
    if (addr >= 0x020 && addr < 0x060) {
        s->comp[(addr - 0x020) / 4] = val;  /* watchpoints: not modelled */
    }
}

static const MemoryRegionOps wb55_dwt_ops = {
    .read = wb55_dwt_read,
    .write = wb55_dwt_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .impl.min_access_size = 4, .impl.max_access_size = 4,
    .valid.min_access_size = 4, .valid.max_access_size = 4,
};

static void wb55_dwt_reset_hold(Object *obj, ResetType type)
{
    WB55DwtState *s = WB55_DWT(obj);

    s->ctrl = 0;
    s->stamped = false;
    s->read_ns = 0;
    s->read_cpu = 0;
    s->base_cycles = 0;
    s->base_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) - s->steal_ns;
    for (int i = 0; i < WB55_DWT_SPANS; i++) {
        wb55_dwt_mark(s, i);
    }
    memset(s->comp, 0, sizeof(s->comp));
}

static void wb55_dwt_init(Object *obj)
{
    WB55DwtState *s = WB55_DWT(obj);

    memory_region_init_io(&s->mmio, obj, &wb55_dwt_ops, s, "dwt", 0x1000);
    /*
     * furi_delay_us() reads CYCCNT in a loop, and each read took the big
     * lock: when the main loop wanted it for a timer, the vCPU's thread
     * slept until it had it back, 20 to 50 us, and a 9 us wait had lasted
     * that long.  Reads need no lock (qemu/patches/0005).
     */
    memory_region_enable_lockless_io(&s->mmio);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    s->clk = qdev_init_clock_in(DEVICE(obj), "clk", dwt_clk_cb, s,
                                ClockPreUpdate);
    s->steal_fd = -2;                   /* (not looked for yet) */
}

/*
 * The time taken out of the CPU's own (base_ns is on that time): a state
 * that was saved without it had none taken out.  The kernel's count is
 * this process's and begins anew in the next.
 */
static bool dwt_steal_needed(void *opaque)
{
    WB55DwtState *s = opaque;

    return s->steal_ns != 0;
}

static int dwt_post_load(void *opaque, int version_id)
{
    WB55DwtState *s = opaque;

    s->read_cpu = s->read_ns - s->steal_ns;
    s->stamp_steal = s->steal_ns;
    for (int i = 0; i < WB55_DWT_SPANS; i++) {
        wb55_dwt_mark(s, i);
    }
    return 0;
}

static int dwt_pre_load(void *opaque)
{
    WB55DwtState *s = opaque;

    s->steal_ns = 0;
    return 0;
}

static const VMStateDescription vmstate_wb55_dwt_steal = {
    .name = "stm32wb55-dwt/steal",
    .version_id = 1,
    .minimum_version_id = 1,
    .needed = dwt_steal_needed,
    .fields = (const VMStateField[]) {
        VMSTATE_INT64(steal_ns, WB55DwtState),
        VMSTATE_END_OF_LIST()
    }
};

static const VMStateDescription vmstate_wb55_dwt = {
    .name = "stm32wb55-dwt",
    .version_id = 1,
    .minimum_version_id = 1,
    .pre_load = dwt_pre_load,
    .post_load = dwt_post_load,
    .subsections = (const VMStateDescription * const []) {
        &vmstate_wb55_dwt_steal,
        NULL
    },
    .fields = (const VMStateField[]) {
        VMSTATE_CLOCK(clk, WB55DwtState),
        VMSTATE_UINT32(ctrl, WB55DwtState),
        VMSTATE_UINT64(base_cycles, WB55DwtState),
        VMSTATE_INT64(base_ns, WB55DwtState),
        VMSTATE_UINT32_ARRAY(comp, WB55DwtState, 16),
        VMSTATE_BOOL(stamped, WB55DwtState),
        VMSTATE_INT64(stamp_ns, WB55DwtState),
        VMSTATE_INT64(read_ns, WB55DwtState),
        VMSTATE_END_OF_LIST()
    }
};

static void wb55_dwt_class_init(ObjectClass *klass, const void *data)
{
    RESETTABLE_CLASS(klass)->phases.hold = wb55_dwt_reset_hold;
    DEVICE_CLASS(klass)->vmsd = &vmstate_wb55_dwt;
}

/* ---------------------------------------------------------------- DBGMCU */

static const VMStateDescription vmstate_wb55_dbgmcu = {
    .name = "stm32wb55-dbgmcu",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32_ARRAY(regs, WB55DbgmcuState, 0x54 / 4),
        VMSTATE_END_OF_LIST()
    }
};

static void wb55_dbgmcu_class_init(ObjectClass *klass, const void *data)
{
    DEVICE_CLASS(klass)->vmsd = &vmstate_wb55_dbgmcu;
}

static uint64_t wb55_dbgmcu_read(void *opaque, hwaddr addr, unsigned size)
{
    WB55DbgmcuState *s = opaque;

    if (addr == 0) {
        return 0x20016495;              /* REV_ID 0x2001 (rev Y), DEV_ID 0x495 */
    }
    return addr / 4 < ARRAY_SIZE(s->regs) ? s->regs[addr / 4] : 0;
}

static void wb55_dbgmcu_write(void *opaque, hwaddr addr, uint64_t val,
                              unsigned size)
{
    WB55DbgmcuState *s = opaque;

    if (addr && addr / 4 < ARRAY_SIZE(s->regs)) {
        s->regs[addr / 4] = val;
    }
}

static const MemoryRegionOps wb55_dbgmcu_ops = {
    .read = wb55_dbgmcu_read,
    .write = wb55_dbgmcu_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .impl.min_access_size = 4, .impl.max_access_size = 4,
    .valid.min_access_size = 1, .valid.max_access_size = 4,
};

static void wb55_dbgmcu_init(Object *obj)
{
    WB55DbgmcuState *s = WB55_DBGMCU(obj);

    memory_region_init_io(&s->mmio, obj, &wb55_dbgmcu_ops, s, "dbgmcu", 0x400);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
}

/* -------------------------------------------------------------- regfile */

static void regfile_tim_write(WB55RegfileState *s, hwaddr addr, uint32_t val)
{
    switch (addr) {
    case 0x10:
        s->regs[0x10 / 4] &= val;       /* SR: rc_w0 */
        return;
    case 0x14:
        if (val & 1u) {                 /* EGR.UG: update event */
            s->regs[0x24 / 4] = 0;
            if (!(s->regs[0x00 / 4] & (1u << 2))) {   /* !URS */
                s->regs[0x10 / 4] |= 1u;
            }
        }
        s->regs[0x10 / 4] |= val & 0x1Eu;
        return;
    }
    s->regs[addr / 4] = val;
}

static uint64_t wb55_regfile_read(void *opaque, hwaddr addr, unsigned size)
{
    WB55RegfileState *s = opaque;

    if (!s->warned) {
        s->warned = true;
        qemu_log_mask(LOG_UNIMP, "%s: register-file model in use\n", s->name);
    }
    if (addr / 4 >= s->size / 4) {
        return 0;
    }
    return extract32(s->regs[addr / 4], (addr & 3) * 8, size * 8);
}

static void wb55_regfile_write(void *opaque, hwaddr addr, uint64_t val,
                               unsigned size)
{
    WB55RegfileState *s = opaque;

    if (addr / 4 >= s->size / 4) {
        return;
    }
    if (size < 4) {                     /* byte/halfword: read-modify-write */
        val = deposit32(s->regs[addr / 4], (addr & 3) * 8, size * 8, val);
        addr &= ~3;
    }
    switch (s->kind) {
    case WB55_RF_TIM: regfile_tim_write(s, addr, val); break;
    default:          s->regs[addr / 4] = val; break;
    }
    if (s->changed) {
        s->changed(s->changed_opaque);
    }
}

static const MemoryRegionOps wb55_regfile_ops = {
    .read = wb55_regfile_read,
    .write = wb55_regfile_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .impl.min_access_size = 1, .impl.max_access_size = 4,
    /* LDRD/STRD arrive as one 8-byte access; split it like the bus does */
    .valid.min_access_size = 1, .valid.max_access_size = 8,
};

static void wb55_regfile_reset_hold(Object *obj, ResetType type)
{
    WB55RegfileState *s = WB55_REGFILE(obj);

    if (s->regs) {
        memset(s->regs, 0, s->size);
    }
}

static void wb55_regfile_reset_exit(Object *obj, ResetType type)
{
    WB55RegfileState *s = WB55_REGFILE(obj);

    if (s->changed) {
        s->changed(s->changed_opaque);
    }
}

static void wb55_regfile_realize(DeviceState *dev, Error **errp)
{
    WB55RegfileState *s = WB55_REGFILE(dev);

    s->nregs = s->size / 4;
    s->regs = g_new0(uint32_t, s->nregs);
    memory_region_init_io(&s->mmio, OBJECT(dev), &wb55_regfile_ops, s,
                          s->name ? s->name : "regfile", s->size);
    sysbus_init_mmio(SYS_BUS_DEVICE(dev), &s->mmio);
}

static const Property wb55_regfile_props[] = {
    DEFINE_PROP_STRING("name", WB55RegfileState, name),
    DEFINE_PROP_UINT32("size", WB55RegfileState, size, 0x400),
    DEFINE_PROP_UINT32("kind", WB55RegfileState, kind, WB55_RF_PLAIN),
};

static const VMStateDescription vmstate_wb55_regfile = {
    .name = "stm32wb55-regfile",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_VARRAY_UINT32(regs, WB55RegfileState, nregs, 0,
                              vmstate_info_uint32, uint32_t),
        VMSTATE_END_OF_LIST()
    }
};

static void wb55_regfile_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);

    dc->realize = wb55_regfile_realize;
    device_class_set_props(dc, wb55_regfile_props);
    RESETTABLE_CLASS(klass)->phases.hold = wb55_regfile_reset_hold;
    RESETTABLE_CLASS(klass)->phases.exit = wb55_regfile_reset_exit;
    dc->vmsd = &vmstate_wb55_regfile;
}

static const TypeInfo wb55_misc_types[] = {
    {
        .name = TYPE_WB55_RNG,
        .parent = TYPE_SYS_BUS_DEVICE,
        .instance_size = sizeof(WB55RngState),
        .instance_init = wb55_rng_init,
        .class_init = wb55_rng_class_init,
    }, {
        .name = TYPE_WB55_DWT,
        .parent = TYPE_SYS_BUS_DEVICE,
        .instance_size = sizeof(WB55DwtState),
        .instance_init = wb55_dwt_init,
        .class_init = wb55_dwt_class_init,
    }, {
        .name = TYPE_WB55_DBGMCU,
        .parent = TYPE_SYS_BUS_DEVICE,
        .instance_size = sizeof(WB55DbgmcuState),
        .instance_init = wb55_dbgmcu_init,
        .class_init = wb55_dbgmcu_class_init,
    }, {
        .name = TYPE_WB55_REGFILE,
        .parent = TYPE_SYS_BUS_DEVICE,
        .instance_size = sizeof(WB55RegfileState),
        .class_init = wb55_regfile_class_init,
    },
};
DEFINE_TYPES(wb55_misc_types)
