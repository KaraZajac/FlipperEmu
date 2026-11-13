/*
 * STM32WB55 AES hardware accelerator (RM0434 chapter 24): AES1 and AES2
 *
 * AES-128/256 in ECB, CBC, CTR and GCM/GMAC, one block per four writes of
 * DINR, the result in four reads of DOUTR. A block is computed at once: CCF
 * is up when the fourth word has been written.
 *
 *   bytes      DINR/DOUTR words are the block's bytes 0..3, 4..7, ... as
 *              big-endian numbers (DATATYPE 00), swapped by half-words,
 *              bytes or bits for DATATYPE 01/10/11. KEYR7 (KEYR3 for 128
 *              bits) holds the key's first four bytes the same way, IVR3
 *              the IV's; IVR0 is the block counter of CTR and GCM.
 *   MODE 00    encrypt with the key in KEYR
 *   MODE 01    key derivation, on enabling: KEYR is replaced by the last
 *              round keys of its schedule, CCF, EN clears itself
 *   MODE 10    decrypt, KEYR holding a derived key. With the plain key
 *              there the result is as wrong as on the chip
 *   MODE 11    decrypt with the plain key in KEYR (derivation on the fly)
 *   CBC        IVR follows the chain, and survives EN going down: a
 *              message can be fed in several goes
 *   GCM        GCMPH 00 on enabling: H = E(K, 0), CCF, EN clears itself;
 *              01 header blocks into GHASH, nothing to read; 10 payload
 *              (CTR from IVR, the ciphertext into GHASH, less its last
 *              NPBLB bytes when encrypting); 11 the block of lengths in,
 *              the tag out. NPBLB is meant for a short last block, but it
 *              counts for every block written while it is set: software
 *              sets it for that block, and resets the peripheral after
 *              the message, as the Flipper firmware does.
 *   reset      by RCC (AHB2RSTR.AES1RST, AHB3RSTR.AES2RST): all registers,
 *              the key included
 *
 * Secure mode (SYSCFG_SIPCR.SAES1): CPU2 can take the key registers for
 * itself. They then read as zero from here and ignore writes, while the
 * rest of the peripheral works as before. ST's FUS does that when it loads
 * a stored key (AN5185, "Customer key storage": stm32wb55_fus.c), which is
 * all there is to the Flipper's "secure enclave": the firmware names a key
 * slot, and encrypts with a key it can never read.
 *
 * What the Flipper firmware does with it (targets/f7/furi_hal/
 * furi_hal_crypto.c, 1.5.1): CBC with DATATYPE 00, raw little-endian words
 * for key, IV and data (so AES over byte-reversed words; the model doesn't
 * care), key derivation once per loaded key before the first decryption;
 * CTR and GCM with byte-swapped words, which is the standard byte order.
 *
 * Not modelled: CCM, DMA, the suspend registers' contents (they are
 * storage), computation time.
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/log.h"
#include "qemu/bitops.h"
#include "qemu/bswap.h"
#include "qemu/host-utils.h"
#include "crypto/aes.h"
#include "hw/core/irq.h"
#include "hw/core/qdev-properties.h"
#include "migration/vmstate.h"
#include "wb55.h"

#define AES_CR      0x00
#define AES_SR      0x04
#define AES_DINR    0x08
#define AES_DOUTR   0x0C
#define AES_KEYR0   0x10        /* ..KEYR3 0x1C */
#define AES_IVR0    0x20        /* ..IVR3  0x2C */
#define AES_KEYR4   0x30        /* ..KEYR7 0x3C */
#define AES_SUSP0R  0x40        /* ..SUSP7R 0x5C */

#define CR_EN        (1u << 0)
#define CR_DATATYPE  (3u << 1)
#define CR_MODE      (3u << 3)
#define CR_CHMOD     ((3u << 5) | (1u << 16))
#define CR_CCFC      (1u << 7)
#define CR_ERRC      (1u << 8)
#define CR_CCFIE     (1u << 9)
#define CR_ERRIE     (1u << 10)
#define CR_DMA       (3u << 11)
#define CR_GCMPH     (3u << 13)
#define CR_KEYSIZE   (1u << 18)
#define CR_NPBLB     (0xFu << 20)
#define CR_STORED    0x00F57E7Fu        /* everything but CCFC and ERRC */
/* what must not change while the peripheral is enabled */
#define CR_STATIC    (CR_DATATYPE | CR_MODE | CR_CHMOD | CR_KEYSIZE)

#define SR_CCF       (1u << 0)
#define SR_RDERR     (1u << 1)
#define SR_WRERR     (1u << 2)

enum { MODE_ENCRYPT = 0, MODE_KEYDERIV, MODE_DECRYPT, MODE_KEYDERIV_DECRYPT };
enum { CH_ECB = 0, CH_CBC, CH_CTR, CH_GCM, CH_CCM };
enum { PH_INIT = 0, PH_HEADER, PH_PAYLOAD, PH_FINAL };

static inline unsigned aes_mode(const WB55AesState *s)
{
    return (s->cr >> 3) & 3;
}

static inline unsigned aes_chmod(const WB55AesState *s)
{
    return ((s->cr >> 5) & 3) | ((s->cr >> 14) & 4);
}

static inline unsigned aes_phase(const WB55AesState *s)
{
    return (s->cr >> 13) & 3;
}

static void aes_update_irq(WB55AesState *s)
{
    bool level = ((s->sr & SR_CCF) && (s->cr & CR_CCFIE)) ||
                 ((s->sr & (SR_RDERR | SR_WRERR)) && (s->cr & CR_ERRIE));

    qemu_set_irq(s->irq, level);
}

/* ------------------------------------------------------------ the cipher */

/* DATATYPE: how a word on the bus maps to four bytes of the block */
static uint32_t aes_swap(const WB55AesState *s, uint32_t w)
{
    switch ((s->cr >> 1) & 3) {
    case 1:  return rol32(w, 16);
    case 2:  return bswap32(w);
    case 3:  return revbit32(w);
    default: return w;
    }
}

/* KEYR as the key's bytes; returns their number */
static int aes_key_get(const WB55AesState *s, uint8_t *k)
{
    int words = (s->cr & CR_KEYSIZE) ? 8 : 4;

    for (int i = 0; i < words; i++) {
        stl_be_p(k + 4 * i, s->key[words - 1 - i]);
    }
    return words * 4;
}

static void aes_key_put(WB55AesState *s, const uint8_t *k, int len)
{
    int words = len / 4;

    for (int i = 0; i < words; i++) {
        s->key[words - 1 - i] = ldl_be_p(k + 4 * i);
    }
}

static void aes_iv_get(const WB55AesState *s, uint8_t *b)
{
    for (int i = 0; i < 4; i++) {
        stl_be_p(b + 4 * i, s->iv[3 - i]);
    }
}

static void aes_iv_put(WB55AesState *s, const uint8_t *b)
{
    for (int i = 0; i < 4; i++) {
        s->iv[3 - i] = ldl_be_p(b + 4 * i);
    }
}

static uint32_t aes_subword(uint32_t w)
{
    return ((uint32_t)AES_sbox[w >> 24] << 24) |
           ((uint32_t)AES_sbox[(w >> 16) & 0xFF] << 16) |
           ((uint32_t)AES_sbox[(w >> 8) & 0xFF] << 8) |
           AES_sbox[w & 0xFF];
}

/*
 * The key schedule run backwards (FIPS 197 5.2): from its last Nk words,
 * which is what key derivation leaves in KEYR, to the key itself.
 */
static void aes_key_underive(const uint8_t *last, int len, uint8_t *key)
{
    static const uint8_t rcon[] = {
        0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36,
    };
    int nk = len / 4, total = 4 * (nk + 7);
    uint32_t w[60];

    for (int i = 0; i < nk; i++) {
        w[total - nk + i] = ldl_be_p(last + 4 * i);
    }
    for (int i = total - 1; i >= nk; i--) {
        uint32_t t = w[i - 1];

        if (i % nk == 0) {
            t = aes_subword(rol32(t, 8)) ^ ((uint32_t)rcon[i / nk - 1] << 24);
        } else if (nk == 8 && i % nk == 4) {
            t = aes_subword(t);
        }
        w[i - nk] = w[i] ^ t;
    }
    for (int i = 0; i < nk; i++) {
        stl_be_p(key + 4 * i, w[i]);
    }
}

/* MODE 01: KEYR becomes the end of its own schedule */
static void aes_key_derive(WB55AesState *s)
{
    uint8_t k[32], d[32];
    int len = aes_key_get(s, k), nk = len / 4;
    AES_KEY ks;

    AES_set_encrypt_key(k, len * 8, &ks);
    for (int i = 0; i < nk; i++) {
        stl_be_p(d + 4 * i, ks.rd_key[4 * (ks.rounds + 1) - nk + i]);
    }
    aes_key_put(s, d, len);
}

static void aes_encrypt_block(const WB55AesState *s, const uint8_t *in,
                              uint8_t *out)
{
    uint8_t k[32];
    int len = aes_key_get(s, k);
    AES_KEY ks;

    AES_set_encrypt_key(k, len * 8, &ks);
    AES_encrypt(in, out, &ks);
}

static void aes_decrypt_block(const WB55AesState *s, const uint8_t *in,
                              uint8_t *out)
{
    uint8_t k[32], plain[32];
    int len = aes_key_get(s, k);
    AES_KEY ks;

    if (aes_mode(s) == MODE_DECRYPT) {          /* KEYR: a derived key */
        aes_key_underive(k, len, plain);
        AES_set_decrypt_key(plain, len * 8, &ks);
    } else {
        AES_set_decrypt_key(k, len * 8, &ks);
    }
    AES_decrypt(in, out, &ks);
}

/* x = x * h in GF(2^128), bit 0 first (NIST SP 800-38D, algorithm 1) */
static void aes_ghash_mul(uint8_t *x, const uint8_t *h)
{
    uint8_t z[16] = { 0 }, v[16];

    memcpy(v, h, 16);
    for (int i = 0; i < 128; i++) {
        bool lsb = v[15] & 1;

        if (x[i / 8] & (0x80 >> (i % 8))) {
            for (int j = 0; j < 16; j++) {
                z[j] ^= v[j];
            }
        }
        for (int j = 15; j > 0; j--) {
            v[j] = (v[j] >> 1) | (v[j - 1] << 7);
        }
        v[0] >>= 1;
        if (lsb) {
            v[0] ^= 0xE1;
        }
    }
    memcpy(x, z, 16);
}

static void aes_ghash(WB55AesState *s, const uint8_t *block)
{
    for (int i = 0; i < 16; i++) {
        s->gcm_y[i] ^= block[i];
    }
    aes_ghash_mul(s->gcm_y, s->gcm_h);
}

/* E(K, IV), then the counter in IVR0 moves on */
static void aes_keystream(WB55AesState *s, uint8_t *ks)
{
    uint8_t ctr[16];

    aes_iv_get(s, ctr);
    aes_encrypt_block(s, ctr, ks);
    s->iv[0]++;
}

/* The fourth word of a block is in: compute. Returns whether there is
 * anything to read back. */
static bool aes_block(WB55AesState *s, const uint8_t *in, uint8_t *out)
{
    unsigned mode = aes_mode(s);
    bool decrypt = mode == MODE_DECRYPT || mode == MODE_KEYDERIV_DECRYPT;
    uint8_t iv[16], ks[16];

    switch (aes_chmod(s)) {
    case CH_ECB:
        if (decrypt) {
            aes_decrypt_block(s, in, out);
        } else {
            aes_encrypt_block(s, in, out);
        }
        return true;

    case CH_CBC:
        aes_iv_get(s, iv);
        if (decrypt) {
            aes_decrypt_block(s, in, out);
            for (int i = 0; i < 16; i++) {
                out[i] ^= iv[i];
            }
            aes_iv_put(s, in);
            return true;
        }
        if (s->answer && !memcmp(iv, s->answer->iv, 16) &&
            !memcmp(in, s->answer->in, 16)) {
            memcpy(out, s->answer->out, 16);    /* wb55_aes_enclave_load() */
        } else {
            uint8_t x[16];

            for (int i = 0; i < 16; i++) {
                x[i] = in[i] ^ iv[i];
            }
            aes_encrypt_block(s, x, out);
        }
        aes_iv_put(s, out);
        return true;

    case CH_CTR:
        aes_keystream(s, ks);
        for (int i = 0; i < 16; i++) {
            out[i] = in[i] ^ ks[i];
        }
        return true;

    case CH_GCM:
        switch (aes_phase(s)) {
        case PH_HEADER:
            aes_ghash(s, in);
            return false;
        case PH_PAYLOAD: {
            uint8_t c[16];

            aes_keystream(s, ks);
            for (int i = 0; i < 16; i++) {
                out[i] = in[i] ^ ks[i];
            }
            if (decrypt) {
                memcpy(c, in, 16);
            } else {
                /* the padding of a short last block came out as key
                 * stream; the tag is over zeros there */
                unsigned pad = (s->cr >> 20) & 0xF;

                memcpy(c, out, 16);
                memset(c + 16 - pad, 0, pad);
            }
            aes_ghash(s, c);
            return true;
        }
        case PH_FINAL: {
            uint8_t j0[16];

            aes_ghash(s, in);                   /* len(A) || len(C), bits */
            aes_iv_get(s, j0);
            stl_be_p(j0 + 12, 1);
            aes_encrypt_block(s, j0, ks);
            for (int i = 0; i < 16; i++) {
                out[i] = s->gcm_y[i] ^ ks[i];
            }
            return true;
        }
        }
        qemu_log_mask(LOG_GUEST_ERROR,
                      "%s: data written in the GCM init phase\n", s->name);
        return false;

    default:
        qemu_log_mask(LOG_UNIMP, "%s: chaining mode %u (CCM) not modelled\n",
                      s->name, aes_chmod(s));
        memset(out, 0, 16);
        return true;
    }
}

static void aes_din_write(WB55AesState *s, uint32_t val)
{
    uint8_t in[16], out[16];

    if (!(s->cr & CR_EN)) {
        qemu_log_mask(LOG_GUEST_ERROR, "%s: DINR written while disabled\n",
                      s->name);
        return;
    }
    if (s->nout) {
        /* a block is waiting to be read: flagged, and overwritten */
        s->sr |= SR_WRERR;
        s->nout = 0;
    }
    s->din[s->nin++] = val;
    if (s->nin < 4) {
        aes_update_irq(s);
        return;
    }
    s->nin = 0;
    for (int i = 0; i < 4; i++) {
        stl_be_p(in + 4 * i, aes_swap(s, s->din[i]));
    }
    if (aes_block(s, in, out)) {
        for (int i = 0; i < 4; i++) {
            s->dout[i] = aes_swap(s, ldl_be_p(out + 4 * i));
        }
        s->nout = 4;
    }
    s->sr |= SR_CCF;
    aes_update_irq(s);
}

static uint32_t aes_dout_read(WB55AesState *s)
{
    if (!s->nout) {
        if (s->cr & CR_EN) {
            s->sr |= SR_RDERR;
            aes_update_irq(s);
        }
        return 0;
    }
    return s->dout[4 - s->nout--];
}

static void aes_cr_write(WB55AesState *s, uint32_t val)
{
    uint32_t old = s->cr;

    if (val & CR_CCFC) {
        s->sr &= ~SR_CCF;
    }
    if (val & CR_ERRC) {
        s->sr &= ~(SR_RDERR | SR_WRERR);
    }
    val &= CR_STORED;
    if ((old & val & CR_EN) && ((old ^ val) & CR_STATIC)) {
        qemu_log_mask(LOG_GUEST_ERROR, "%s: CR 0x%08x -> 0x%08x while "
                      "enabled\n", s->name, old, val);
    }
    if ((val & CR_DMA) && !s->warned_dma) {
        s->warned_dma = true;
        qemu_log_mask(LOG_UNIMP, "%s: DMA requests not modelled\n", s->name);
    }
    s->cr = val;

    if (!(old & CR_EN) && (val & CR_EN)) {
        s->nin = s->nout = 0;
        if (aes_mode(s) == MODE_KEYDERIV) {
            aes_key_derive(s);
            s->sr |= SR_CCF;
            s->cr &= ~CR_EN;
        } else if (aes_chmod(s) == CH_GCM && aes_phase(s) == PH_INIT) {
            static const uint8_t zero[16];

            aes_encrypt_block(s, zero, s->gcm_h);
            memset(s->gcm_y, 0, 16);
            s->sr |= SR_CCF;
            s->cr &= ~CR_EN;
        }
    }
    aes_update_irq(s);
}

/* ------------------------------------------------------------- registers */

static uint32_t aes_reg_read(WB55AesState *s, hwaddr addr, bool peek)
{
    switch (addr) {
    case AES_CR:
        return s->cr;
    case AES_SR:
        return s->sr;
    case AES_DINR:
        return 0;
    case AES_DOUTR:
        return peek ? 0 : aes_dout_read(s);
    case AES_KEYR0 ... AES_KEYR0 + 12:
        return s->secure ? 0 : s->key[(addr - AES_KEYR0) / 4];
    case AES_KEYR4 ... AES_KEYR4 + 12:
        return s->secure ? 0 : s->key[4 + (addr - AES_KEYR4) / 4];
    case AES_IVR0 ... AES_IVR0 + 12:
        return s->iv[(addr - AES_IVR0) / 4];
    case AES_SUSP0R ... AES_SUSP0R + 28:
        return s->susp[(addr - AES_SUSP0R) / 4];
    }
    return 0;
}

static void aes_reg_write(WB55AesState *s, hwaddr addr, uint32_t val)
{
    switch (addr) {
    case AES_CR:
        aes_cr_write(s, val);
        return;
    case AES_SR:
        return;                         /* read-only */
    case AES_DINR:
        aes_din_write(s, val);
        return;
    case AES_DOUTR:
        return;
    case AES_KEYR0 ... AES_KEYR0 + 12:
    case AES_KEYR4 ... AES_KEYR4 + 12:
        if (s->secure) {
            qemu_log_mask(LOG_GUEST_ERROR, "%s: key register written in "
                          "secure mode (a key of CPU2's is loaded)\n", s->name);
        } else if (s->cr & CR_EN) {
            qemu_log_mask(LOG_GUEST_ERROR, "%s: key register written while "
                          "enabled\n", s->name);
        } else if (addr < AES_IVR0) {
            s->key[(addr - AES_KEYR0) / 4] = val;
        } else {
            s->key[4 + (addr - AES_KEYR4) / 4] = val;
        }
        return;
    case AES_IVR0 ... AES_IVR0 + 12:
        if (s->cr & CR_EN) {
            qemu_log_mask(LOG_GUEST_ERROR, "%s: IV register written while "
                          "enabled\n", s->name);
        } else {
            s->iv[(addr - AES_IVR0) / 4] = val;
        }
        return;
    case AES_SUSP0R ... AES_SUSP0R + 28:
        s->susp[(addr - AES_SUSP0R) / 4] = val;
        return;
    }
    qemu_log_mask(LOG_GUEST_ERROR, "%s: write 0x%" HWADDR_PRIx "\n", s->name,
                  addr);
}

static uint64_t wb55_aes_read(void *opaque, hwaddr addr, unsigned size)
{
    WB55AesState *s = opaque;
    uint32_t v = aes_reg_read(s, addr & ~3u, false);

    return extract32(v, (addr & 3) * 8, size * 8);
}

static void wb55_aes_write(void *opaque, hwaddr addr, uint64_t val64,
                           unsigned size)
{
    WB55AesState *s = opaque;
    uint32_t val = val64;

    if (size < 4) {                     /* a byte or half-word of a register */
        val = deposit32(aes_reg_read(s, addr & ~3u, true), (addr & 3) * 8,
                        size * 8, val);
    }
    aes_reg_write(s, addr & ~3u, val);
}

static const MemoryRegionOps wb55_aes_ops = {
    .read = wb55_aes_read,
    .write = wb55_aes_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .impl.min_access_size = 1,
    .impl.max_access_size = 4,
    /* LDRD/STRD arrive as one 8-byte access; split it like the bus does */
    .valid.min_access_size = 1,
    .valid.max_access_size = 8,
};

/* ------------------------------------------------------------ CPU2's side */

/*
 * CPU2 puts a key of its own into the key registers and closes them to
 * CPU1: 32 bytes into KEYR7..0, 16 into KEYR3..0. AES_CR stays CPU1's, so
 * KEYSIZE has to say the same. `answer`, if given, is one CBC encryption
 * this key is known to produce: that block is answered from it instead of
 * computed (see flipper_enclave.c for why).
 */
void wb55_aes_enclave_load(WB55AesState *s, const uint8_t *key, int len,
                           const WB55AesAnswer *answer)
{
    memset(s->key, 0, sizeof(s->key));
    aes_key_put(s, key, len);
    s->secure = true;
    s->answer = answer;
}

/* CPU2 wipes the key registers and gives them back. */
void wb55_aes_enclave_unload(WB55AesState *s)
{
    memset(s->key, 0, sizeof(s->key));
    s->secure = false;
    s->answer = NULL;
}

static void wb55_aes_reset_hold(Object *obj, ResetType type)
{
    WB55AesState *s = WB55_AES(obj);

    /*
     * Also what RCC's AESxRST does. Secure mode is not the peripheral's to
     * drop: SIPCR lives in SYSCFG and CPU2 owns it, so it is left to
     * wb55_aes_enclave_unload(), which CPU2's own reset calls.
     */
    s->cr = s->sr = 0;
    memset(s->key, 0, sizeof(s->key));
    memset(s->iv, 0, sizeof(s->iv));
    memset(s->susp, 0, sizeof(s->susp));
    memset(s->din, 0, sizeof(s->din));
    memset(s->dout, 0, sizeof(s->dout));
    memset(s->gcm_h, 0, sizeof(s->gcm_h));
    memset(s->gcm_y, 0, sizeof(s->gcm_y));
    s->nin = s->nout = 0;
    s->answer = NULL;
    qemu_set_irq(s->irq, 0);
}

static void wb55_aes_init(Object *obj)
{
    WB55AesState *s = WB55_AES(obj);

    memory_region_init_io(&s->mmio, obj, &wb55_aes_ops, s, "aes", 0x400);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq);
}

static const Property wb55_aes_props[] = {
    DEFINE_PROP_STRING("name", WB55AesState, name),
};

/*
 * A saved state.  A key of CPU2's is in it like any other (a state file
 * holds keys).  `answer` is a pointer: the state says whether there was one,
 * and CPU2's side puts it back (wb55_fus_loaded()).
 */
static int wb55_aes_pre_save(void *opaque)
{
    WB55AesState *s = opaque;

    s->has_answer = s->answer != NULL;
    return 0;
}

static const VMStateDescription vmstate_wb55_aes = {
    .name = "stm32wb55-aes",
    .version_id = 1,
    .minimum_version_id = 1,
    .pre_save = wb55_aes_pre_save,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32(cr, WB55AesState),
        VMSTATE_UINT32(sr, WB55AesState),
        VMSTATE_UINT32_ARRAY(key, WB55AesState, 8),
        VMSTATE_UINT32_ARRAY(iv, WB55AesState, 4),
        VMSTATE_UINT32_ARRAY(susp, WB55AesState, 8),
        VMSTATE_UINT32_ARRAY(din, WB55AesState, 4),
        VMSTATE_UINT32_ARRAY(dout, WB55AesState, 4),
        VMSTATE_UINT8(nin, WB55AesState),
        VMSTATE_UINT8(nout, WB55AesState),
        VMSTATE_UINT8_ARRAY(gcm_h, WB55AesState, 16),
        VMSTATE_UINT8_ARRAY(gcm_y, WB55AesState, 16),
        VMSTATE_BOOL(secure, WB55AesState),
        VMSTATE_BOOL(has_answer, WB55AesState),
        VMSTATE_END_OF_LIST()
    }
};

static void wb55_aes_class_init(ObjectClass *klass, const void *data)
{
    device_class_set_props(DEVICE_CLASS(klass), wb55_aes_props);
    RESETTABLE_CLASS(klass)->phases.hold = wb55_aes_reset_hold;
    DEVICE_CLASS(klass)->vmsd = &vmstate_wb55_aes;
}

static const TypeInfo wb55_aes_info = {
    .name = TYPE_WB55_AES,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(WB55AesState),
    .instance_init = wb55_aes_init,
    .class_init = wb55_aes_class_init,
};

static void wb55_aes_register(void)
{
    type_register_static(&wb55_aes_info);
}
type_init(wb55_aes_register)
