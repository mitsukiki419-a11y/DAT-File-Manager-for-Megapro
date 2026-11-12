# Phase 21 - live sound, live video, smoother OSD, remote window, slow boot, MIDI player

## Live MIDI (midiout.py)
* **Device access:** winmm through ctypes (midiOutOpen / midiOutShortMsg / midiOutLongMsg), with no extra
  packages. "Microsoft GS Wavetable Synth" is always present on Windows. A SoundFont synth such as CoolSoft
  VirtualMIDISynth loaded with dreamsam_2695.sf2 gives the player's SAM2695 sound.
* **Emulator:** soc.MidiOut forwards each byte the firmware sends to its synth chip. A StreamParser rebuilds the
  messages (running status, SysEx; the chip-reset bytes are dropped), and the app sends them to the device as the
  emulated player plays them. The player runs in real time, so sound and lyric wipe stay together.
* **Sound setting:** "MIDI: <device>" (default on Windows), "SoundFont render" (the old way), or "No sound".
* **Player:** midiout.Player plays a MIDI file on a device with play, pause, stop and seek. On a seek, controller
  and program changes are chased.

## Live background video
* **Decoding:** a persistent ffmpeg process decodes the MPEG stream the firmware feeds its decoder window (fed as
  it arrives) into JPEG frames. The page shows them as a motion-JPEG stream (/api/bgv.mjpg).
* **Without ffmpeg:** there is no background video, and the page says so.
* **Restarts:** if ffmpeg exits, it is restarted at the next MPEG pack header.

## Smoother OSD
* /api/osd.png?after=V waits up to 2 s for the next OSD change, so the page shows each lyric step as soon as it
  is drawn. Before, the page polled about once a second, which made the lyrics lag.
* Redraws are capped at about 16 per second.

## Other
* **Fullscreen:** a button, or double-click the screen.
* **Pop-out remote:** /remote?t=TOKEN, a remote-control window with all keys (digits, Enter, arrows, stop,
  volume, key, karaoke functions).
* **Real time from power-on:** an option that shows the whole startup (READ, the MP100 loading screens) at the
  player's true speed. It doesn't resume the saved state for that boot. Pacing only waits between steps, so the
  emulation, including the state saved at the main loop, is identical.
* **Drive log:** the drive's command log was capped at 5,000 entries. After the background video had sent that
  many READ CD commands, song starts were no longer seen. It now keeps the newest entries with a running count.

## Song Manager 1.2
* A MIDI player in the song list: ▶ plays at once on the chosen MIDI device. The player bar has pause, stop and a
  position slider (seek). The "SoundFont render" preview is still available.

## Known limit
* At the very end of a background-video file the firmware waits for an end-of-stream signal from the video
  decoder (not emulated yet), so the video stops there until the next song. view.vob on MPPV45 holds about
  55 minutes of video.

## Speed option (v0.6.1)
* Speed: **Fast start** (default; runs flat out until the player waits for a song number, then real time),
  **Real time** (paced from the main loop, as in 0.6), **Instant** (never paced). The option can be changed
  while the emulator runs.
* Core speed-ups:
  * the plain peripheral register file lives in C; only hooked words call Python
  * the busy-waited status registers are computed in C (field phase 0x4D4, field parity 0x204/0x50C, ready bits
    0x4C0/0x070, forced handshakes)
  * one compare per instruction for all slow checks
  * built with -O3 and an x86-64-v2 baseline (CPUs from about 2009 onwards)
* Measured here: 197 M instructions/s raw, about 1.8x real time. The lockstep check is still identical and the
  firmware MIDI timing still matches within 1 ms.
