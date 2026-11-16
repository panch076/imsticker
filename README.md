# imsticker

Put any PNG, GIF or APNG into the native iMessage sticker library exactly as it
is: no subject lift, no cut-out, transparency and animation kept.

The short version of the reverse engineering: **the subject separation is only a
step in Apple's "make a sticker" screen. It is not part of the sticker format.**
A sticker is a few HEIC images (or one HEIF image sequence, for animated ones)
sitting in a database that the `stickersd` daemon owns. Write your own images
into that database and they are stickers. That database is only reachable on a
Mac, so this needs a Mac signed in to the same Apple account.

## Use it

On a Mac (macOS 14 Sonoma or later) with Apple's command line tools
(`xcode-select --install` gives you both `python3` and `swift`):

```bash
python3 imsticker.py add --dry-run cat.png
```

```bash
python3 imsticker.py add cat.png party.gif wave.png
```

The dry run encodes the image and checks the database layout but changes
nothing; do it once first. After a real import, open the sticker drawer in
Messages. If macOS refuses access to the database, give your terminal app Full
Disk Access in System Settings > Privacy & Security.

| Command | What it does |
| --- | --- |
| `add FILE...` | Import images. An animated GIF or APNG becomes an animated sticker with its own frame timing. |
| `list -v` | Show every sticker in the library and each image stored for it. |
| `export DIR` | Write the stored images out as files, to look at how Apple's own stickers are built. |
| `restore` | Put back the backup taken before the last import. |

Useful `add` options: `--name "Troll"` (search text), `--max-edge 512`,
`--resample nearest` (crisp pixel art), `--no-upscale`, `--max-bytes 0` (do not
auto-shrink). A backup of the library is made before every import, in
`~/Library/Application Support/imsticker/Backups`.

## What is verified and what is not

| | Status |
| --- | --- |
| The format and the technique | Read directly from the source of [StickerHack](https://github.com/AdamEXu/StickerHack), which does this on macOS 15.7.6. A second person confirms it on 15.1.1 in [its issue #2](https://github.com/AdamEXu/StickerHack/issues/2). |
| This tool's database code | Tested on Windows against stand-in databases with both known layouts: 35 tests, including a second process holding the database open, backup and restore. |
| This tool's Swift encoder | **Never compiled or run.** There was no Mac. It uses the same ImageIO calls as StickerHack's working encoder, but expect to fix a typo. |
| A real `stickersd`, iCloud sync to iPhone, macOS 14 and 26+ | Not tested by me. StickerHack's README says imported stickers sync to iPhone and iPad. |

If you just want it to work today, StickerHack is the proven tool:
`npx stickerhack ./image.png`. imsticker exists because of four gaps in it:

- **APNG timing.** StickerHack reads frame delays only from GIF, so an APNG plays at a fixed 10 fps. imsticker reads the APNG delays.
- **Database location.** It moved between macOS versions; StickerHack only looks in one place. imsticker checks both.
- **Database columns.** They differ between versions and StickerHack's fixed insert fails on 15.1. imsticker reads the layout first and stops, changing nothing, if it sees something it does not understand.
- **Heavy stickers.** StickerHack's author found that ones over about 1 MB show up but do not send. imsticker shrinks until they fit, imports many files in one go, and can undo.

## How iMessage stickers work

### Where they live

| System | Path |
| --- | --- |
| macOS 15.7 | `~/Library/Group Containers/com.apple.stickersd.group/Stickers/stickers.stickerdb` |
| macOS 15.1 | `~/Library/Stickers/stickers.stickerdb` |
| iOS 18 | `/private/var/mobile/Library/Stickers/stickers.stickerdb` |

It is a Core Data SQLite store owned by `stickersd`. On iPhone that path is
outside anything an app or a non-jailbroken user can write to.

### What a sticker is

One row in `ZMANAGEDSTICKER` plus one row per stored image in
`ZMANAGEDREPRESENTATION` (the image bytes are in its `ZDATA` column).

A user sticker row has `ZTYPE = 1`, `ZEFFECT = 0`, a random UUID in
`ZIDENTIFIER` (16 bytes), `ZEXTERNALURI = sticker:///user/identifier/<UUID>`,
and `ZLIBRARYINDEX` set to the highest existing value plus 1024, which is its
place in the drawer. Dates are seconds since 2001-01-01. Newer systems add
`ZSANITIZEDPROMPT` and `ZPROMPTPRIMARYLANGUAGEIDENTIFIER` for Genmoji. Primary
keys are tracked in `Z_PRIMARYKEY` under the entity names `ManagedSticker` and
`ManagedRepresentation`.

Each image row has a role, a type, a size and an index:

| `ZROLE` | `ZUTI` | Longest side | `ZINDEX` |
| --- | --- | --- | --- |
| `com.apple.stickers.role.still` (preferred) | `public.heic` | 768 | 0 |
| `com.apple.stickers.role.keyboard` | `public.heic` | 100 | 1 |
| `com.apple.stickers.role.stillVariant_320` | `public.heic` | 768 | 2 |
| `com.apple.stickers.role.stillVariant_160` / `_96` / `_64` / `_40` | `public.heic` | 160 / 96 / 64 / 40 | 3 to 6 |
| `com.apple.stickers.role.animated` (preferred) | `public.heics` | 768 | 2 |

Stills are HEIC with an alpha channel. An animated sticker is a single HEIF
image sequence (file brand `msf1`) with per-frame delays and loop count 0.
These are the sizes StickerHack writes; `list -v` on a sticker you made the
normal way shows what Apple itself stores.

### How one is sent

On the wire a sticker is just that image as an attachment plus placement data.
The receiver draws whatever image arrives, which is why nothing downstream
cares whether a subject was ever lifted.

- The message's app identifier ends in `com.apple.Stickers.UserGenerated.MessagesExtension` for library stickers (`com.apple.messages.genmoji` for Genmoji).
- Associated message type `1000` is a sticker stuck onto a message; `2007` and `3007` add and remove a sticker tapback.
- Placement travels as attributes on the attachment: `spw` (bubble width), `sro` (rotation in radians), `ssa` (scale), `sxs` and `sys` (position), `sid` (sticker id), `shash`, `spv`, `stickerEffectType`.
- Outline, comic, puffy and shiny effects are a tag inside the HEIC: `stickerEffect:type="stroke"`, `"comic"`, `"puffy"` or `"iridescent"`.

## No Mac?

I found no way to write to the native library from an iPhone alone. The
database is out of reach without a jailbreak, and none of the guides I read
describe an import that skips the cut-out. Two things that do work on a phone:

- A sticker app that imports images as they are. Apple's sticker API takes PNG, APNG and GIF with transparency, but the stickers live in that app's tab of the drawer, not in your own library.
- Having someone with a Mac import the image and send it to you. Guides for iOS 18 describe saving a received sticker with touch and hold > Sticker Details (some call it Emoji Details) > the download button. I have not tested that with an injected sticker.

## Sources

- [StickerHack](https://github.com/AdamEXu/StickerHack) and [its issue #2](https://github.com/AdamEXu/StickerHack/issues/2): database paths, tables, roles, image formats. The technique is theirs; the code here is written from scratch.
- [imessage-exporter, `sticker.rs`](https://github.com/ReagentX/imessage-exporter/blob/develop/imessage-database/src/message_types/sticker.rs): app identifiers and the effect tag.
- [rustpush, `messages.rs`](https://github.com/OpenBubbles/rustpush/blob/master/src/imessage/messages.rs): message types and placement attributes.
- [Apple Developer Forums thread 764980](https://developer.apple.com/forums/thread/764980): `stickersd` logs showing the iOS path and that it is Core Data.
- [Apple ImageIO: APNG delay time](https://developer.apple.com/documentation/imageio/kcgimagepropertyapngdelaytime)
- [iPhone Life: saving a received sticker](https://iphonelife.com/content/how-to-get-same-stickers-your-friend-just-sent-you-messages)

## Tests

```bash
python3 -m unittest discover -s tests
```
