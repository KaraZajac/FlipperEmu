/*
 * FlipperEmu's coverage plugin: which code the firmware executed.
 *
 * A TCG plugin for qemu-system-arm (scripts/setup_qemu.sh builds it into
 * build/plugins/libfe_cov.so; `./flipperemu run --coverage` loads it).
 * Each block of code that QEMU translates (a run of instructions with one
 * way in) gets a counter, which QEMU's own generated code adds 1 to when
 * the block runs: no call out of the translated code, so a Flipper under
 * coverage runs about as fast as one without.  A block is its first
 * address and its length in bytes; two blocks may overlap (a jump into the
 * middle of one makes another).
 *
 * The counters are asked for on a socket (sock=PATH), a line to a
 * connection:
 *
 *   stat          ->  ok blocks=N executed=M resets=R
 *   reset         ->  ok            counted from now on
 *   dump PATH     ->  ok blocks=M   the blocks that ran since the reset
 *   news          ->  ok new=N      blocks that ran since `forget` and that
 *                                   no `news` has counted yet (a fuzzer's
 *                                   question: did this input get anywhere
 *                                   new?)
 *   forget        ->  ok            `news` begins anew: what runs from now
 *                                   on is new again, once
 *
 * and written at QEMU's exit with out=PATH.  A dump is text:
 *
 *   # flipperemu coverage 1
 *   0800a4f0 12 3          (a block's address, its bytes, how often it ran)
 *   20004a10 6 1 08b5fff7feff      (one in RAM: and the bytes themselves)
 *
 * Code in RAM is an app's that the firmware loaded where its heap had room,
 * or an updater's, and that room is another's later.  So a block there is
 * kept with the bytes it had when it was translated (from keep=ADDRESS up;
 * default 0x10000000, which is above a Flipper's flash): the same address
 * with other bytes is another block, and what ran can be told an app's by
 * its bytes, after the app has ended too.  The launcher gives the address
 * where the firmware in the flash ends: the flash above it is free, and
 * what runs there was written by a loader of apps that run in place, to be
 * another's later as well.
 *
 * The machine runs on meanwhile.  A reset writes no counter (the vCPU's
 * thread adds to them, and a write beside it could be lost): it notes what
 * each counter has, and a dump says what came since.
 *
 * With trace=N it keeps the order too: the last N blocks that ran (N a
 * power of two), a block that ran again at once counted and not put down
 * twice, so that a loop of one block (a firmware that has halted) leaves
 * what came before it.  What ran is no use once the firmware has started
 * again or a fault's handler has run on for a while, so the trace is copied
 * at those moments: when the block at the reset vector's address runs
 * ("start": what ran before the firmware started, the end of its last
 * life), and when one at the HardFault, MemManage, BusFault or UsageFault
 * vector's does ("fault").  The vectors are read from the flash's start
 * (0x08000000), where a Flipper's firmware has them.  `trace PATH
 * now|start|fault` writes one of the three, the oldest block first:
 *
 *   # flipperemu trace 1 start 2 0800a001  (which; how many such moments so
 *                                           far; the vector's address)
 *   0800a4f0 12 3          (a block's address, its bytes, how often in a row)
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include <errno.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <glib.h>

#include <qemu-plugin.h>

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;

/* The counters live in QEMU's scoreboards, which its inline operations
 * add to.  A scoreboard has room for every vCPU that might come (16 at
 * least), so one with all the counters would be large from the start: they
 * come in chunks instead, as blocks are translated. */
#define CHUNK       16384               /* counters to a scoreboard */
#define CHUNKS_MAX  512                 /* 8 million blocks */

typedef struct Block {
    uint64_t key;                       /* address << 32 | mark << 16 | bytes */
    uint32_t id;
    uint8_t *code;                      /* its bytes, of one in RAM */
} Block;

static GMutex lock;
static GHashTable *by_key;              /* Block.key -> Block */
static GPtrArray *blocks;               /* Block, by id */
static struct qemu_plugin_scoreboard *chunk[CHUNKS_MAX];
static uint64_t *base[CHUNKS_MAX];      /* the counters at the last reset */
static uint64_t *mark[CHUNKS_MAX];      /* the counters at the last forget */
static uint8_t *seen[CHUNKS_MAX];       /* a block that `news` has counted */
static bool full;
static unsigned resets;

/* the trace */
enum { RING_NOW, RING_START, RING_FAULT, RINGS };
enum { KIND_START = 1, KIND_FAULT };    /* what a block is the first of */
#define VECTORS     0x08000000u         /* the firmware's vector table */
#define ID_BITS     28                  /* (a block's id; its kind above) */

typedef struct Step {
    uint32_t id, times;
} Step;

typedef struct Ring {
    Step *e;
    uint64_t at;                        /* steps put down so far */
    unsigned moments;                   /* how often it was copied */
    uint32_t vector;                    /* the handler's address, as read */
} Ring;

static uint32_t trace_size;             /* 0: no trace */
static Ring ring[RINGS];
static Ring copied;                     /* RING_NOW, for who asked */
static int copy_wanted;                 /* 1: asked for; 2: made */
static uint32_t vec_start, vec_fault[4];
static char *sock_path, *out_path;
static uint64_t keep_from = 0x10000000; /* blocks from here up: with bytes */
static int listener = -1;

static uint64_t *counter(uint32_t id)
{
    uint64_t *c = qemu_plugin_scoreboard_find(chunk[id / CHUNK], 0);

    return &c[id % CHUNK];
}

/* how often a block ran since the last reset */
static uint64_t count_of(uint32_t id)
{
    return *counter(id) - base[id / CHUNK][id % CHUNK];
}

static void ring_copy(Ring *to, const Ring *from)
{
    memcpy(to->e, from->e, trace_size * sizeof(Step));
    to->at = from->at;
}

/* a block runs (the vCPU's thread; nothing else writes the trace) */
static void tb_exec(unsigned int vcpu_index, void *udata)
{
    uintptr_t v = (uintptr_t)udata;
    uint32_t id = v & ((1u << ID_BITS) - 1), kind = v >> ID_BITS;
    Ring *r = &ring[RING_NOW];
    Step *last = &r->e[(r->at - 1) & (trace_size - 1)];

    if (kind) {
        Ring *to = &ring[kind == KIND_START ? RING_START : RING_FAULT];

        ring_copy(to, r);               /* what ran up to here */
        to->moments++;
        to->vector = kind == KIND_START ? vec_start : 0;
    }
    if (r->at && last->id == id && last->times != UINT32_MAX) {
        last->times++;
    } else {
        r->e[r->at++ & (trace_size - 1)] = (Step){ id, 1 };
    }
    if (g_atomic_int_get(&copy_wanted) == 1) {
        ring_copy(&copied, r);
        g_atomic_int_set(&copy_wanted, 2);
    }
}

/* What is this block the first of?  By the firmware's vector table, read
 * again each time: a firmware may be another after an update. */
static uint32_t block_kind(uint64_t pc)
{
    g_autoptr(GByteArray) v = g_byte_array_new();

    if (!qemu_plugin_read_memory_vaddr(VECTORS + 4, v, 6 * 4) ||
        v->len < 6 * 4) {
        return 0;
    }
    for (int i = 0; i < 6; i++) {
        uint32_t at = (v->data[4 * i] | v->data[4 * i + 1] << 8 |
                       v->data[4 * i + 2] << 16 |
                       (uint32_t)v->data[4 * i + 3] << 24) & ~1u;

        if (i == 0) {
            vec_start = at;
        } else if (i >= 2) {            /* (1 is the NMI's) */
            vec_fault[i - 2] = at;
        }
    }
    if (pc == vec_start) {
        return KIND_START;
    }
    for (int i = 0; i < 4; i++) {
        if (pc == vec_fault[i]) {
            return KIND_FAULT;
        }
    }
    return 0;
}

static void tb_trans(struct qemu_plugin_tb *tb, void *unused)
{
    size_t n = qemu_plugin_tb_n_insns(tb);
    struct qemu_plugin_insn *last = qemu_plugin_tb_get_insn(tb, n - 1);
    uint64_t pc = qemu_plugin_tb_vaddr(tb);
    uint64_t end = qemu_plugin_insn_vaddr(last) + qemu_plugin_insn_size(last);
    uint32_t size = end - pc;
    uint64_t key = pc << 32 | (size & 0xFFFF);
    g_autofree uint8_t *code = NULL;
    Block *b;

    if (size > 0xFFFF) {
        return;                         /* (no block is that long) */
    }
    if (pc >= keep_from) {
        uint32_t mark = 5381;
        size_t at = 0;

        code = g_malloc(size);
        for (size_t i = 0; i < n; i++) {
            struct qemu_plugin_insn *insn = qemu_plugin_tb_get_insn(tb, i);
            size_t len = qemu_plugin_insn_size(insn);

            if (at + len > size ||
                qemu_plugin_insn_data(insn, code + at, len) != len) {
                return;
            }
            at += len;
        }
        for (size_t i = 0; i < size; i++) {
            mark = mark * 33 + code[i];
        }
        key |= (uint64_t)(mark & 0xFFFF) << 16;
    }
    g_mutex_lock(&lock);
    b = g_hash_table_lookup(by_key, &key);
    while (b && code && memcmp(b->code, code, size)) {
        key += 1 << 16;                 /* (two with one mark: the next) */
        b = g_hash_table_lookup(by_key, &key);
    }
    if (!b && !full) {
        uint32_t next = blocks->len;

        if (next / CHUNK >= CHUNKS_MAX) {
            full = true;
            fprintf(stderr, "fe_cov: %u blocks: no counters for more\n", next);
        } else {
            if (!chunk[next / CHUNK]) {
                chunk[next / CHUNK] =
                    qemu_plugin_scoreboard_new(CHUNK * sizeof(uint64_t));
                base[next / CHUNK] = g_new0(uint64_t, CHUNK);
                mark[next / CHUNK] = g_new0(uint64_t, CHUNK);
                seen[next / CHUNK] = g_new0(uint8_t, CHUNK);
            }
            b = g_new0(Block, 1);
            b->key = key;
            b->id = next;
            b->code = g_steal_pointer(&code);
            g_ptr_array_add(blocks, b);
            g_hash_table_insert(by_key, &b->key, b);
        }
    }
    g_mutex_unlock(&lock);
    if (b) {
        qemu_plugin_u64 at = { chunk[b->id / CHUNK],
                               (b->id % CHUNK) * sizeof(uint64_t) };

        qemu_plugin_register_vcpu_tb_exec_inline_per_vcpu(
            tb, QEMU_PLUGIN_INLINE_ADD_U64, at, 1);
        if (trace_size) {
            uintptr_t v = b->id | (uintptr_t)block_kind(pc) << ID_BITS;

            qemu_plugin_register_vcpu_tb_exec_cb(tb, tb_exec,
                                                 QEMU_PLUGIN_CB_NO_REGS,
                                                 (void *)v);
        }
    }
}

/* trace PATH now|start|fault -> the steps written, -1: errno, -2: no such */
static int trace_dump(const char *path, const char *which)
{
    static const char *const names[RINGS] = { "now", "start", "fault" };
    Ring *r = NULL;
    uint64_t from;
    FILE *f;
    int n = 0;

    for (int i = 0; i < RINGS; i++) {
        if (!strcmp(which, names[i])) {
            r = &ring[i];
        }
    }
    if (!r || !trace_size) {
        return -2;
    }
    if (r == &ring[RING_NOW]) {
        /* the vCPU's thread makes the copy when it next runs a block; a
         * CPU that sleeps runs none, and its trace stands still */
        g_atomic_int_set(&copy_wanted, 1);
        for (int i = 0; i < 300 && g_atomic_int_get(&copy_wanted) != 2; i++) {
            g_usleep(1000);
        }
        if (g_atomic_int_get(&copy_wanted) != 2) {
            ring_copy(&copied, r);
        }
        g_atomic_int_set(&copy_wanted, 0);
        copied.moments = 0;
        copied.vector = 0;
        r = &copied;
    }
    f = fopen(path, "w");
    if (!f) {
        return -1;
    }
    fprintf(f, "# flipperemu trace 1 %s %u %08x\n", which, r->moments,
            r->vector);
    from = r->at > trace_size ? r->at - trace_size : 0;
    g_mutex_lock(&lock);
    for (uint64_t i = from; i < r->at; i++, n++) {
        Step *s = &r->e[i & (trace_size - 1)];
        Block *b = s->id < blocks->len ? g_ptr_array_index(blocks, s->id)
                                       : NULL;

        if (b) {
            fprintf(f, "%08x %u %u\n", (uint32_t)(b->key >> 32),
                    (uint32_t)(b->key & 0xFFFF), s->times);
        }
    }
    g_mutex_unlock(&lock);
    if (fclose(f)) {
        return -1;
    }
    return n;
}

/* (the lock held) */
static unsigned executed(void)
{
    unsigned n = 0;

    for (unsigned i = 0; i < blocks->len; i++) {
        n += count_of(i) != 0;
    }
    return n;
}

/* (the lock held) -> the blocks written, or -1 */
static int dump(const char *path)
{
    g_autofree char *tmp = g_strdup_printf("%s.tmp", path);
    FILE *f = fopen(tmp, "w");
    int n = 0;

    if (!f) {
        return -1;
    }
    fprintf(f, "# flipperemu coverage 1\n");
    for (unsigned i = 0; i < blocks->len; i++) {
        Block *b = g_ptr_array_index(blocks, i);
        uint64_t count = count_of(i);

        if (count) {
            uint32_t size = b->key & 0xFFFF;

            fprintf(f, "%08" PRIx64 " %u %" PRIu64, b->key >> 32, size, count);
            if (b->code) {
                fputc(' ', f);
                for (uint32_t k = 0; k < size; k++) {
                    fprintf(f, "%02x", b->code[k]);
                }
            }
            fputc('\n', f);
            n++;
        }
    }
    if (fclose(f) != 0 || rename(tmp, path) != 0) {
        unlink(tmp);
        return -1;
    }
    return n;
}

static void answer(int c, const char *line)
{
    g_autofree char *reply = NULL;

    if (g_str_has_prefix(line, "trace ")) {
        /* (not under the lock: the vCPU's thread may want it to translate) */
        g_auto(GStrv) w = g_strsplit(line + 6, " ", 2);
        int n = w[0] && w[1] ? trace_dump(w[0], w[1]) : -2;

        reply = n == -2 ? g_strdup(trace_size ?
                    "error trace PATH now|start|fault\n" :
                    "error no trace is kept (trace=N)\n")
              : n < 0 ? g_strdup_printf("error %s: %s\n", w[0],
                                        g_strerror(errno))
              : g_strdup_printf("ok steps=%d\n", n);
        if (write(c, reply, strlen(reply)) < 0) {
            /* (it hung up) */
        }
        return;
    }
    g_mutex_lock(&lock);
    if (!strcmp(line, "stat")) {
        reply = g_strdup_printf("ok blocks=%u executed=%u resets=%u trace=%u "
                                "starts=%u faults=%u\n",
                                blocks->len, executed(), resets, trace_size,
                                ring[RING_START].moments,
                                ring[RING_FAULT].moments);
    } else if (!strcmp(line, "reset")) {
        for (unsigned i = 0; i < blocks->len; i++) {
            base[i / CHUNK][i % CHUNK] = *counter(i);
        }
        resets++;
        reply = g_strdup("ok\n");
    } else if (!strcmp(line, "news")) {
        unsigned n = 0;

        for (unsigned i = 0; i < blocks->len; i++) {
            if (!seen[i / CHUNK][i % CHUNK] &&
                *counter(i) != mark[i / CHUNK][i % CHUNK]) {
                seen[i / CHUNK][i % CHUNK] = 1;
                n++;
            }
        }
        reply = g_strdup_printf("ok new=%u\n", n);
    } else if (!strcmp(line, "forget")) {
        for (unsigned i = 0; i < blocks->len; i++) {
            mark[i / CHUNK][i % CHUNK] = *counter(i);
            seen[i / CHUNK][i % CHUNK] = 0;
        }
        reply = g_strdup("ok\n");
    } else if (g_str_has_prefix(line, "dump ") && line[5]) {
        int n = dump(line + 5);

        reply = n < 0 ? g_strdup_printf("error %s: %s\n", line + 5,
                                        g_strerror(errno))
                      : g_strdup_printf("ok blocks=%d\n", n);
    } else {
        reply = g_strdup("error stat, reset, dump PATH, news, forget or "
                         "trace PATH now|start|fault\n");
    }
    g_mutex_unlock(&lock);
    if (write(c, reply, strlen(reply)) < 0) {
        /* (it hung up) */
    }
}

static gpointer serve(gpointer unused)
{
    for (;;) {
        char line[4200];
        size_t have = 0;
        int c = accept(listener, NULL, NULL);

        if (c < 0) {
            if (errno == EINTR) {
                continue;
            }
            return NULL;                /* (closed: QEMU ends) */
        }
        while (have < sizeof(line) - 1) {
            ssize_t got = read(c, line + have, sizeof(line) - 1 - have);

            if (got <= 0) {
                break;
            }
            have += got;
            if (memchr(line, '\n', have)) {
                break;
            }
        }
        line[have] = '\0';
        line[strcspn(line, "\r\n")] = '\0';
        answer(c, line);
        close(c);
    }
}

static void at_exit(void *unused)
{
    if (out_path) {
        g_mutex_lock(&lock);
        if (dump(out_path) < 0) {
            fprintf(stderr, "fe_cov: %s: %s\n", out_path, g_strerror(errno));
        }
        g_mutex_unlock(&lock);
    }
    if (sock_path) {
        unlink(sock_path);
    }
}

static bool listen_on(const char *path)
{
    struct sockaddr_un sa = { .sun_family = AF_UNIX };

    if (strlen(path) >= sizeof(sa.sun_path)) {
        fprintf(stderr, "fe_cov: sock=%s: too long a path for a socket\n",
                path);
        return false;
    }
    strcpy(sa.sun_path, path);
    unlink(path);
    listener = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (listener < 0 || bind(listener, (struct sockaddr *)&sa,
                             sizeof(sa)) < 0 || listen(listener, 4) < 0) {
        fprintf(stderr, "fe_cov: sock=%s: %s\n", path, g_strerror(errno));
        return false;
    }
    g_thread_unref(g_thread_new("fe_cov", serve, NULL));
    return true;
}

QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t id,
                                           const qemu_info_t *info,
                                           int argc, char **argv)
{
    for (int i = 0; i < argc; i++) {
        g_auto(GStrv) kv = g_strsplit(argv[i], "=", 2);

        if (kv[1] && !strcmp(kv[0], "sock")) {
            sock_path = g_strdup(kv[1]);
        } else if (kv[1] && !strcmp(kv[0], "out")) {
            out_path = g_strdup(kv[1]);
        } else if (kv[1] && !strcmp(kv[0], "keep")) {
            keep_from = g_ascii_strtoull(kv[1], NULL, 0);
        } else if (kv[1] && !strcmp(kv[0], "trace")) {
            trace_size = g_ascii_strtoull(kv[1], NULL, 0);
            if (trace_size & (trace_size - 1)) {
                fprintf(stderr, "fe_cov: trace=%s: a power of two\n", kv[1]);
                return -1;
            }
        } else {
            fprintf(stderr, "fe_cov: %s: its options are sock=PATH, out=PATH, "
                    "keep=ADDRESS and trace=N\n", argv[i]);
            return -1;
        }
    }
    if (trace_size) {
        for (int i = 0; i < RINGS; i++) {
            ring[i].e = g_new0(Step, trace_size);
        }
        copied.e = g_new0(Step, trace_size);
    }
    by_key = g_hash_table_new(g_int64_hash, g_int64_equal);
    blocks = g_ptr_array_new();
    if (sock_path && !listen_on(sock_path)) {
        return -1;
    }
    qemu_plugin_register_vcpu_tb_trans_cb(id, tb_trans, NULL);
    qemu_plugin_register_atexit_cb(id, at_exit, NULL);
    return 0;
}
