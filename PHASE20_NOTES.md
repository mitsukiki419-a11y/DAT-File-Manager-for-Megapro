# Phase 20 - fast CPU core (C), real-time player

## C interpreter core (fastcpu/cpu.c)
* **What it does:** the whole fetch/decode/execute loop runs in C. Instruction semantics are identical to the
  Python core, including:
  * the chip's 3-operand 16x16 `mult`/`multu`
  * lwl/lwr/swl/swr
  * the busy-wait loop skip
* **Memory:** SDRAM (phys 0) and the flash/RAM image (phys 0x08000000) are the Python bytearrays themselves,
  shared with C through ctypes. Python HLE code keeps reading and writing them directly.
* **Breakpoints:** breakpoints and HLE hooks use a bitmap by physical word address. The core returns to Python at
  a breakpoint; Python runs the hook and resumes.
* **MMIO:** goes through ctypes callbacks into the same Python _mmio_r/_mmio_w (hooks, windows, logging).
* **Fast paths, all in C:**
  * the decoder data window 0x1FFF0000 is plain memory, and its writes are captured and handed to VideoFIFO in
    bulk
  * the free-running timers PB+0x80/0x90/0x94
  * the write-only idle-loop register PB+0x4D8
  * a cached answer for the stream's write-index poll PB+0x6110 (valid until the next sector is due or any
    register write)
* **Interrupts:** the core calls the Python controller only when the instruction count reaches the controller's
  next deadline (IRQC.next_check). It also calls it after mask/ack register writes and after status-register
  changes (mtc0 / rfe).
* **Lockstep check:** the Python and C cores ran from the same snapshots (idle, and a song playing) for 40 M
  instructions with interrupts off. Registers, pc, hi/lo and all memory were identical.
* **Full-system check:** the firmware's MIDI output during a song matches the slow run note for note, with timing
  within 1 ms.
* **Builds:**
  * Linux: `gcc -O2 -shared -fPIC -o libkecpu.so fastcpu/cpu.c`
  * Windows: `python -m ziglang cc -target x86_64-windows-gnu -O2 -shared -o kecpu.dll fastcpu/cpu.c` (built
    here; exports cpu_run / cpu_struct_size; needs only the standard Windows 10/11 runtime)

  emu.Emu is the C core when the library loads. KE_PYCPU=1 forces the Python core.

## Speed

| | Python core | C core |
|---|---|---|
| Boot to "Select Song" (quick boot) | 213 s | 2-3 s |
| One emulated second while a song plays | 95 s | 0.7 s (C core alone) |
| App end-to-end test (boot, song 2, MIDI, video picture) | 375 s | 15 s |

## App changes (v0.5)
* **Real time:** after the main loop the emulator is held to real time (EmuJob._pace); startup runs flat out.
* **Song start fixed:** the song lookup decodes the record straight from the sector the firmware read. The old
  code scanned the whole 1 GB bank inside the emulator thread, which explains the earlier 2-minute delay. The
  lookup now runs in a worker thread, with disc access serialized (one file handle).
* **Song clock:** the firmware's first note-on is matched to the same note in the song file, giving the
  firmware's song start time. The page starts the audio at that position, so the sound follows the firmware's
  lyrics.
* **OSD:** the renderer uses numpy (9 ms instead of 23 ms) and redraws at most about 10 times a second.

## Song index correction (affects songdisc / Song Manager)
* **ML0 location word:** bits 28-30 are the **bank file number** (0 = AUDIO.DAT, 1 = AUDIO1.DA1,
  2 = AUDIO2.DA2 ...), and bits 0-27 are the offset >> 4 **inside that file**. The firmware's table entry +8 is
  file << 28 | sector.
* **Song numbers:** they are limited to 1..99999. The firmware's number entry (0x080B26D4) resets anything above
  99999.
* **Verified in the emulated player:**
  * an ISO written by the Song Manager boots
  * the added song #99500 is read from AUDIO2.DA2 and played
  * the deleted #4 plays nothing
