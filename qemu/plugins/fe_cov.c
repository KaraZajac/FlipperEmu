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
    }
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

    g_mutex_lock(&lock);
    if (!strcmp(line, "stat")) {
        reply = g_strdup_printf("ok blocks=%u executed=%u resets=%u\n",
                                blocks->len, executed(), resets);
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
        reply = g_strdup("error stat, reset, dump PATH, news or forget\n");
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
        } else {
            fprintf(stderr, "fe_cov: %s: its options are sock=PATH, out=PATH "
                    "and keep=ADDRESS\n", argv[i]);
            return -1;
        }
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
