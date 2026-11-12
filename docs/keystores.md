# Sub-GHz keystores in the emulator

A real Flipper's Sub-GHz keystores (`subghz/assets/keeloq_mfcodes`,
`nice_flor_s`, `alutech_at_4n`, `came_atomo`) are AES-256-CBC encrypted under
**factory key 1**, a key that never leaves the hardware. An emulated Flipper
has its *own* public factory keys (see [the secure enclave](../README.md) and
`scripts/fe_enclave.py`), so the stock files decrypt to noise and Sub-GHz
loses its manufacturer codes.

The fix needs no firmware patch and no hardware key: **re-encrypt the keystore
contents under the emulator's own key 1**. The unmodified firmware then loads
the file in the emulator exactly as it loads the stock file on hardware.

## Using it

`flipperemu`'s `make_sd` automatically applies `keystores/overlay/` over the
stock resources whenever it builds a card — it is opt-in, happening only when
that folder exists. With the overlay in place:

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

## Making the overlay (`scripts/rekey_keystore.py`)

```
scripts/rekey_keystore.py keys   keys.txt        keystores/overlay/subghz/assets/keeloq_mfcodes
scripts/rekey_keystore.py raw    nice_flor_s.hex keystores/overlay/subghz/assets/nice_flor_s
scripts/rekey_keystore.py verify keystores/overlay/subghz/assets/keeloq_mfcodes
```

`keys.txt` is a decrypted keystore — one `KEY:TYPE:NAME` line each, exactly the
firmware's `%08lX%08lX:%hu:%s`. A RAW input is the decrypted blob as hex. The
tool re-encrypts under the emulator's key 1, in the firmware's on-disk format
(continuous AES-256-CBC, the `IV:` scrambled the same way — see
`lib/subghz/subghz_keystore.c`). It self-checks every write and produces
output byte-for-byte identical to the project's own keystore encoder
(`scripts/enclave_check.py: keystore_lines`), which the firmware round-trips in
the enclave test. A re-keyed `keeloq_mfcodes` has been loaded through the
unmodified firmware in the emulator (all 124 codes decoded back).

## Where the keys come from, and why they aren't here

`rekey_keystore.py` needs the *decrypted* keystores as input. Those come from a
real device used as an enclave oracle (it decrypts under the hardware key it
holds but cannot export); this repository does not include them. **`keystores/`
is git-ignored on purpose:** a file re-keyed to the emulator's public key is
trivially decryptable, so committing one would publish the manufacturer codes,
which the firmware source explicitly asks you not to share. Generate the
overlay locally and keep it local.
