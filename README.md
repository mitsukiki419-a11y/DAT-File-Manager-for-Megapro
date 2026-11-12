# KaraokeEmu 0.6

Emulator for the **Sunplus MP100NSP karaoke-DVD firmware** (MEGAPRO PLUS MEGASOUND), plus a built-in
**disc-image reader**. Pure Python, standard library only.

## Run from source
    python karaoke_emu.py                       # opens http://127.0.0.1:8765 in your browser
    python karaoke_emu.py boot --fw MP100NSP.BIN --out display     # headless: boot + save what it displays
    python karaoke_emu.py iso disc.iso info     # also: ls [path] | tree | extract <path> <dest> | thumb <out.png>

Put `MP100NSP.BIN` (original, scrambled file) next to the program, or type its path in the page.
The program descrambles it itself. Put `ffmpeg` (ffmpeg.exe on Windows) next to the program if you want the
firmware's MPEG-2 pictures rendered as images.

## Build KaraokeEmu.exe (Windows)
1. Install Python 3.9+ from python.org (tick "Add python.exe to PATH").
2. (Optional) copy `ffmpeg.exe` into this folder to bundle it.
3. Double-click `build_exe.bat` -> `dist\KaraokeEmu.exe`.
`build_exe.sh` does the same on Linux/macOS (PyInstaller can't cross-compile, so the .exe must be built on Windows).

## What works (0.4)
* **Boot:** the firmware boots into its main loop. The first boot takes about half a minute. Quick boot saves that
  state next to the firmware, so later boots resume in seconds.
* **Karaoke disc in the virtual drive:** the firmware reads a karaoke ISO (MPPV42 / MPPV45 layout, KARA125):
  * passes the karaoke key-chip check
  * loads its song index (17,142 songs on MPPV45)
  * starts the background video
* **OSD:** the firmware's on-screen display is rendered with the real colours: "Select Song", the number entry
  (white 00 + yellow digits), the reserve list, menus and the screensaver.
* **Remote:** digits type a song number. **Enter (key 109)** reserves the song and the player starts it. The
  keyboard's number keys and Enter work while the page has focus.
* **Background video:** the firmware streams the VOB (READ CD). The stream is paced to playback speed, and the
  picture shown is decoded (ffmpeg) from exactly what the firmware feeds its MPEG decoder.
* **Songs:**
  * **How the firmware plays them:** the firmware loads the song record from the disc, its sequencer runs on the
    karaoke clock, and it sends serial MIDI to its GS synthesizer chip. The emulator captures that MIDI (download:
    "MIDI sent by the emulated player").
  * **Live playback:** when the firmware starts a song, the program plays the same song in real time with the
    SoundFont (dreamsam_2695.sf2 by default).
  * **Song browser:** section 5 lists and plays every song on the disc directly.
* **Disc images:** ISO9660, Joliet, UDF and raw 2352-byte images. Browse, extract and preview.

## New in 0.6
* **Sound:** the firmware's MIDI plays live on a Windows MIDI device, in step with the lyrics (choose it under the
  screen). "Microsoft GS Wavetable Synth" always works. For the real player's sound, install CoolSoft
  VirtualMIDISynth, load dreamsam_2695.sf2 in it, and pick it here.
* **Background video:** it moves now (needs ffmpeg.exe next to the program). Fullscreen with the button, or
  double-click the screen.
* **Pop-out remote:** a remote-control window with all keys.
* **Real time from power-on:** an option to watch READ and the loading screens at the player's true speed.
* **Song Manager:** ▶ plays songs live on a MIDI device, with pause, stop and seek.

## New in Song Manager 1.3
* **Add music videos…:** put videos into KARA125\MVV with a song-list entry, like the disc's own music videos.
  Enter the number on the remote to play one.
  * AVI files with DivX/Xvid video and MP3 sound are added as they are.
  * MP4, MKV, MOV, WMV, MPG and other formats are converted with ffmpeg (ffmpeg.exe next to the program).
* **Song list:** a filter for All / Karaoke songs / Music videos. Each music video has an "Open video" button.
* **Extract and delete:** extract saves a music video as .avi. Delete also removes its file from the new disc.

## New in Song Manager 1.4
* **Lyrics for music videos:** the "Lyrics…" button on a music video sets lyrics from a .kar file or a karaoke
  MIDI, or removes them. The player shows them over the video. Use the offset if they come too early or too late.
* **Adding videos with lyrics:** lyrics can be given when adding videos. A .kar or .mid file with the same name as
  the video is picked up automatically.

## Limits (please read)
* **Speed (new in 0.5):** the CPU runs in a C core (kecpu.dll on Windows, libkecpu.so on Linux), about 100-150 M
  instructions/s against the real chip's 108 MHz. Boot to "Select Song" takes a few seconds. After startup the
  player is held to real time, and song audio is lined up with the firmware's own song clock. If kecpu.dll is
  missing, the old Python core is used and runs about 100x slower.
* **Recorded audio:** audio.dat songs carry a recorded audio part (probably chorus/backing vocals) in an
  undecoded format. Only the MIDI is played.
* **Not emulated:**
  * the A/V decoder hardware itself (pictures come from ffmpeg)
  * microphone, echo and scoring audio
  * the analog drive servo (bypassed by the ATAPI-level virtual drive)
* **Inferred, not confirmed:** some hardware behaviour, such as interrupt periods and the display-status bits. Each
  one is marked INFERRED in soc.py.
* **Untested on Windows:** the author has not run the program on Windows or built the .exe. Run build_exe.bat on
  your PC.
