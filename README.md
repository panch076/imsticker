# imsticker

Put any PNG, GIF or APNG into the native iMessage sticker library exactly as it
is: no subject lift, no cut-out, transparency and animation kept.

The short version of the reverse engineering: **the subject separation is only a
step in Apple's "make a sticker" screen. It is not part of how stickers are
stored or sent.** A sticker is a few images in a database that the `stickersd`
daemon owns. Apple's own code for adding a sticker stores whatever image bytes
it is handed. Put your own images in that database and they are stickers.

The database can only be reached on a Mac, so this needs a Mac signed in to the
same Apple account as the phone.

## Use it

On a Mac with Apple's command line tools (`xcode-select --install` gives you
both `python3` and `swift`):

```bash
python3 imsticker.py add --dry-run cat.png
```

```bash
python3 imsticker.py add cat.png party.gif wave.png
```

The dry run encodes the images and checks that your library can be opened, but
changes nothing; do it once first. After a real import, open the sticker drawer
in Messages. If macOS refuses access to the library, give your terminal app Full
Disk Access in System Settings > Privacy & Security.

| Command | What it does |
| --- | --- |
| `add FILE...` | Import images. An animated GIF or APNG becomes an animated sticker with its own frame timing. |
| `list -v` | Show every sticker in the library and each image stored for it. |
| `export DIR` | Write the stored images out as files. |
| `restore` | Put back the backup taken before the last import. The library as it was just before the restore is kept too. |

Options for `add`:

| Option | Effect |
| --- | --- |
| `--name "Troll"` | Search text for the sticker (one file at a time). |
| `--max-edge 512` | Longest side in pixels. Default 768. |
| `--max-bytes N` | Shrink the image until the encoded sticker is under N bytes, and say so if that cannot be done. Default 1000000; 0 turns it off. |
| `--no-upscale` | Never enlarge a small image. |
| `--resample nearest` | Keep pixel art crisp. |
| `--animation-only` | For an animated image, store only the animation, without the still and keyboard preview. |
| `--raw-sql` | Write the database rows directly instead of saving through Core Data. See "Two ways in". |
| `--no-restart` | Do not restart `stickersd` afterwards. |

A backup of the library is made before every import, in
`~/Library/Application Support/imsticker/Backups`.

Animated stickers need a Mac that can write an HEIF image sequence. Real Macs
can. Virtual Apple-silicon Macs cannot, with this tool or any other.

## What is verified, and what is not

Everything in the first table is checked by [the workflow in this
repository](.github/workflows/verify.yml) on GitHub's Macs: macOS 15.8 on Apple
silicon, macOS 15.7 on Intel, and macOS 26.7 on Apple silicon. Each of those
Macs has a real `stickersd` and a real sticker library.

| Claim | How it was checked |
| --- | --- |
| The Swift helper builds | Compiled with Swift 6.1 and 6.3. |
| The Python runs on what Apple ships | The unit tests pass on `/usr/bin/python3` (3.9.6). |
| A still is stored as the picture you gave it | Every stored image is decoded with ImageIO and compared with the source: size, colour, a fully transparent corner, a half-transparent stripe. |
| An animation keeps its frames, order, timing, transparency and looping | The same, per frame, for a GIF and an APNG with uneven delays. Intel only: GitHub's Apple-silicon Macs are virtual and cannot write the format. |
| A save through Core Data opens the real library and enters its history | Run against the libraries `stickersd` created on all three Macs. The job fails unless every saved object appears in the history tables. |
| The system's own code accepts what the tool writes | Apple's sticker store code is run in-process on a copy of the library and asked for the stickers. It returns all of them, with the still, keyboard and animated images. |
| The library survives | `stickersd` is restarted on top of the imported stickers. They remain, the database passes its integrity check, and the daemon logs no sticker errors. |

| Not verified | Why |
| --- | --- |
| That Messages shows and sends them | No way to drive the Messages window on a build machine. [StickerHack](https://github.com/AdamEXu/StickerHack) users report that stickers written this way appear and send. |
| That they reach your iPhone | The build machines have no iCloud account, so `stickersd` never gets as far as uploading. The default route is built so that they should; see "Two ways in". |
| An animation made on Apple silicon | Needs a real Mac. |
| macOS 14 | GitHub no longer offers it. |

## How iMessage stickers work

### The cut-out is only in the picker

StickerKit, the framework behind the sticker picker, has a class
`STKStickerStore` with `+stickerRepresentationWithData:size:`,
`+stickerWithRepresentation:identifier:isRecent:` and `-addSticker:`. Handed a
PNG or a GIF, it builds a user sticker whose one image is those bytes, unchanged,
typed `public.image`. Nothing in the store, the daemon or the message format
looks for a lifted subject.

That class cannot be used from outside, though. It talks to `stickersd`, and
`stickersd` only answers programs that hold the entitlement
`com.apple.private.stickers`. A connection from anything else is rejected. The
only way in for a program Apple did not sign is the library file itself.

### Where the library lives

| System | Path |
| --- | --- |
| macOS 15.7, 15.8, 26.7 | `~/Library/Group Containers/com.apple.stickersd.group/Stickers/stickers.stickerdb` |
| macOS 15.1 | `~/Library/Stickers/stickers.stickerdb` |
| iOS | `/var/mobile/Library/Stickers/stickers.stickerdb` |

It is a Core Data SQLite store. `stickersd` is a launch agent
(`com.apple.stickersd`, at
`/System/Library/PrivateFrameworks/Stickers.framework/Support/stickersd`). On a
phone that path is out of reach of any app and of any user without a jailbreak.

### What is in it

Apple ships the store's Core Data model on every Mac, at
`/System/Library/PrivateFrameworks/Stickers.framework/Versions/A/Resources/Stickers.momd`.
It has two entities.

`ManagedSticker` (table `ZMANAGEDSTICKER`):

| Attribute | Type | Notes |
| --- | --- | --- |
| `identifier` | UUID | |
| `type` | integer | 0 = recent, 1 = a sticker in your library |
| `externalURI` | string | `sticker:///user/identifier/<UUID>` for your own; other schemes are `emoji`, `memoji`, `third_party`, `recent` |
| `libraryIndex` | double | Ordering key. Apple's own code gives a new sticker the highest value of its type plus 1024. |
| `effect` | integer | 0 = none. The others are stroke, comic, puffy and iridescent. |
| `creationDate`, `lastUsedDate` | double | Seconds since 2001-01-01 |
| `name`, `accessibilityName`, `searchText` | string | |
| `byteCount`, `version` | integer | Apple's code writes version 1. |
| `metadata` | binary | |
| `attributionAdamID`, `attributionBundleIdentifier`, `attributionName` | | For stickers that came from an app |
| `sanitizedPrompt`, `promptPrimaryLanguageIdentifier` | string | Genmoji. macOS 26 adds `rewrittenPrompt`. |

`ManagedRepresentation` (table `ZMANAGEDREPRESENTATION`), one row per stored
image: `data` (the image file, stored inline), `uti`, `role`, `size_w`,
`size_h`, `index`, `isPreferred`, `byteCount`, `version`, `identifier`, and the
sticker it belongs to.

### The images

| Role | Type | What it is |
| --- | --- | --- |
| `com.apple.stickers.role.still` | `public.heic` | The sticker |
| `com.apple.stickers.role.keyboard` | `public.heic` | Small preview, at most 100 px |
| `com.apple.stickers.role.animated` | `public.heics` | The animation |
| `com.apple.stickers.role.stillVariant_320`, `_160`, `_96`, `_64`, `_40` | `public.heic` | Sizes for use inline in text |
| `com.apple.stickers.role.original` | | Defined by the framework; not written by this tool |

A still is HEIC with an alpha channel. An animation is one HEIF image sequence:
brand `msf1`, holding a cover image and two HEVC tracks, one for colour and one
for alpha, with each frame's duration in the track's timing table.

The roles matter. Asked for a sticker's images by role, Apple's code returns
nothing for a sticker that holds only an untyped original. imsticker writes the
still, the keyboard preview and the five inline sizes for a still image, and the
animation plus a still and a keyboard preview of its first frame for an animated
one.

### Two ways in

The library is mirrored to iCloud by Core Data, working from the store's
persistent history: a log of every save.

- **Rows written with SQL leave no trace in that history.** This is what
  StickerHack does, and what `--raw-sql` does here. The stickers are in the
  library on the Mac, but nothing has told the sync machinery about them.
- **A save through Core Data is recorded in it.** This is the default. The
  helper opens the library with Apple's own model, refuses to migrate or alter
  it, and saves the stickers as managed objects. Core Data writes the rows, the
  keys and the history itself.

Because it uses whatever model your macOS ships, the default route needs no
knowledge of which columns your version has. If Apple's model does not fit the
library, it stops before changing anything, and `--raw-sql` is there as the
fallback.

### How a sticker is sent

This part comes from open-source work on the iMessage protocol, not from the
checks above. A sticker travels as that image as an attachment plus placement
data, and the receiver draws whatever image arrives.

- The message's app identifier ends in `com.apple.Stickers.UserGenerated.MessagesExtension` for library stickers (`com.apple.messages.genmoji` for Genmoji).
- Associated message type `1000` is a sticker stuck onto a message; `2007` and `3007` add and remove a sticker tapback.
- Placement travels as attributes on the attachment: `spw` (bubble width), `sro` (rotation in radians), `ssa` (scale), `sxs` and `sys` (position), `sid` (sticker id), `shash`, `spv`, `stickerEffectType`.
- Outline, comic, puffy and shiny effects are a tag inside the HEIC: `stickerEffect:type="stroke"`, `"comic"`, `"puffy"` or `"iridescent"`.

### Two oddities

- **The last frame of an animation plays for as long as the one before it.**
  An HEIF sequence written by ImageIO reads back that way whatever delay was
  asked for. Where the last two delays differ, imsticker writes the last picture
  twice at half its delay each, so it is on screen for the time the source asked
  for.
- **Virtual Apple-silicon Macs cannot write an HEIF sequence at all.** ImageIO
  fails for want of a hardware video encoder.

## imsticker and StickerHack

[StickerHack](https://github.com/AdamEXu/StickerHack) found this technique, and
it is the simplest thing to try: `npx stickerhack ./image.png`. The differences,
each checked by the workflow here:

- **APNG timing.** StickerHack reads frame delays from GIF and HEICS files but not from APNG. Given an APNG with delays of 0.05, 0.2, 0.5 and 0.1 s, its encoder wrote 0.1 s for every frame. imsticker keeps them.
- **The sync history.** StickerHack writes SQL rows. imsticker saves through Core Data by default.
- **macOS versions.** The library moved and its columns changed between releases; StickerHack's fixed insert fails on 15.1. imsticker finds the library in either place and takes the layout from Apple's model.
- **Animated stickers.** StickerHack stores the animation alone. imsticker adds the still and the keyboard preview, and corrects the last frame's timing.
- **Size and undo.** StickerHack's author found that images over about 1 MB show up as stickers but do not send. imsticker shrinks the image until the encoded sticker is under that, imports many files in one go, and backs up first.

## No Mac?

There is no way to write to the native library from an iPhone alone: the
database is out of reach without a jailbreak. Two things that do work on a
phone:

- A sticker app that imports images as they are. Apple's sticker API takes PNG, APNG and GIF with transparency, but the stickers live in that app's tab of the drawer, not in your own library.
- Having someone with a Mac import the image and send it to you. Guides for iOS 18 describe saving a received sticker with touch and hold > Sticker Details (some call it Emoji Details) > the download button. I have not tested that with an imported sticker.

## Sources

Checked on real Macs by this repository's workflow: the model, the tables, the
roles and types, `stickersd`'s entitlement check, what StickerKit stores, the
structure of an animated sticker, and everything in the first table above. The
scripts are in `ci/`.

Taken from others:

- [StickerHack](https://github.com/AdamEXu/StickerHack) and [its issue #2](https://github.com/AdamEXu/StickerHack/issues/2): the technique, the 15.1 library path, and the reports that stickers written this way work in Messages. The code here was written separately, but its encoder follows StickerHack's closely: the same ImageIO calls, roles, sizes and quality settings. StickerHack is licensed GPL-3.0.
- [imessage-exporter, `sticker.rs`](https://github.com/ReagentX/imessage-exporter/blob/develop/imessage-database/src/message_types/sticker.rs): app identifiers and the effect tag.
- [rustpush, `messages.rs`](https://github.com/OpenBubbles/rustpush/blob/master/src/imessage/messages.rs): message types and placement attributes.
- [Apple Developer Forums thread 764980](https://developer.apple.com/forums/thread/764980): `stickersd` logs showing the iOS path.
- [iPhone Life: saving a received sticker](https://iphonelife.com/content/how-to-get-same-stickers-your-friend-just-sent-you-messages)

## Tests

```bash
python3 -m unittest discover -s tests
```

These run anywhere and cover the tool's own logic against stand-in databases.
The checks that need a Mac are in `ci/` and run on every push.
