# imsticker

Put any PNG, GIF or APNG into the native iMessage sticker library whole: no
subject lift, no cut-out, transparency and animation kept. By default the
picture is scaled to 768 px on its longest side (a smaller one is enlarged
unless you pass `--no-upscale`) and re-encoded as HEIC, or as an HEIF image
sequence for an animation. Both are lossy.

The short version of the reverse engineering: **the subject separation is a
step in Apple's "make a sticker" screen, not a property of a stored sticker.**
A sticker is a few images in a database that the `stickersd` daemon owns, and
Apple's own code for adding one stores the image it is handed. Put your own
images in that database and they are stickers.

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
in Messages.

The library is another program's data, and macOS can keep a terminal out of
that. If it does, imsticker says so instead of reporting the library missing:
give your terminal app Full Disk Access in System Settings > Privacy & Security,
reopen the terminal and run it again.

A file that cannot be encoded is reported as `FAILED` and skipped; the others
are still imported, and the command exits with status 1. Retry only the failed
files: a file imported twice becomes two stickers. A file that does not exist
stops the command before anything is imported.

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
| `--max-bytes N` | Re-encode at smaller sizes, down to 128 px, until the sticker's main image is at most N bytes. If that cannot be done the sticker is still imported at the smallest size tried, marked `still heavy`. Default 1000000; 0 turns it off. |
| `--no-upscale` | Never enlarge a small image. |
| `--resample nearest` | Keep pixel art crisp. `smooth` always smooths. The default, `auto`, smooths, except that an animated GIF of 256 px or less being enlarged keeps its hard pixels. |
| `--animation-only` | For an animated image, store only the animation, without the still and keyboard preview. |
| `--raw-sql` | Write the database rows directly instead of saving through Core Data. See "Two ways in". |
| `--no-restart` | Do not restart `stickersd` afterwards. |

A backup of the library is made before every import, in
`~/Library/Application Support/imsticker/Backups`.

Animated stickers need a Mac on which ImageIO can write an HEIF image sequence.
In the checks here GitHub's Intel runner could and its Apple-silicon runners
could not. All of them are virtual machines; no physical Mac was tested. Where
ImageIO cannot, imsticker reports the file as `FAILED`.

## What is verified, and what is not

Everything in the first table is checked by [the workflow in this
repository](.github/workflows/verify.yml) on GitHub's Macs: macOS 15.8 on Apple
silicon, macOS 15.7 on Intel, and macOS 26.7 on Apple silicon. Each of those
Macs has a real `stickersd` and a real sticker library, empty when the run
starts.

| Claim | How it is checked |
| --- | --- |
| The Swift helper builds | Compiled with Swift 6.1 and 6.3. |
| The Python runs on what Apple ships | The unit tests pass on `/usr/bin/python3` (3.9.6). |
| A still is stored as the picture you gave it | Every stored image is decoded with ImageIO and compared with the source for size, colour and a fully transparent corner. A half-transparent stripe is compared on the images at least 100 px tall. |
| An animation keeps its frames, order, timing and transparency, and is written to loop forever | The same, per frame, for a GIF and an APNG with uneven delays. Intel runner only: on GitHub's Apple-silicon runners ImageIO could not write the format. |
| The tool finds the real library and imports into it | The import into each Mac's own library is run without telling the tool where it is. |
| A save through Core Data enters the store's history | Asserted twice: on a store Core Data creates from Apple's model, and on the library `stickersd` keeps on each Mac. The job fails unless every sticker and every image saved is in the history tables. |
| Apple's own code accepts what the tool writes | Apple's sticker store code is run in-process on a copy of that library and asked for the imported stickers by identifier. The job fails unless it returns every one with its still and keyboard images, and on Intel the animation. |
| The library survives | `stickersd` is restarted on top of the imported stickers. The job fails unless they are still there and the database passes its integrity check. |

One thing the runs showed that I cannot interpret: after such an import, on
macOS 15, `stickersd`'s Spotlight indexer logs "Could not create attributes" a
number of times that matches the stored images other than keyboard previews (6
for a still's seven images). On macOS 26 it logs nothing. There was no sticker
made by Apple's own software on those machines to compare with, so whether that
is normal is not known.

| Not verified | Why |
| --- | --- |
| That Messages shows and sends them | No way to drive the Messages window on a build machine. [StickerHack](https://github.com/AdamEXu/StickerHack) users report that stickers written as SQL rows appear and send. |
| That they reach your iPhone | The build machines have no iCloud account, so `stickersd` never gets as far as uploading, whichever way the stickers were written. StickerHack's README says its stickers do sync. The default route here also records the save in the history that Core Data's iCloud mirroring works from; whether that makes a difference was not tested. |
| A library that already holds stickers made by Apple's software, or that has synced | Each build machine's library starts empty. The only stickers in it during an import are ones this tool has just added. |
| An animation made on Apple silicon, or on macOS 26 | ImageIO on GitHub's Apple-silicon runners could not write one. It needs a physical Mac, and none was tested. |
| What macOS's privacy protection does on your own Mac | On the build machines nothing stood between a terminal and the library. On your Mac macOS may ask first, or refuse. The message imsticker gives for a refusal was only exercised by simulating one in the unit tests. |
| macOS 15.1, where the library is in another place | No build machine runs it. Only the unit tests cover it, on stand-in files. |
| macOS 14 | GitHub no longer offers it. |

## How iMessage stickers work

### The cut-out is only in the picker

StickerKit, the framework behind the sticker picker, has a class
`STKStickerStore` with `+stickerRepresentationWithData:size:`,
`+stickerWithRepresentation:identifier:isRecent:` and `-addSticker:`. Handed a
PNG or a GIF, it builds a user sticker whose one image is those bytes, typed
`public.image`, with no role. Apple's store code, run by the workflow on a copy
of the library, saved them as given.

`stickersd` is built on that same store code, but its own add path could not be
reached (see below), and the message format as others have documented it has no
field for a lifted subject. Neither of those two was tested here.

`STKStickerStore` cannot be used from outside. It talks to `stickersd`, and
`stickersd` refused every connection from the test program. The daemon contains
an entitlement check and the string `com.apple.private.stickers`, which is
presumably the entitlement it wants. The only way in for a program Apple did
not sign is the library file itself.

### Where the library lives

| System | Path | Known from |
| --- | --- | --- |
| macOS 15.7, 15.8, 26.7 | `~/Library/Group Containers/com.apple.stickersd.group/Stickers/stickers.stickerdb` | The build machines |
| macOS 15.1 | `~/Library/Stickers/stickers.stickerdb` | StickerHack's issue #2 |
| iOS | `/var/mobile/Library/Stickers/stickers.stickerdb` | A string in the framework, and `stickersd` logs posted on Apple's forums |

It is a Core Data SQLite store. `stickersd` is a launch agent
(`com.apple.stickersd`, at
`/System/Library/PrivateFrameworks/Stickers.framework/Support/stickersd`). On a
phone that path is out of reach of any app and of any user without a jailbreak.

### What is in it

Apple ships the store's Core Data model on every Mac, at
`/System/Library/PrivateFrameworks/Stickers.framework/Versions/A/Resources/Stickers.momd`.
It has two entities. What follows is the current version of that model on macOS
15.7 and 15.8 (`Stickers-24E1`); the bundle also holds eleven older versions,
and older releases have fewer attributes.

`ManagedSticker` (table `ZMANAGEDSTICKER`):

| Attribute | Type | Notes |
| --- | --- | --- |
| `identifier` | UUID | |
| `type` | integer | 0 = recent, 1 = a sticker in your library |
| `externalURI` | string | `sticker:///user/identifier/<UUID>` for your own. The framework also has `emoji`, `memoji`, `third_party` and `recent` forms. |
| `libraryIndex` | double | Ordering key. Apple's own code gives a new sticker the highest value of its type plus 1024. |
| `effect` | integer | 0 = none, the only value seen here. The framework also names stroke, comic, puffy and iridescent effects; their numbers were not checked. |
| `creationDate`, `lastUsedDate` | double | Seconds since 2001-01-01 |
| `name`, `accessibilityName`, `searchText` | string | |
| `byteCount`, `version` | integer | Apple's code writes version 1. |
| `metadata` | binary | |
| `attributionAdamID`, `attributionBundleIdentifier`, `attributionName` | | For stickers that came from an app |
| `sanitizedPrompt`, `promptPrimaryLanguageIdentifier` | string | Genmoji. Not in every release: StickerHack's issue #2 reports a 15.1.1 library without them. macOS 26 adds `rewrittenPrompt`. |

`ManagedRepresentation` (table `ZMANAGEDREPRESENTATION`), one row per stored
image: `data` (the image file, stored inline), `uti`, `role`, `size_w`,
`size_h`, `index`, `isPreferred`, `byteCount`, `version`, `identifier`, and the
sticker it belongs to.

### The images

The role names below are in Apple's binaries. The types and sizes are what this
tool writes, following StickerHack; no sticker made by Apple's own software was
available to compare with.

| Role | Type | What it is |
| --- | --- | --- |
| `com.apple.stickers.role.still` | `public.heic` | The sticker |
| `com.apple.stickers.role.keyboard` | `public.heic` | Small preview. This tool writes it at most 100 px. |
| `com.apple.stickers.role.animated` | `public.heics` | The animation |
| `com.apple.stickers.role.stillVariant_320`, `_160`, `_96`, `_64`, `_40` | `public.heic` | Copies of the still for use inline in text. This tool writes `_320` up to the larger of `--max-edge` and 320 px, and the others at most 160, 96, 64 and 40 px. |
| `com.apple.stickers.role.original` | | Defined by the framework; not written by this tool |

A still is HEIC with an alpha channel. An animation, as ImageIO writes it, is
one HEIF image sequence: brand `msf1`, holding a cover image and two HEVC
tracks, one for colour and one for alpha, with each frame's duration in the
track's timing table.

The roles matter. Asked for a sticker's images by role, Apple's code returns
nothing for a sticker whose only image has no role, which is what
`STKStickerStore` builds. imsticker writes the still, the keyboard preview and
the five variants for a still image, and the animation plus a still and a
keyboard preview of its first frame for an animated one.

### Two ways in

The library is set up to be mirrored to iCloud by Core Data: `stickersd`
configures CloudKit on it, and the store carries Core Data's mirroring and
history tables. By Core Data's design that mirroring exports from the store's
persistent history, a log of every save.

- **Rows written with SQL leave no trace in that history.** This is what
  StickerHack does, and what `--raw-sql` does here. The stickers are in the
  library on the Mac, and by that design the mirroring has not been told about
  them, so they may not reach iCloud on their own. This was not tested, and
  StickerHack's README says its stickers do sync to an iPhone or iPad.
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

### Three oddities

- **The last frame of an animation reads back with the delay of the one before
  it.** Seen on macOS 15.7 (Intel): a sequence written through ImageIO with
  delays of 0.05, 0.2, 0.5 and 0.1 s reads back through ImageIO as 0.05, 0.2,
  0.5, 0.5. Where the last two delays differ, imsticker writes the last picture
  twice at half its delay each, so it is on screen for the time the source asked
  for.
- **GitHub's virtual Apple-silicon Macs (macOS 15.8 and 26.7) cannot write an
  HEIF sequence.** ImageIO's writer reports failure there even for a two-frame
  test, although the same Macs write HEIC stills. The cause was not established.
- **An animation's sides are kept even.** An odd side is padded with one
  transparent pixel rather than the picture being squeezed.

## imsticker and StickerHack

[StickerHack](https://github.com/AdamEXu/StickerHack) found this technique, and
it is the simplest thing to try: `npx stickerhack ./image.png`.

The differences are below. StickerHack's side of the first, and of the
last-frame timing in the fourth, was measured by the workflow here, which runs
StickerHack's own encoder on the Intel Mac. The rest of what is said about
StickerHack comes from reading its source, its README and its issue #2.
imsticker's side of each is checked by the workflow, except the 15.1 layout and
the size limit, which only its unit tests cover.

- **APNG timing.** StickerHack reads frame delays from GIF and HEICS files but not from APNG. Given an APNG with delays of 0.05, 0.2, 0.5 and 0.1 s, its encoder wrote 0.1 s for every frame. imsticker keeps them.
- **The history.** StickerHack writes SQL rows. imsticker saves through Core Data by default.
- **macOS versions.** The library moved and its columns changed between releases; StickerHack's issue #2 reports its fixed path and insert failing on 15.1. imsticker looks in both places and takes the layout from Apple's model.
- **Animated stickers.** StickerHack stores the animation alone. imsticker adds the still and the keyboard preview, and corrects the last frame's timing.
- **Size and undo.** StickerHack's author found that source images over about 1 MB show up as stickers but do not work when used. imsticker applies that figure to what it stores, shrinking until the sticker's main image is under it, and imports many files in one go. Both tools back the library up before every import; imsticker also has a `restore` command.

## No Mac?

There is no way to write to the native library from an iPhone alone: the
database is out of reach without a jailbreak. Two things that do work on a
phone:

- A sticker app that imports images as they are. Apple's sticker API takes PNG, APNG and GIF with transparency, but the stickers live in that app's tab of the drawer, not in your own library.
- Having someone with a Mac import the image and send it to you. Guides for iOS 18 describe saving a received sticker with touch and hold > Sticker Details (some call it Emoji Details) > the download button. I have not tested that with an imported sticker.

## Sources

Checked on GitHub's Macs by this repository's workflow: the model, the tables,
the role names, that `stickersd` refuses outside programs, what StickerKit
stores, the structure of an animated sticker, and everything in the first table
above. The scripts are in `ci/`.

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

## Licence

imsticker is free software under the [GNU General Public License, version
3](LICENSE), the licence of StickerHack, whose encoder this one follows. It
comes with no warranty.
