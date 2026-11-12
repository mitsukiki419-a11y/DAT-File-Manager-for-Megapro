# Phase 22 - Song Manager 1.3: music videos (Add MVV)

## How the disc holds music videos (MPPV45)
* **Files:** KARA125/MVV/<5-digit number>.mvv. Each file is an AVI with MPEG-4 ASP video (DivX 5 tag "DX50",
  560x400, 25 fps) and MP3 sound (44.1 kHz stereo). MPPV45 has 154 files.
* **Song list:** each video has an ML0 entry. Word 1 = flags | kind << 17 | number. Kind 3 is an ordinary music
  video; kind 7 with flag 0x00100000 marks the "Just Dance" videos. Word 2 points to a small song record in a bank
  file, as for songs.
* **What the firmware does at mount:** it reads the Joliet MVV directory, plus one sector past its end. It builds
  a table of [number, MSF start (min << 16 | sec << 8 | frame of LBA + 150), size].
* **What the firmware does when a video number is entered** (traced in the emulator):
  1. reads the song record (1 sector)
  2. reads the AVI header (2, then 15 sectors)
  3. reads the idx1 index at the end of the file
  4. streams the file in 32-sector reads
* **The record:** with an all-zero record the video still plays from the start. The real MV records are not in
  the samples, so new videos get an all-zero record.

## Song Manager 1.3
* **Add music videos…:**
  * Pick any video files. AVI with DivX/Xvid and MP3 is copied as it is. Anything else is converted with ffmpeg:
    mpeg4 / DX50 tag, at most 720x480 with the aspect kept, at most 30 fps, MP3 128 kb/s 44.1 kHz stereo. The
    conversion runs as a job with progress.
  * The suggested numbers follow the highest music-video number.
  * The title and artist come from the file name ("Artist - Title").
* **Song list:**
  * New filter: All / Karaoke songs / Music videos.
  * Music videos show an "MV" tag and an "Open video" button, which opens the video in the computer's player.
* **Extract and delete:**
  * Extract writes a music video as <number> <title>.avi.
  * Delete removes the entry and its .mvv file from the new disc.
* **Saving:**
  * ISO: the new .mvv files are added at the end of the image and the MVV folder records are rewritten in both the
    ISO9660 and Joliet trees. If a folder no longer fits, it is moved to the end of the image; its parent record,
    its "." record and both path tables (L and M) are updated.
  * 2 MB of zero padding follows the added data. Without it, the firmware's read one sector past a directory
    failed at the end of the disc and it dropped the last directory sector (seen with 90 added videos).
  * Folder: files are copied or left out.

## Tests
* **mvv_disc_test.py (18 checks):**
  * conversion and the as-is check
  * ISO and folder output
  * re-read, extract
  * old files unchanged
* **Independent check:** pycdlib reads both directory trees of a disc with 90 added videos. That disc's MVV folder
  was moved and grew to 7 sectors.
* **test_songmgr.py:** 36 checks; 13 of them are new for music videos.
* **Emulator:**
  * On the written ISO, entering a new video's number makes the firmware read its record, header, index and
    then the stream.
  * With 90 added videos (moved folder), the last one (96090) is found and streamed.
  * The table lists all 242 videos.

## Limits
* **Video picture:** the emulator does not decode the MPEG-4 picture, so music videos play only on the real player.
* **Real MV records:** their content is unknown. A script that dumps one from a full disc would show whether they
  hold more, for example lyrics timing.
* **ffmpeg needed:** converting needs an ffmpeg with the mpeg4 and libmp3lame encoders (the usual full Windows
  builds have both).

# KaraokeEmu 0.6.2 - fix: black screen and very slow emulation while the page is open
* **Cause:** before the first background-video picture existed, the motion-JPEG endpoint (/api/bgv.mjpg) spun in
  a tight loop: its wait returned at once because version 0 counted as "newer than -1". That held Python's lock
  and starved the emulator thread and the OSD endpoint. The emulator ran about 60x slower than real time, and no
  picture or OSD ever came out. In a user report it took 48 minutes to reach 15 emulated seconds.
* **Fix:** the endpoint now waits properly for the first picture.
* **Decoder:**
  * Each picture is output once (-fps_mode passthrough, or -vsync 0 on older ffmpeg). Before, ffmpeg repeated
    pictures to fill timestamp gaps, producing thousands of copies.
  * The stream is handed to a writer thread through a bounded queue, so the emulator never waits for ffmpeg. At
    Instant speed, stream data is dropped when the decoder falls behind.
  * A new decoder starts at a pack header followed by a sequence header. Before, it could start without the
    picture size and hang ("Invalid frame dimensions 0x0").
  * A decoder that takes 8 MB of stream without producing a picture is restarted, at most twice a second.
* **Measured with the page open (video stream and OSD connected), Instant speed:** before, 2.7 emulated seconds
  in 30 s; after, 44.8 emulated seconds in 30 s.

# Song Manager 1.4 - lyrics for music videos
## How the player uses a music video's song record (traced in the emulator)
* **Reading:** after a video number is entered, the firmware reads the video's song record (first sector, then
  the rest when it is longer, e.g. 1 + 4 sectors for a 10 KB record), then the .mvv file.
* **Playing:** it loads the record as a song MIDI and starts its sequencer. With a song MIDI in the record it sent
  that song's GS reset, volume and program changes. With an all-zero record it sent only the reset.
* **So:** a music video's lyrics are a karaoke MIDI kept in its song record. The disc's own MV records are 2-4 KB,
  about the size of a lyrics MIDI. The real records are not in the samples; get_mv_records.bat copies them from a
  full disc, to confirm their exact contents (lyrics only, or with a melody).

## mvlyrics.py
* **Input:** a Megapro song MIDI (lyric events FF 05, '\r' ends a line, '@Title$3Artist' track name), a .kar file
  (text events FF 01 with '/' and '\' line marks, converted), or any MIDI with lyric events.
* **Output:** the lyrics MIDI, rebuilt on a fixed clock (960 ticks = 1 s). It has a conductor track and a lyrics
  track named '@Title$3Artist'. The music is optional (default off, because the video has its own sound).
* **Offset:** an offset in seconds moves the lyrics against the video.

## Song Manager
* **Lyrics… on every music video:**
  * shows the current lyrics (timed lines)
  * sets lyrics from a .kar or .mid file, with an offset and a "keep the music" option
  * removes them
* **Where changed records go:** a disc video with new or removed lyrics gets a new song record in the new
  AUDIOn.DAn. Its song-list entry points there; the video file is unchanged.
* **Add music videos:** has a lyrics column. A .kar or .mid file with the same name as the video is picked up
  automatically.
* **Song list:** a "lyrics" tag shows on videos whose record holds lyrics. Extract also writes "<number> <title>
  lyrics.mid".
* **Tests:** test_songmgr.py has 46 checks and mvv_disc_test.py 26. In the emulator, the moved record of a disc
  video is read from the new bank and then the video.

# Song Manager 1.4.1 - lyrics in the disc's own music-video format
get_mv_records.bat copied the 142 music-video records from the user's MPPV45 disc. Every one is a lyrics MIDI:
* **Layout:**
  * format 1, 120 ticks per beat, 120 bpm (240 ticks = 1 s)
  * track 0: port, time and key signature, tempo
  * track 1: port, name '@Title$1$2$3Artist$TMTV', then channel-16 controllers 71=2, 72=13, 70=1
* **Syllables:** each one is a lyric event (FF 05), followed at the same moment by a silent marker note (channel
  16, note 64, velocity 1, one tick long) that drives the wipe.
  * '^' ends a word inside a line (shown as a space).
  * A line ends with the lyric events '' and '\r' (some songs only '\r').
* **Controller 20 (screen control):**
  * 20, then 4: announce a section
  * four 0s, 0.5 s apart, ending 0.5 s before its first syllable: the count-in
  * 10: ends a section, 2 s after its last syllable, before a pause of over 18 s
  * 30: ends the song, 3 s after the last syllable
* **No music:** the records contain no music. The records are 2 or 4 KB.

mvlyrics.build now writes exactly this layout from a .kar or karaoke MIDI. It was checked by rebuilding all 142
real records from their own lyrics:
* syllables, markers and line breaks: identical in 141 (the rest differ only in end-of-line variants the disc
  itself mixes)
* section controls: the same sequence in 141
* in the emulator, a real record and a rebuilt one are handled the same way

The "keep the music" option was removed, since the disc's videos have none.

# KaraokeEmu 0.6.4 - fix: blocky, smeared background video
* **Cause:**
  * The firmware asks the drive for the next 4 sectors before the paced stream has delivered the previous ones.
  * The emulated drive started over at each new command and dropped the undelivered sectors. Only 1 of every 4
    sectors reached the player, giving grey blocks, smearing and frozen pictures.
* **Fix:** a command that continues the current stream extends it, and every sector is delivered in order, like a
  real drive.
* **Live video:** now decoded from the stream exactly as read from the disc (StreamDMA.since), not from the
  decoder window, which only ever receives part of the stream.
* **Checked:** the tapped stream is the disc's sectors in order with no gaps, and it decodes cleanly with ffmpeg.
  full_test10 passes: song start, firmware MIDI, live video, real-time pacing.

# KaraokeEmu 0.7.0
## Number keys during a song
* **Cause:** while a karaoke song plays, the firmware's song task 0x080979F4 runs inside the main loop's task
  call, so the main loop's remote-control poll (0x0801659C) is not reached. This was seen for whole stretches of
  a song, especially after the background video has run for a while. The remote's code then stays in the receiver
  register and every later key waits behind it.
* **Fix:** a key the register path has not taken within 0.25 s is handed straight to the firmware's key dispatcher
  0x080176AC. The call is injected at the entry of the song task and returns through a trampoline, after which the
  task runs as usual. The next 2 seconds of keys go the same way.
* **Checked:** digits typed during a song show the number entry ("000005 Wedding March", "000058"...). Enter
  reserves the song (R1 5810), and the lyrics keep wiping.

## Background video button
* Remote key 114 switches to the next background video, e.g. V1_VIEW/view.vob, then V2_SEXY/COSPLAY,
  EVENT, LIZA1, LIZA2... All other remote keys were tried; none changes the video.
* There is a "🎞 Change video" button under the screen and on the pop-out remote. Key 114 is now named "BGV".

## SoundFont render
* **Separate process:** the song is rendered in a separate process. Rendered in a thread, the synthesizer and the
  emulator took turns on Python's lock: a 1-minute song took about 9 s instead of 0.2 s, and the emulator lagged.
  The render process starts and loads the SoundFont ahead of time, when render mode is chosen and at boot.
* **Only one window plays the song:** the latest page to fetch it. Old audio is stopped before new audio starts.
  Before, a replaced audio element kept playing, giving two copies of the song.
* **Follows the player:** the song jumps to the player's position once, then follows its clock through the
  playback rate (0.85-1.15). A drift over 2 s jumps again.
* **Stops with the player:** on the remote's Stop key, or after 6 s without a note from the firmware.
* **Checked in a browser with two windows open:** one plays, it stays within 0.6 s of the player, and Stop
  silences it. Audio is ready about 1 s after the firmware's first note on a 2-core machine.

## Remembered settings
* KaraokeEmu.settings.json next to the program (or in ~/.karaoke_emu) keeps:
  * the firmware, disc image and SoundFont paths
  * the sound choice and MIDI device (by name)
  * speed, insert disc, quick boot, startup assist, real time from power-on, and the OSD layer
* They are restored when the program starts. The disc image is opened again automatically.

# KaraokeEmu 0.7.1 - background video breaks up after a short time
* **What was wrong:**
  * The emulated firmware has no video decoder holding it back, so it asks for its stream several times faster
    than real time. About 20 s in, it starts reading in a ladder of jumps: 4 sectors, then +12, +24, ... +264, and
    again. That is up to ~12,000 reads/s.
  * Since 0.6.4 the live picture was decoded from exactly what the firmware read, so it turned into smears and
    flat colour. In 0.6.4 the drive also kept a backlog of undelivered sectors, which made it worse; that change
    is reverted.
* **Fix: the picture now comes straight from the disc.**
  * The disc's video files are found once, and the picture plays the file the firmware is reading, starting at
    its sector (at the next sequence header).
  * It plays in real time (ffmpeg -re, 20 pictures/s).
  * It restarts when the firmware moves to another file, for example a new clip or the Change video key, and
    stops when the firmware stops streaming.
* **Lighter decoder:** ffmpeg runs at lower priority with one thread, so the player keeps real time.
* **Drive seek time:** a far jump now waits 80 ms before data comes, as on a real drive.
* **Status line:** shows the file being shown and the drive's reads per second, for diagnosis.
* **Rendered song:** follows the player more tightly (jumps if more than 0.8 s off, rate 0.8-1.2).
* **Checked:**
  * The live picture runs 25 pictures/s of emulated time for 45 s and is clean (trees, park path).
  * full_test10: live MJPEG and real time while the song plays.
  * test_app: 23 passed.
  * In a browser: one window plays the song, it stays in step, and Stop silences it.

# KaraokeEmu 0.7.2 - a stopped or earlier song played later
* **Cause:** in SoundFont mode, while a song was still rendering, the next song the player started was ignored
  ("already rendering"). Stop only worked on a song that had finished rendering. So after Stop and a new number,
  the earlier request finished rendering and played.
* **Fix:**
  * The latest song the player starts always takes over; an earlier render is dropped.
  * Stop on the remote also cancels a song that is still rendering.
  * A render that was stopped or replaced is never played.
* **Checked:** request song 2, press Stop while it renders, then request song 5. Song 5 (Wedding March) plays,
  song 2 never does. The browser test passes (one window, in step, Stop silences).

# KaraokeEmu 0.8.0 / Song Manager 1.5 - live sound only, with a built-in SoundFont synthesizer
* **livesynth.py:** a real-time SoundFont synthesizer, built in as the MIDI device "SoundFont synth (built in)".
  * It runs in its own process (no competition for Python's lock) and outputs through winmm waveOut with
    4 x 16 ms buffers, about 60 ms latency.
  * Voices: regions, loops, tuning, the volume envelope, attenuation and pan. Volume, expression, pan and pitch
    bend act live; also the sustain pedal, all notes off, reset controllers and exclusive classes. At most 64
    voices.
  * CPU: about 9% of one core for a full karaoke arrangement (Happy Birthday, 15 channels).
* **Default device:** with a SoundFont next to the program (dreamsam_2695.sf2) it is the default device in both
  programs. The emulator's firmware MIDI and the Song Manager's ▶ both play through it live: no rendering step and
  no delay.
* **Render mode removed:** "SoundFont render" is gone from both menus. A saved "render" choice switches to the
  built-in synth. A device that cannot start falls back to no sound instead of stopping the boot.
* **Checked:**
  * The firmware starts Happy Birthday and the synth's output is music (RMS 2,000-4,000 per second).
  * Song Manager playback works.
  * test_app 23 passed, test_songmgr 46 passed, full_test10 passes (real time while the song plays).
* **Faster emulator:** the song-task key hook (0.7.0) ran ~270,000 times per emulated second and cost about 20% of
  the emulator's speed. It is now armed only while a key waits.
* **Still open:** after ~20 s of background video the firmware starts reading the disc in a ladder of jumps
  (+12, +24 ... +264). While it does, its song task does not return to the main loop, so remote keys are not
  read. Seen with the test overlay, and very likely on the real disc too (the user's status showed sector
  3219297, inside that ladder). Under investigation.

# KaraokeEmu 0.8.1 / Song Manager 1.5.1 - the disc-scan and MIDI timing
* **The firmware's disc scan:**
  * **Found:** the emulated drive reported a stream read (READ CD) as finished the moment it was sent. A real
    drive stays busy (status bit7 at gp+3294) until the sectors are in the A/V buffer.
  * **What went wrong:** the firmware's 1 kHz stream service then kept issuing reads. After ~20 s of background
    video it fell into the scan ladder (+12, +24 ... +264; ~12,000 reads/s). Its song task then never returned to
    the main loop, so remote keys were not read, and the emulator slowed down.
  * **Fix:** the drive now stays busy until a stream read is delivered.
  * **Result:** no scan in 150 emulated seconds (a steady ~685 reads/s at 1x DVD rate). Late in a song the main
    loop runs 833x/s again and polls the remote.
* **MIDI timing:** every firmware MIDI message now carries its emulated time, and the built-in synth plays it at
  that time (40 ms behind the first message, sample-exact).
  * Before, messages came in bursts, one per emulator slice of ~18 ms, and played on 16 ms block boundaries.
  * Test: 8 drum hits stamped 0.25 s apart but sent in two bursts of four come out exactly 0.250 s apart.
  * The Song Manager's player stamps its messages the same way.
  * If the clocks drift apart by more than 0.25 s (the emulator running slow or fast), the synth locks onto them
    again.
* **Real time when keys are pressed:** "Fast start" switches to real time as soon as keys are pressed. If a number
  was typed before the player was seen as ready, a song could run at full emulator speed (too fast).
* **Diagnostics:** KaraokeEmu.log next to the settings gets a line every 5 s:
  * speed, mode, ready, song, reads/s, stream sector, video frames, MIDI bytes, waiting keys
  * a header per boot with the version, firmware, disc, CPU cores and sound device

# KaraokeEmu 0.8.2 - speed during songs, smoother OSD
* **OSD drawing:** off the emulator thread. The emulator only checks a cheap signature (0.2 ms); drawing and PNG
  encoding run in a worker thread, up to 30 frames/s.
  * Before: ~15 ms per frame on the emulator thread, at ~11 frames/s during a song (~20% of its time).
  * Integer renderer: output identical to the float version (pixel-compared). PNG compression level 1.
  * The page decodes each frame before showing it, so frames swap without flicker.
* **Old background still decoder disabled when the live picture is on.** It started an extra ffmpeg every 2 s to
  decode 700 KB of stream.
* **Measured on the 2-core test machine:**
  * During a song (real time): 0.49x before, 0.85x after. OSD 11 frames/s before, 19 after.
  * Idle (instant): ~1.0x.
* **Fixed:**
  * The diagnostics log stayed empty (its header used a missing attribute).
  * The speed figure could read empty right after the pacing clock reset.

# KaraokeEmu 0.8.3: spoken numbers
* **Where the voice lives:** the firmware image holds ten MP3 clips (44.1 kHz mono, 40 kbps, 0.5-0.9 s each)
  at 0x88123B78 and on. The lookup 0x0809885C(digit) returns each clip's address and size.
* **What the firmware does:** the digit handler 0x080B26D4 says the digit when 0x080B488C (settings byte +132,
  read from the EEPROM) is on. It then passes the clip to the audio DSP's MP3 mixer, 0x080AA908(ptr, size, vol).
* **What the emulator does (soc.KeyVoice):**
  * The emulated EEPROM reads blank, so the voice setting was off. The check now answers on by default.
  * Each play request hands the clip to App.VoiceOut, which decodes it with ffmpeg once (cached in a temp
    folder) and plays it with the Windows sound API.
  * A new digit cuts off the one still playing.
* **Page:** a "spoken numbers" checkbox, remembered in the settings file.
* test_voice.py: 4 checks.
* MP100NSP_sounds.zip: the ten number clips plus one other voice clip, as MP3 and WAV, and the score fanfare
  MIDIs (sections of the firmware image).

# KaraokeEmu 0.8.4: Setup button
* **Remote key 75 opens the player's SETUP menu.** It is in the karaoke key switch 0x080A05A4 (table 0x88156A08,
  index key-12, case 0x880A0840 → 0x080B4DC4). It was listed before as "title return".
* **Password:** the menu asks for a 4-digit password, which is settings +40. On a blank EEPROM that is 0000.
* **Menu:** Language, Sound, Coin/Time, Option, Password, Default Setup.
  * ▲▼◀▶ and Enter move through it.
  * Setup (75), Stop (25) or Return (24) close it.
* **Changes are not kept** after the emulator stops: the EEPROM is not saved yet. Quick start resumes a snapshot
  taken after the settings were read.
* **Buttons:** "⚙ Setup" on the main page and on the pop-out remote. On the remote it replaces "Return", which
  was the same key.

# KaraokeEmu 0.8.5: speed on Windows, Setup Enter, Greetings crash, OSD up to 60 fps
* **The user's log (16-core PC):** 0.4-0.8x, even idle with no song. The test machine gets ~1.0x of raw capacity.
  * Likely cause: Windows 11 "efficiency mode" (EcoQoS). The emulator console is never the window in front (the
    browser is), so its threads run on efficiency cores at a low clock.
  * The MIDI timestamps are emulated time, so a slow emulator means slow, late music.
* **winperf.py** (new):
  * The process opts out of power throttling (ProcessPowerThrottling, EXECUTION_SPEED and
    IGNORE_TIMER_RESOLUTION) and runs at ABOVE_NORMAL priority with 1 ms timers.
  * The emulator thread also opts out (ThreadPowerThrottling) and runs at HIGHEST.
  * The synth process: HIGH priority, and its thread HIGHEST.
  * The log header shows what Windows accepted (windows=...).
  * Each log line shows emu_thread_cpu=%: near 100% means the core is too slow; well below means the thread
    is waiting for CPU time.
* **C core (cpu.c):**
  * The instruction count, pc and npc are kept in locals and stored only around callbacks and returns.
    Checked bit-identical with the old core over 600 M instructions of boot.
  * A Python MMIO write clears the cached register only when it is near that register.
  * PB+0x904 (UART/MIDI status) and PB+0x720 (command register, busy bits) are now answered in C.
  * kecpu.dll is rebuilt (zig cc, x86_64-windows-gnu, -O2).
* **Stream write index PB+0x6110:** besides the earlier cases, it is answered from C for 0.5 ms after each look.
* **Setup:**
  * The menu takes key 72 as Select/Enter; key 109 only counts presses there. While Setup is open, Enter/OK
    (109) is now sent as 72, so Option > Greetings and the other sub-items open.
* **Greetings editor crash (IndexError):**
  * Firmware globals were read through the live r28. Some routines (seen in the Greetings editor) use r28 for
    something else, so the status code indexed SDRAM with a garbage address.
  * Fixed: soc.fw_gp() returns the fixed gp 0x800045F4.
  * Errors and their traceback now go into KaraokeEmu.log.
* **OSD:** up to 60 frames/s. Measured during a song: 33/s (was 20), with real-time speed unchanged (0.95x).

# KaraokeEmu 0.9.0: synth fixes and controller, saved player settings, songbook, random score, chorus
* **"Screaming" instruments.** Three causes, all fixed in livesynth.py:
  1. **Drum part on channel 11.** Every song start sends GS "use for rhythm part" (F0 41 10 42 12 40 1A 15 02):
     channel 11 is a drum kit. The synth ignored SysEx, so drum notes played on melodic instruments.
     * Now handled: GS reset, GM on, and rhythm-part assignment.
  2. **No filters.** 60% of this SoundFont's regions use the low-pass filter (initialFilterFc 5000-11000 cents) and
     57% open it with the modulation envelope. Without them the instruments are harsh.
     * Now a per-voice resonant low-pass: initialFilterFc, initialFilterQ, modEnvToFilterFc, modLfoToFilterFc,
       plus the SF2 default velocity-to-cutoff modulator.
     * The block filter runs in C: synth_biquad in kecpu.dll / libkecpu.so.
  3. **Data entry was ignored.** RPN 0 (pitch bend range) now works; NRPN is tracked.
     * NRPN 37xx are the SAM2695's mic/echo settings; the player's Vol keys change 37 07. They are not applied
       to the music.
  * **Also added:** vibrato and modulation LFOs on pitch (with CC1), and the modulation envelope on pitch.
  * New generators in sf2synth.GEN / DEFAULTS.
* **Synthesizer controller** (page panel; /api/synth GET and POST):
  * master volume 0-200%, transpose ±12, filters on/off, all notes off, reset
  * per channel: activity light, instrument name, mute, volume 0-200%, and **instrument choice** (any preset of
    the SoundFont, or the song's own)
  * Kept in the settings file. Port tracks each channel's program, bank and drum state.
* **Player settings saved (EEPROM):**
  * I2CBus.dump() and load(). The EEPROM is kept in KaraokeEmu.eeprom.json next to the settings and saved when
    the firmware writes it (on leaving Setup).
  * The quick-boot key includes the EEPROM, so changed settings get a cold boot once.
  * **Factory settings:** the bytes the firmware's own Default Setup writes (captured), with password 0000.
    Default Setup stores 99999 = no password, and that path leaves the Setup menu not reacting to the arrows.
  * **Before this, the EEPROM read blank (0):** e.g. the key range was 0, so Key up/down did nothing. With the
    defaults, Key up/down transposes MIDI-only songs. The firmware refuses key changes on songs with recorded
    audio, as on the player.
* **Songbook** (page panel; /api/songbook, /api/songbook/play):
  * The disc's ML0 list: search by number, title or singer; filter songs / music videos.
  * "Sing" types the number and Enter on the remote; "#" types only the number.
* **Random score:**
  * The song-end routine 0x08098CF4 shows the score (0x080AACF8, fanfares for 90/80/70/60) only when s0 >= 50 at
    0x08098DD0; it was always 0 (no microphone).
  * soc.RandomScore puts a random 70-100 there, mostly 80-99. There is a page switch.
* **Chorus (recorded voices):**
  * The firmware starts each clip with 0x080AA90C(ptr, size): the unscrambled WMA in SDRAM.
  * soc.Chorus passes it to App.ChorusOut, which decodes it once with ffmpeg (cached). It is mixed into the
    built-in synth's output at the emulated time the firmware started it (sample-exact; if decoding is late, it
    skips ahead). Other MIDI devices use the Windows sound API.
  * The synth reset byte 0xFF (song end or stop) stops it. There is a page switch.
  * The synth pipe now has a lock: it is written from several threads, and interleaved sends corrupted it.
* **Spoken numbers:** the page switch now decides both ways. Before, off fell back to the EEPROM setting, which
  the defaults turn on.
* **Music videos (not fixed yet):**
  * A READ of 0 sectors no longer cancels the transfer in progress.
  * The MV player still runs through the whole file in a fraction of a second (it issues 32-sector reads every
    ~1.5 ms; no decoder holds it back), then searches past the end of the disc and gives up. Pacing it needs the
    MPEG-4 decoder's status registers.

# Song Manager 1.6.0: "Add chorus"; KaraokeEmu 0.9.1: pop-out screen
* **How a song's chorus works** (checked on the disc and in the emulated player):
  * The record holds the clip(s) as scrambled WMA parts, sector-aligned from the record start. The header and
    part table cover the first (3n+10)*4 bytes; those bytes are kept at the MIDI's data offset, followed by the
    LZSS MIDI. The ML0 flag 0x00800000 is set.
  * The song's channel-16 track starts the clip with CC72 = 14 then CC70 = 1 (song 3: at 8.15 s).
  * songdisc.make_record(midi, parts) rebuilds songs 1 and 3 with a byte-identical part region; only the LZSS
    length differs.
  * karamidi.scramble_audio is the inverse of unscramble_audio (round trip checked).
* **songdisc:**
  * convert_chorus(file): any audio to WMA v2 in ASF, 48 kbps stereo, with ffmpeg.
  * chorus_trigger(midi) and set_chorus_trigger(midi, at): the start time. Old start events are removed; the
    new ones go into the channel-16 track, or a new track if the song has none.
  * KaraokeDisc.set_chorus(number, wma or None, at) and chorus_info().
  * save() writes added songs with their chorus and rehomes disc songs whose chorus changed (new record, flag set
    or cleared).
* **Song Manager:**
  * Each karaoke song has an "Add chorus…" / "Chorus…" button. The dialog takes an audio file and a start time
    (defaults to the current chorus's start), or removes the chorus.
  * The Add songs dialog has a Chorus column. An audio file named like the MIDI, or "<name> chorus", is picked up
    automatically.
  * The pending line counts chorus changes.
  * test_chorus.py: 8 checks. test_mgr_chorus.py: 15 checks (HTTP + dialog).
* **In the emulated player:** an added chorus is started by the firmware itself on time: 3.0 s requested →
  started 3.15 s after Enter; 1.25 s → 1.35 s.
* **KaraokeEmu "🖥 Pop-out screen" (/screen):**
  * Only the picture and OSD, scaled 3:2 to the window. Drag it to a TV or second monitor; double-click or F for
    fullscreen.
  * Keys: digits, Enter, arrows, Esc = Stop, S = Setup, V = Change video. The mouse pointer hides when idle.
