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

One overlay serves every firmware: the keys are the same everywhere. Its
`keeloq_mfcodes` holds 124 codes. The packages' own files have 63 lines
(official 1.5.1 and its dev build), 65 (Momentum mntm-012) and 125 (Unleashed
unlshd-094 and RogueMaster RM1024-1347). So the official firmware and Momentum
gain makers they do not ship, which is harmless, and Unleashed's and
RogueMaster's own file has one line more than the overlay. Only a real
Flipper's key opens those files, but an encrypted keystore shows how long
each of its lines is, in blocks of 16 characters. By that, the package's file
is the overlay's list with one line of 17 to 32 characters more, somewhere
after the overlay's lines 3 to 45. A later list of Unleashed's has two lines
that the overlay has not: `HomeGate` (simple learning) after line 22, which
fits, and `Phantom_Hiland` (normal learning) after line 26, which is a block
too long for this package's file. Unleashed's package has no `came_atomo`,
and its card gets the overlay's.

The numbers of the kinds of learning are not the same everywhere, and the
overlay has Unleashed's (RogueMaster is built on Unleashed). Types 0 to 4
mean the same in every firmware here. Momentum has Unleashed's numbers up to
8, its last one, and passes over a line of a type from 9 to 19. The official
firmware has 5, 6 and 7 for the three kinds of magic-serial learning, where
Unleashed and Momentum have 6, 7 and 8. So for the five codes of that kind
(KGB/Subaru, Magic_1 to Magic_4) it tries another kind of learning, or none:
of three made-up remotes for each of the five it names none of the 15, where
Momentum names 14 and Unleashed 12 (the others have the name of an earlier
line). A second line for the same key with the official firmware's number
mends it: with a local list that has such lines for four of the five, the
official firmware names those four makers' 12 remotes, and Momentum and
Unleashed name what they named before. The official firmware's Kingates
Stylo 4K decoder tries the lines of type 1, and the overlay's line for that
maker has type 10 (read in the source; no such remote was made up).

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

### Putting key lists together

```
scripts/rekey_keystore.py merge base.keys more.keys... out.keys [--all]
```

`merge` writes one decrypted list: the lines of the base list, first and as
they are, and then the codes of the other lists that it does not have. It
encrypts nothing (`keys` does that with its output), and what it prints is
names and types, never a key. The base list stays first because a firmware
gives a remote the name of the first line that fits it, and some decoders
take the first line of a type. Of the other lists, these lines are left out,
and each is printed with the line that stands for it:

- a code that the list has: the same key and the same type, whatever its
  name;
- a type above 19, which no firmware here has;
- a line of type 1 to 4 whose key a type-0 line of the base list has, as it
  is or with its bytes in the other order. For a type-0 line the firmware
  tries simple, normal, secure and magic-XOR learning, each with both orders;
- a type-0 line whose key a line that stays has, as it is or with its bytes
  in the other order.

The new type-0 lines go last. `--all` leaves out only the first kind.

The reason for a short list: a firmware tries the lines in their order, and
takes the first one whose decryption has the packet's button and the low byte
of its serial number (or a zero in that place). A packet of a remote that no
line is for passes that about once in 2048 tries, and then has that line's
name. A line of type 1 to 4 is one try and a type-0 line is eight. In
Unleashed the `JCM_Gen2` line (type 16) is 256, one for each value of a byte
that the packet does not carry, and a remote whose line comes after it loses
about one packet in nine to that name. What 300 made-up remotes with random
keys were called, with the overlay and with two lists that `merge` made of
its codes and a longer private list:

| `keeloq_mfcodes` | Official 1.5.1 | Momentum | Unleashed |
|---|---|---|---|
| the overlay, 124 lines | 23 named (7.7 %) | 22 (7.3 %) | 73 (24.3 %) |
| merged, 230 lines | 37 (12.3 %) | 36 (12.0 %) | 87 (29.0 %) |
| merged with `--all`, 298 lines | 88 (29.3 %) | not tried | 130 (43.3 %) |

The dev build counted as the official release and RogueMaster as Unleashed,
with the first two lists. A firmware loads each of these lists and saves it
again line for line. With the first two, each of the five names a made-up
remote for every line with simple or normal learning (72 and 147) within
three tries: the first packet of some has the name of an earlier line, for
the reason above. With the 298 lines it took the official firmware four tries
for 161, and Unleashed had one of them wrong after four.

With a later list of Unleashed's as the base, which has two lines more than
the overlay, the merged lists have 231 and 299 lines. The 126 and the 231
lines count as the 124 and the 230 do above, on all five firmwares, and
each firmware names a made-up remote for every line of theirs with simple
or normal learning (74 and 148).

## Checking it (`scripts/keystore_check.py`)

```
scripts/keystore_check.py                  # the tool and the tracked files
scripts/keystore_check.py FIRMWARE.tgz     # and a firmware that uses them
```

Without a firmware it checks the tool against the project's own keystore
encoder, and the tracked files: each is what re-keying its own contents
gives, under key 1 and no other key, and of `keystores/` git has the four
overlay files and nothing else. The tool's commands are tried with made-up
keys: what `keys`, `raw`, `verify` and `merge` write, what they refuse, and
that `merge` prints no key.

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

36 checks (23 of them without a firmware), which pass on the official
1.5.1, its dev build, Momentum,
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
