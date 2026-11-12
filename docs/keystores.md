# Sub-GHz keystores in the emulator

A real Flipper's Sub-GHz keystores (`subghz/assets/keeloq_mfcodes`,
`nice_flor_s`, `alutech_at_4n`, `came_atomo`) are AES-256-CBC encrypted under
**factory key 1**, a key that never leaves the hardware. An emulated Flipper
has its *own* public factory keys (see
[the secure enclave](../README.md#the-secure-enclave) and
`scripts/fe_enclave.py`), so the stock files decrypt to noise and Sub-GHz
loses its manufacturer codes.

The fix needs no firmware patch and no hardware key: **re-encrypt the keystore
contents under the emulator's own key 1**. The unmodified firmware then loads
the file in the emulator exactly as it loads the stock file on hardware. The
re-keyed superset ships here under `keystores/overlay/`, so a clone works out
of the box.

## Using it

`flipperemu`'s `make_sd` automatically applies `keystores/overlay/` over the
stock resources whenever it builds a card (a device's first `run -f`, and
`sd` for a new card):

```
./flipperemu -d dev run -f firmware/unleashed-unlshd-094/...tgz
```

boots an unmodified firmware whose stock Sub-GHz codes decrypt. By hand:

```
scripts/make_sdcard.py sd.img --resources <stock.tgz> --overlay keystores/overlay
```

The launcher has no switch for a card without them; `scripts/make_sdcard.py`
without `--overlay` builds one with the package's own files.

One overlay serves every firmware: the manufacturer codes are universal. Its
`keeloq_mfcodes` holds 124 codes. The packages' own files have 63 lines
(official 1.5.1 and its dev build), 65 (Momentum mntm-012) and 125 (Unleashed
unlshd-094 and RogueMaster RM1024-1347). So the official firmware and Momentum
gain makers they do not ship, which is harmless, and Unleashed's and
RogueMaster's own file has one line more than the overlay. Which code that is
cannot be told here: only a real Flipper's key opens those files. Unleashed's
package has no `came_atomo`, and its card gets the overlay's.

### A card that has the package's own keystores

Two kinds of card have them: one that was built before the overlay existed
(a card is built once and kept), and one whose firmware was updated. A
firmware's updater unpacks the resources of its package to the card, the
stock keystores among them, and the firmware names no KeeLoq remote's maker
after that.

`update` and `run --update` write the emulator's keystores to the card again
when the updater is done, and say so. For the rest (an older card,
`update --no-wait`, an update that was started on the Flipper itself):

```
./flipperemu -d dev push keystores/overlay/subghz /ext/
```

It works for a device that runs (through the firmware) and for one that is
stopped (in the card's image), and it sends only the files that differ.
Unleashed's decoder keeps the Nice Flor-S table in memory once it has read it
(and RogueMaster is built on Unleashed): there a changed `nice_flor_s` counts
from the firmware's next start.

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
the enclave test. The IV is fixed, so re-keying is reproducible and the
tracked files don't churn.

These codes change rarely. To cover a fork exactly, decrypt its stock keystore
once on a real device used as an enclave oracle, drop the `KEY:TYPE:NAME` list
in `keystores/plaintext/`, and re-key.

## Checking it (`scripts/keystore_check.py`)

```
scripts/keystore_check.py                  # the tool and the tracked files
scripts/keystore_check.py FIRMWARE.tgz     # and a firmware that uses them
```

Without a firmware it checks the tool against the project's own keystore
encoder, and the tracked files: each is what re-keying its own contents
gives, under key 1 and no other key, and of `keystores/` git has the four
overlay files and nothing else.

With a firmware it boots a throwaway device, and the unmodified firmware does
the rest:

- its own `subghz encrypt_keeloq` loads `keeloq_mfcodes` and saves it again:
  124 of 124 lines, and with the same IV the tracked file;
- its own `subghz encrypt_raw` makes each of the three RAW keystores from the
  decrypted bytes: the tracked file, byte for byte;
- a made-up KeeLoq remote for each of the 72 codes with simple or normal
  learning (41 and 31; the other 52 codes are not tried) is named after its
  code by `subghz decode_raw`;
- 8 made-up Nice Flor-S remotes are read with their serial number, counter
  and button, which needs the `nice_flor_s` table;
- with the package's own `keeloq_mfcodes` and `nice_flor_s` in their place,
  and the firmware started anew, none of the 72 is named and none of the 8 is
  read as it was made.

32 checks, which pass on the official 1.5.1, its dev build, Momentum,
Unleashed and RogueMaster. Alutech AT-4N and CAME Atomo are not tried with a
remote: their keystores are checked as files, and made anew by the firmware's
`encrypt_raw`.

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
