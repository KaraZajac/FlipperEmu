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
re-keyed keystores ship here under `keystores/overlay/`, so a clone works out
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

One overlay serves every firmware: the keys are the same everywhere. It has
two KeeLoq keystores.

**`keeloq_mfcodes`** is the list that a firmware reads: the 126 lines (123
keys) of a list of Unleashed's, as they are. The packages' own files have 63
lines (official 1.5.1 and its dev build), 65 (Momentum mntm-012) and 125
(Unleashed unlshd-094 and RogueMaster RM1024-1347), so the official firmware
and Momentum get makers that they do not ship. A list of each firmware's own
would be its package's, and of those only Unleashed's is here in a form that
can be re-keyed.

**`keeloq_mfcodes_kat`** beside it has 105 lines more: the codes of a longer
collected list that the first list has not (how they were chosen:
[Putting key lists together](#putting-key-lists-together)). They are 91
keys, 12 of them keys of the first list with another type. No firmware reads
this file by itself. It is for a program that asks for it (the name is after
KAT, the Keyfob Analysis Toolkit), and for whoever wants a firmware to read
more codes: [The second list](#the-second-list-keeloq_mfcodes_kat).

Only a real Flipper's key opens the packages' files, but an encrypted
keystore shows how long each of its lines is, in blocks of 16 characters. By
that, the file of Unleashed's package is the 126 lines of `keeloq_mfcodes`
without `Phantom_Hiland` (line 28, normal learning). A line as long with
another name would look the same. Unleashed's package has no `came_atomo`,
and its card gets the overlay's.

The numbers of the kinds of learning are not the same everywhere, and
`keeloq_mfcodes` has Unleashed's (RogueMaster is built on Unleashed). Types
0 to 4 mean the same in every firmware here. Momentum has Unleashed's
numbers up to 8, its last one, and passes over a line of a type from 9 to
19. The official firmware has 5, 6 and 7 for the three kinds of
magic-serial learning, where Unleashed and Momentum have 6, 7 and 8. So with
Unleashed's lines for the five codes of that kind (KGB/Subaru, Magic_1 to
Magic_4) it tries another kind of learning, or none: of three made-up remotes
for each of the five it names none of the 15. Second lines for the same keys
with the official firmware's numbers are in `keeloq_mfcodes_kat`, for four of
the five (there is none for Magic_1), and so is a line of type 1 for Kingates
Stylo 4K: the official firmware's decoder of that name tries the lines of
type 1, and Unleashed's line has type 10 (read in the source; no such remote
was made up).

### The second list: `keeloq_mfcodes_kat`

A firmware loads two KeeLoq lists by itself: `keeloq_mfcodes`, and then
`keeloq_mfcodes_user` if there is one. The official source has these two
names (`lib/subghz/types.h`), and the binaries of Momentum and Unleashed
have these two paths and no third. A file of another name in the folder is
as good as not there: with `keeloq_mfcodes_kat` beside the first list, each
of the five firmwares names none of 74 made-up remotes of its codes. So a
firmware works with the first list alone, as if the second file were not on
the card. No package has a file of that name, and an updater leaves it where
it is.

The file is a keystore like the first one: the same format, under the
emulator's key 1. A program reads it with the firmware's own loader, after
the first list:

```c
subghz_environment_load_keystore(environment, "/ext/subghz/assets/keeloq_mfcodes");
subghz_environment_load_keystore(environment, "/ext/subghz/assets/keeloq_mfcodes_kat");
```

The call is in the SDK's API, and a decoder tries the lines in the order in
which they were loaded (read in the official source; no program in this
repository does it yet). What was tried is the loader with this file: the
firmware's `subghz encrypt_keeloq` loads it and saves it again, 105 of 105
lines.

To have a firmware's own Sub-GHz app read these codes too, give it the file
as its second list:

```
./flipperemu -d dev push keystores/overlay/subghz/assets/keeloq_mfcodes_kat /ext/subghz/assets/keeloq_mfcodes_user
```

With that, each of the five firmwares names the 74 remotes. A new card has
no `keeloq_mfcodes_user`; a list of one's own of that name would be replaced
by this. The Sub-GHz app reads its lists when it starts (in the official
source), and `subghz decode_raw` at the CLI with each call. What the longer
list costs is measured [below](#putting-key-lists-together). To take it away
again:

```
./flipperemu -d dev rm /ext/subghz/assets/keeloq_mfcodes_user
```

With both lists read, the official firmware names the made-up magic-serial
remotes of the four makers that have a second line (12 of 15), Momentum 14 of
the 15 and Unleashed 12; the others have the name of an earlier line. (That
was measured with the 231 lines in one file, which a firmware reads in the
same order.)

`keeloq_mfcodes_kat` has type numbers of other tools too. A firmware that
reads it tries such a line with the kind of learning that the number means
to it, or passes over it: 13 lines of types 9 to 14 are so on Unleashed, and
four lines have a magic-serial key as normal learning, which no firmware here
reads so.

### A card that has the package's own keystores

Two kinds of card have them: one that was built before the overlay existed
(a card is built once and kept), and one whose firmware was updated. A
firmware's updater unpacks the resources of its package to the card, the
stock keystores among them, and the firmware names no KeeLoq remote's maker
after that. A card that was built before the overlay had two KeeLoq
keystores has an earlier `keeloq_mfcodes` (124 lines, or the 231 of both
lists in one file) and no `keeloq_mfcodes_kat`. An `update` and the command
below bring it the present files.

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

The two tracked KeeLoq keystores are what three commands make of two
decrypted lists, Unleashed's 126 lines and the longer one:

```
scripts/rekey_keystore.py merge unleashed.keys longer.keys more.keys --new-only
scripts/rekey_keystore.py keys  unleashed.keys keystores/overlay/subghz/assets/keeloq_mfcodes
scripts/rekey_keystore.py keys  more.keys      keystores/overlay/subghz/assets/keeloq_mfcodes_kat
```

### Putting key lists together

```
scripts/rekey_keystore.py merge base.keys more.keys... out.keys [--all] [--new-only]
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

The new type-0 lines go last. `--all` leaves out only the first kind. With
`--new-only` the output has the new codes alone, without the base list's
lines: a list for a keystore of its own beside the base list's, as
`keeloq_mfcodes_kat` is beside `keeloq_mfcodes`.

The reason for a short list: a firmware tries the lines in their order, and
takes the first one whose decryption has the packet's button and the low byte
of its serial number (or a zero in that place). A packet of a remote that no
line is for passes that about once in 2048 tries, and then has that line's
name. A line of type 1 to 4 is one try and a type-0 line is eight. In
Unleashed the `JCM_Gen2` line (type 16) is 256, one for each value of a byte
that the packet does not carry, and a remote whose line comes after it loses
about one packet in nine to that name. What 300 made-up remotes with random
keys were called, with `keeloq_mfcodes` alone, with the codes of
`keeloq_mfcodes_kat` after it, and with nearly every line of the two
decrypted lists (`merge --all`):

| the lines that the firmware reads | Official 1.5.1 | Momentum | Unleashed |
|---|---|---|---|
| `keeloq_mfcodes` alone, 126 lines | 23 named (7.7 %) | 22 (7.3 %) | 73 (24.3 %) |
| ... and `keeloq_mfcodes_kat` after it, 231 lines | 37 (12.3 %) | 36 (12.0 %) | 87 (29.0 %) |
| merged with `--all`, 298 lines | 88 (29.3 %) | not tried | 130 (43.3 %) |

Each row was measured with its lines in one file. The dev build counted as
the official release and RogueMaster as Unleashed, with the first two rows.
A firmware loads each of these lists and saves it again line for line. Each
of the five names a made-up remote for every line of the two keystores with
simple or normal learning (74 and 74), given up to five packets of each: a
packet can have the name of an earlier line, for the reason above. Two
packets were enough everywhere but for one remote of the second list on
Unleashed and RogueMaster, whose first three had the name of the `JCM_Gen2`
line. With the 298 lines it took the official firmware four packets for 161
remotes, and Unleashed had one of them wrong after four. (The 298 lines were
made when Unleashed's list had 124 lines. With its 126, `--all` gives 299,
which were not tried.)

So `keeloq_mfcodes` alone gives about one unknown KeeLoq remote in thirteen
a maker's name on the official firmware and Momentum, and one in four on
Unleashed, whose own package has all but one of these lines. With the second
list read too it is one in eight, and nearly three in ten on Unleashed. For
a shorter list: `verify` prints a keystore's lines, `keys` makes a keystore
of the ones that are wanted, and `push` brings it to a device's card.

## Checking it (`scripts/keystore_check.py`)

```
scripts/keystore_check.py                  # the tool and the tracked files
scripts/keystore_check.py FIRMWARE.tgz     # and a firmware that uses them
```

Without a firmware it checks the tool against the project's own keystore
encoder, and the tracked files: each is what re-keying its own contents
gives, under key 1 and no other key, the second KeeLoq keystore has no code
of the first, and of `keystores/` git has the five overlay files and nothing
else. The tool's commands are tried with made-up keys: what `keys`, `raw`,
`verify` and `merge` write, what they refuse, and that `merge` prints no key.

With a firmware it boots a throwaway device, and the unmodified firmware does
the rest:

- its own `subghz encrypt_keeloq` loads each of the two KeeLoq keystores and
  saves it again: 126 of 126 and 105 of 105 lines, and with the same IV the
  tracked file;
- its own `subghz encrypt_raw` makes each of the three RAW keystores from the
  decrypted bytes: the tracked file, byte for byte;
- a made-up KeeLoq remote for each of the 74 lines of `keeloq_mfcodes` with
  simple or normal learning (42 and 32; the other 52 lines are not tried) is
  named after its line by `subghz decode_raw`;
- of 74 such remotes for the lines of `keeloq_mfcodes_kat` (19 and 55; the
  other 31 lines are not tried) none is named while the file is beside the
  first list, and every one is when the same file is `keeloq_mfcodes_user`;
- 8 made-up Nice Flor-S remotes are read with their serial number, counter
  and button, which needs the `nice_flor_s` table;
- with the package's own `keeloq_mfcodes` and `nice_flor_s` in their place,
  and the firmware started anew, none of the first 74 is named and none of
  the 8 is read as it was made.

45 checks (28 of them without a firmware), which pass on the official
1.5.1, its dev build, Momentum, Unleashed and RogueMaster. Alutech AT-4N and
CAME Atomo are not tried with a remote: their keystores are checked as
files, and made anew by the firmware's `encrypt_raw`.

## What's tracked, and what isn't

`keystores/overlay/` (the re-keyed keystores) **is** committed, the same way a
firmware ships its keystores as encrypted assets — so the emulator is
self-contained. The difference from real firmware: *its* key is secret
hardware, while the emulator's key 1 is public by construction
(`SHA-256("FlipperEmu enclave key 1")`), so these files decode with
`rekey_keystore.py verify`. The three RAW keystores are the stock ones, and
`keeloq_mfcodes` is a list of Unleashed's: stock manufacturer codes, which
already circulate publicly. The 105 lines of `keeloq_mfcodes_kat` come from
a longer collected list. The repository carries all of them in the
emulator's own key, and no decrypted list.

`keystores/plaintext/` — the decrypted key lists that *produce* the overlay —
is **git-ignored**: there's no reason to carry a raw, greppable key list in the
tree when the re-keyed asset already covers it.
