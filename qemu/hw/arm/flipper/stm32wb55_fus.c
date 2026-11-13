/*
 * STM32WB55 FUS: customer key storage (AN5185 Rev 15, chapter 5)
 *
 * ST's Firmware Upgrade Service, on CPU2, keeps AES keys for the application
 * in flash that CPU1 cannot read: a master key at index 0 and up to 100
 * others, each written once, in the order they come, never changed or
 * removed. All CPU1 can do with one is have it put into AES1's key
 * registers, which are closed to it meanwhile (stm32wb55_aes.c). Four SHCI
 * commands, decoded in stm32wb55_ipcc.c:
 *
 *   FUS_STORE_USR_KEY    type, size, key [, IV] -> status, index
 *                        A master key (type 2) goes to index 0, once. A
 *                        simple (1) or encrypted (3) key gets the next free
 *                        index from 1. An encrypted key is AES-GCM under
 *                        the master key with a 12-byte IV, the tag not
 *                        sent: so CTR, the first counter block IV|00000002
 *   FUS_LOAD_USR_KEY     index -> the key into AES1. Never the master key,
 *                        a locked key, or an index nothing was stored at
 *   FUS_UNLOAD_USR_KEY   index -> AES1's key registers zeroed and released
 *   FUS_LOCK_USR_KEY     index -> no more loads of it until the next reset
 *
 * The status is 0x00 for done and 0x01 for failed.
 *
 * The store is a text file on the host, property "keys", written back after
 * every key stored. One key per line; `#` starts a comment:
 *
 *   0 master 256 <64 hex digits>
 *   1 simple 256 <64 hex digits>
 *   2 encrypted 128 <32 hex digits>        the key itself, decrypted
 *
 * A file that exists is taken as it is, an empty one included: that is a
 * device nobody has stored a key on. A file that doesn't exist yet is
 * created from what the board provisions (WB55IpccState.provision).
 * Without the property, the keys are provisioned in memory and gone at exit.
 *
 * What AN5185 doesn't say, and how it is here:
 *   - where a key goes in AES1: the key registers and nothing else. KEYSIZE
 *     and the rest of AES_CR are the application's, set before the load as
 *     the note tells it to; a 128-bit key lands in KEYR3..0
 *   - the index of the first key when there is no master key: 1
 *   - the cipher for an encrypted key under a 256-bit master key ("AES-128
 *     GCM" is all it says): the master key's size decides
 *   - unloading another index than the one loaded: unloads all the same
 *   - locking an index nothing is stored at, or the key now loaded: accepted;
 *     a loaded key stays loaded until it is unloaded
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/log.h"
#include "qemu/error-report.h"
#include "qemu/bswap.h"
#include "qapi/error.h"
#include "crypto/aes.h"
#include "wb55.h"

#define FUS_DONE    0x00
#define FUS_FAIL    0x01

#define FUS_IV_LEN  12

static const char *const fus_type_name[] = {
    [WB55_FUS_KEY_SIMPLE] = "simple",
    [WB55_FUS_KEY_MASTER] = "master",
    [WB55_FUS_KEY_ENCRYPTED] = "encrypted",
};

static const char fus_file_header[] =
    "# STM32WB55 FUS customer key storage (AN5185): INDEX TYPE BITS KEY\n"
    "# Index 0 is the master key; the others follow in the order they were\n"
    "# stored, without gaps. An encrypted key is kept decrypted.\n";

/* ---------------------------------------------------------- the host file */

static bool fus_parse_line(WB55IpccState *s, const char *line, Error **errp)
{
    unsigned index, bits, type;
    char name[16], hex[65], junk;
    WB55FusKey *k;

    if (sscanf(line, "%u %15s %u %64s %c", &index, name, &bits, hex,
               &junk) != 4) {
        error_setg(errp, "expected INDEX TYPE BITS KEY");
        return false;
    }
    for (type = WB55_FUS_KEY_SIMPLE; type <= WB55_FUS_KEY_ENCRYPTED; type++) {
        if (!strcmp(name, fus_type_name[type])) {
            break;
        }
    }
    if (type > WB55_FUS_KEY_ENCRYPTED) {
        error_setg(errp, "key type '%s': expected master, simple or "
                   "encrypted", name);
        return false;
    }
    if (index >= WB55_FUS_KEYS) {
        error_setg(errp, "index %u: the FUS holds keys 0 to %d", index,
                   WB55_FUS_KEYS - 1);
        return false;
    }
    if ((index == 0) != (type == WB55_FUS_KEY_MASTER)) {
        error_setg(errp, "index 0 is the master key, and only that one");
        return false;
    }
    if (bits != 128 && bits != 256) {
        error_setg(errp, "a key has 128 or 256 bits, not %u", bits);
        return false;
    }
    if (strlen(hex) != bits / 4 || strspn(hex, "0123456789abcdefABCDEF") !=
        bits / 4) {
        error_setg(errp, "a %u-bit key is %u hex digits", bits, bits / 4);
        return false;
    }
    k = &s->key[index];
    if (k->type != WB55_FUS_KEY_NONE) {
        error_setg(errp, "index %u is there twice", index);
        return false;
    }
    k->type = type;
    k->len = bits / 8;
    for (int i = 0; i < k->len; i++) {
        k->data[i] = (g_ascii_xdigit_value(hex[2 * i]) << 4) |
                     g_ascii_xdigit_value(hex[2 * i + 1]);
    }
    return true;
}

static bool fus_file_load(WB55IpccState *s, const char *text, Error **errp)
{
    g_auto(GStrv) lines = g_strsplit(text, "\n", -1);

    for (int n = 0; lines[n]; n++) {
        char *line = g_strstrip(lines[n]);
        Error *err = NULL;

        if (!*line || *line == '#') {
            continue;
        }
        if (!fus_parse_line(s, line, &err)) {
            error_setg(errp, "%s:%d: %s", s->key_file, n + 1,
                       error_get_pretty(err));
            error_free(err);
            return false;
        }
    }
    /* the FUS hands out indexes in order: a hole can't have come from it */
    for (int i = 2; i < WB55_FUS_KEYS; i++) {
        if (s->key[i].type != WB55_FUS_KEY_NONE &&
            s->key[i - 1].type == WB55_FUS_KEY_NONE) {
            error_setg(errp, "%s: key %d without a key %d: the FUS stores "
                       "them in order", s->key_file, i, i - 1);
            return false;
        }
    }
    return true;
}

static void fus_file_save(WB55IpccState *s)
{
    g_autoptr(GString) text = NULL;
    g_autoptr(GError) err = NULL;

    if (!s->key_file) {
        return;
    }
    text = g_string_new(fus_file_header);
    for (int i = 0; i < WB55_FUS_KEYS; i++) {
        const WB55FusKey *k = &s->key[i];

        if (k->type == WB55_FUS_KEY_NONE) {
            continue;
        }
        g_string_append_printf(text, "%d %s %d ", i, fus_type_name[k->type],
                               k->len * 8);
        for (int j = 0; j < k->len; j++) {
            g_string_append_printf(text, "%02x", k->data[j]);
        }
        g_string_append_c(text, '\n');
    }
    /* by rename, so a crash can't leave half a store; keys: owner only */
    if (!g_file_set_contents_full(s->key_file, text->str, text->len,
                                  G_FILE_SET_CONTENTS_CONSISTENT, 0600,
                                  &err)) {
        warn_report("flipper-cpu2: the FUS's keys could not be saved: %s",
                    err->message);
    }
}

bool wb55_fus_realize(WB55IpccState *s, Error **errp)
{
    g_autofree char *text = NULL;
    g_autoptr(GError) err = NULL;

    if (s->key_file && !*s->key_file) {
        g_clear_pointer(&s->key_file, g_free);
    }
    if (s->key_file && g_file_get_contents(s->key_file, &text, NULL, &err)) {
        return fus_file_load(s, text, errp);
    }
    if (s->key_file &&
        !g_error_matches(err, G_FILE_ERROR, G_FILE_ERROR_NOENT)) {
        error_setg(errp, "%s", err->message);
        return false;
    }
    /* a new device: as it leaves the factory */
    if (s->provision) {
        s->provision(s);
    }
    fus_file_save(s);
    return true;
}

/* -------------------------------------------------------------- the store */

/* Put a key in place, whatever was there: provisioning, and the commands. */
void wb55_fus_put_key(WB55IpccState *s, int index, int type,
                      const uint8_t *key, int len)
{
    WB55FusKey *k;

    g_assert(index >= 0 && index < WB55_FUS_KEYS);
    g_assert(len == 16 || len == 32);
    k = &s->key[index];
    memset(k, 0, sizeof(*k));
    k->type = type;
    k->len = len;
    memcpy(k->data, key, len);
}

/* The payload of AES-GCM, which is CTR from the IV with counter 2 */
static void fus_decrypt(const WB55FusKey *master, const uint8_t *iv,
                        const uint8_t *in, uint8_t *out, int len)
{
    uint8_t ctr[16], pad[16];
    AES_KEY ks;

    AES_set_encrypt_key(master->data, master->len * 8, &ks);
    memcpy(ctr, iv, FUS_IV_LEN);
    for (int off = 0; off < len; off += 16) {
        stl_be_p(ctr + FUS_IV_LEN, 2 + off / 16);
        AES_encrypt(ctr, pad, &ks);
        for (int i = 0; i < 16; i++) {
            out[off + i] = in[off + i] ^ pad[i];
        }
    }
}

static uint8_t fus_store_refused(const char *why)
{
    qemu_log_mask(LOG_GUEST_ERROR, "flipper-cpu2: FUS_STORE_USR_KEY refused: "
                  "%s\n", why);
    return FUS_FAIL;
}

/* FUS_STORE_USR_KEY. p: KeyType | KeySize | KeyData | IV if encrypted */
uint8_t wb55_fus_store_key(WB55IpccState *s, const uint8_t *p, int plen,
                           uint8_t *index)
{
    uint8_t key[32];
    int type, len, at;

    if (plen < 2) {
        return fus_store_refused("no key type and size");
    }
    type = p[0];
    len = p[1];
    if (type < WB55_FUS_KEY_SIMPLE || type > WB55_FUS_KEY_ENCRYPTED) {
        return fus_store_refused("unknown key type");
    }
    if (len != 16 && len != 32) {
        return fus_store_refused("a key is 16 or 32 bytes");
    }
    if (plen < 2 + len + (type == WB55_FUS_KEY_ENCRYPTED ? FUS_IV_LEN : 0)) {
        return fus_store_refused("the command is shorter than its key");
    }

    if (type == WB55_FUS_KEY_MASTER) {
        if (s->key[0].type != WB55_FUS_KEY_NONE) {
            return fus_store_refused("there is a master key already");
        }
        at = 0;
    } else {
        for (at = 1; at < WB55_FUS_KEYS; at++) {
            if (s->key[at].type == WB55_FUS_KEY_NONE) {
                break;
            }
        }
        if (at == WB55_FUS_KEYS) {
            return fus_store_refused("all 100 keys are taken");
        }
    }
    if (type == WB55_FUS_KEY_ENCRYPTED) {
        if (s->key[0].type == WB55_FUS_KEY_NONE) {
            return fus_store_refused("an encrypted key, and no master key "
                                     "to decrypt it");
        }
        fus_decrypt(&s->key[0], p + 2 + len, p + 2, key, len);
    } else {
        memcpy(key, p + 2, len);
    }
    wb55_fus_put_key(s, at, type, key, len);
    fus_file_save(s);
    *index = at;
    return FUS_DONE;
}

/* FUS_LOAD_USR_KEY: the key into AES1, for CPU1 to use and not to see */
uint8_t wb55_fus_load_key(WB55IpccState *s, uint8_t index)
{
    const WB55FusKey *k;
    const WB55AesAnswer *answer = NULL;

    if (index == 0 || index >= WB55_FUS_KEYS || !s->aes) {
        return FUS_FAIL;
    }
    k = &s->key[index];
    if (k->type == WB55_FUS_KEY_NONE || s->key_locked[index]) {
        return FUS_FAIL;
    }
    if (s->key_answer) {
        answer = s->key_answer(index, k->data, k->len);
    }
    wb55_aes_enclave_load(s->aes, k->data, k->len, answer);
    s->key_loaded = index;
    return FUS_DONE;
}

/* FUS_UNLOAD_USR_KEY */
uint8_t wb55_fus_unload_key(WB55IpccState *s, uint8_t index)
{
    if (index == 0 || index >= WB55_FUS_KEYS || !s->aes) {
        return FUS_FAIL;
    }
    wb55_aes_enclave_unload(s->aes);
    s->key_loaded = 0;
    return FUS_DONE;
}

/* FUS_LOCK_USR_KEY */
uint8_t wb55_fus_lock_key(WB55IpccState *s, uint8_t index)
{
    if (index == 0 || index >= WB55_FUS_KEYS) {
        return FUS_FAIL;
    }
    s->key_locked[index] = true;
    return FUS_DONE;
}

/*
 * A saved state was loaded and the machine runs.  The store is the loaded
 * one, and the file on the host has to say the same.  AES1 came with its key
 * registers; the answer that went with the key (wb55_fus_load_key()) is a
 * pointer, which no state holds: it is asked for again.
 */
void wb55_fus_loaded(WB55IpccState *s)
{
    fus_file_save(s);
    if (s->aes && s->aes->has_answer && s->key_loaded && s->key_answer) {
        const WB55FusKey *k = &s->key[s->key_loaded];

        if (k->type != WB55_FUS_KEY_NONE) {
            s->aes->answer = s->key_answer(s->key_loaded, k->data, k->len);
        }
    }
}

/* A system reset: CPU2 starts over. The locks go, and AES1 is CPU1's again. */
void wb55_fus_reset(WB55IpccState *s)
{
    memset(s->key_locked, 0, sizeof(s->key_locked));
    s->key_loaded = 0;
    if (s->aes) {
        wb55_aes_enclave_unload(s->aes);
    }
}
