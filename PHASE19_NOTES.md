# Phase 19 - chorus clips, the full song index, and the Song Manager

## Recorded audio ("chorus") in song records
* In a record with n > 0 parts, the header's part table at +40 holds n entries of (offset, size, flags =
  0x1000000).
* Each part is a **Windows Media Audio file (ASF/WMA v2)**, for example 32 kHz stereo at 48 kbps, 45-60 s per
  clip. One song can have several clips (7 in one record of the sample).
* The record header occupies the first (3n+10)*4 bytes of the record. The ASF bytes it covers are kept at the
  record's data offset. The loader 0x080BA454 copies them back (memcpy at 0x080BA730) before using the parts.
* Each part is word-scrambled per 2048-byte sector by 0x080BAAF0(key, size, buf, 0, [size, count]):
  * key = (size * 14) % 493815 (0x788F7); count = size % 7 + 21
  * in every full sector (only (size-73) >> 11 sectors) it rotates `count` words at positions
    16 + (s1 % 496), where s1 is a 32-bit LCG: A starts at 1391 (+key each step), B starts at 1003 - 3*sector
    (+count each step), s1 = A*B + 17*sector + 7 (+ previous s1)
* The key was first recovered by brute force from the damaged ASF packet headers, which gave 0x788F7 rather than
  a misread 494839. The modulus was then confirmed in the code.
* All 54 complete clips in the sample decode with ffmpeg with no errors. Implementation:
  `karamidi.record_parts()` and `karamidi.unscramble_audio()`.

## title.hdr (contents table of title.bnk)
* Layout: a 16-byte header (word3 = key seed), then an LZSS stream scrambled with the 0x080BACB0 rotation:
  key = word3 ^ 0x47E39035, steps = key % 15 + 107, length 2048.
* The decoded table is 2048 bytes:
  * header checksum: word1 = XOR of words 0..32 (with word1 = 0) ^ 0x44444444
  * word 19 = title.bnk size
  * entries start at 0x88, 32 bytes each: [type, param, offset, size, a, b, c, hash]
* Decoding was checked byte-for-byte against the firmware's own LZSS output.
* MPPV45 sections:

  | Type | Content |
  |---|---|
  | 0 | 10 "WBF0" fonts / bitmaps |
  | 2 | menu graphics |
  | 9 | 8 bitmaps (280x80) |
  | 3, 4 | UI layout |
  | 14 | two 64 KB character-class tables |
  | **5** | **ML0 song list** |
  | 6 | — |
  | 7 | a MIDI |
  | 13 | end marker (offset = title.bnk size) |

* **The section hash is not checked by the firmware.** Tested: ML0 with songs 2 and 3 swapped and the old hash
  loads, and the swapped entry is used. Its algorithm is unknown, and the writer keeps the old value.

## ML0 = the complete song list
* Header: ['ML0\0', 1, n, 8 + 3n, 0, 0, 0, 0], followed by three parallel lists of n words:
  * L1 = number (bits 0-19) | flags (0x06000000 always; bit 23 (0x00800000) = record has recorded audio;
    bit 20 seen on 11 songs)
  * L2 = record offset >> 4 in the song bank (AUDIO.DAT then AUDIO1.DA1 ... as one stream)
  * L3 = offset (inside the section) of "title\0artist\0"
* The string table follows. The order is the disc's own (not strictly sorted); the firmware searches linearly.
* The firmware builds its in-memory song table (17,142 x 12 bytes) from this section only.
* `songdisc.build_ml0(parse_ml0(x)) == x` for the MPPV45 section (byte-identical rebuild).

## Song bank files
* The firmware opens "AUDIO.DAT" and "AUDIO%d.DA%d", so a new bank file KARA125/AUDIO2.DA2 can carry added songs
  while the original banks stay byte-identical.
* MIDI-only record (as in AUDIO1.DA1): [0, rsize, MIDI size, 0, L, 0 x5], then at +40 the LZSS stream, scrambled
  when L >= 2048. `karamidi.lzss_encode` (Okumura, greedy with hash chains) reproduces the original compressed
  size exactly on a test song.

## Writing a disc (songdisc.py)
* **ISO output:** a sparse copy of the original image. On the copy:
  * title.hdr is rewritten in place
  * title.bnk is rewritten in place, or appended if it grew
  * the new AUDIOn.DAn is appended
  * the KARA125 directory records are patched in both the ISO9660 and Joliet trees
  * the volume space size is updated

  The karaoke signature sector (284) and every other file stay identical.
* **Folder output:** writes all files.
* **Deleted songs** leave the list; their data stays unused on the disc.
* **Verified:** the written ISO reads back with the added song (MIDI intact), without the deleted song, with old
  songs intact, and with the signature sector identical. The emulated player was also booted with it and the
  added number entered (see the test result below).

## Song Manager (songmgr.py, KaraokeSongManager.exe)
* Opens an ISO or a disc folder (native file dialogs).
* Lists and searches 17,142 songs by number, title or artist.
* Previews a song with the SoundFont, and plays its chorus clip (as WAV via ffmpeg).
* **Extract:** .mid, plus "chorus N".wma and .wav.
* **Add / delete:** add MIDI files (number suggested, title from the file, both editable); delete and undo.
* **Save:** saves a new ISO or folder.
* Tests: test_songmgr.py (18 checks).
