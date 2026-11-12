# Sub-GHz keystores in the emulator

A real Flipper's Sub-GHz keystores (`subghz/assets/keeloq_mfcodes`,
`nice_flor_s`, `alutech_at_4n`, `came_atomo`) are AES-256-CBC encrypted under
**factory key 1**, a key that never leaves the hardware. An emulated Flipper
has its *own* public factory keys (see [the secure enclave](../README.md) and
`scripts/fe_enclave.py`), so the stock files decrypt to noise and Sub-GHz
loses its manufacturer codes.

The fix needs no firmware patch and no hardware key: **re-encrypt the keystore
contents under the emulator's own key 1**. The unmodified firmware then loads
the file in the emulator exactly as it loads the stock file on hardware. The
re-keyed superset ships here under `keystores/overlay/`, so a clone works out
of the box.

## Using it

`flipperemu`'s `make_sd` automatically applies `keystores/overlay/` over the
stock resources whenever it builds a card:

```
./flipperemu -d dev run -f firmware/unleashed-unlshd-094/...tgz
```

boots an unmodified firmware whose stock Sub-GHz codes decrypt. By hand:

```
scripts/make_sdcard.py sd.img --resources <stock.tgz> --overlay keystores/overlay
```

One overlay serves every firmware: the manufacturer codes are universal, so a
single superset keystore makes official, Momentum and Unleashed all work (a
fork that ships fewer codes simply gains the extra ones, which is harmless).

## Regenerating / extending (`scripts/rekey_keystore.py`)

```
scripts/rekey_keystore.py keys   keys.txt        keystores/overlay/subghz/assets/keeloq_mfcodes
scripts/rekey_keystore.py raw    nice_flor_s.hex keystores/overlay/subghz/assets/nice_flor_s
scripts/rekey_keystore.py verify keystores/overlay/subghz/assets/keeloq_mfcodes
```

`keys.txt` is a decrypted keystore — one `KEY:TYPE:NAME` line each, exactly the
firmware's `%08lX%08lX:%hu:%s`. A RAW input is the decrypted blob as hex. The
tool re-encrypts under the emulator's key 1, in the firmware's on-disk format
(continuous AES-256-CBC, the `IV:` scrambled the same way — see
`lib/subghz/subghz_keystore.c`). It self-checks every write and produces output
byte-for-byte identical to the project's own keystore encoder
(`scripts/enclave_check.py: keystore_lines`), which the firmware round-trips in
the enclave test. A re-keyed `keeloq_mfcodes` has been loaded through the
unmodified firmware in the emulator (all 124 codes decoded back). The IV is
fixed, so re-keying is reproducible and the tracked files don't churn.

These codes change rarely. To cover a fork exactly, decrypt its stock keystore
once on a real device used as an enclave oracle, drop the `KEY:TYPE:NAME` list
in `keystores/plaintext/`, and re-key.

## What's tracked, and what isn't

`keystores/overlay/` (the re-keyed keystores) **is** committed, the same way a
firmware ships its keystores as encrypted assets — so the emulator is
self-contained. The difference from real firmware: *its* key is secret
hardware, while the emulator's key 1 is public by construction
(`SHA-256("FlipperEmu enclave key 1")`), so these files decode with
`rekey_keystore.py verify`. They are the stock manufacturer codes, which
already circulate publicly; this just carries them in the emulator's own key.

`keystores/plaintext/` — the decrypted key lists that *produce* the overlay —
is **git-ignored**: there's no reason to carry a raw, greppable key list in the
tree when the re-keyed asset already covers it.
