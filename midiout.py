"""Live MIDI output and a Standard MIDI File player.

On Windows the system MIDI devices are used through winmm (no extra packages): "Microsoft GS Wavetable Synth" is
always there; a software synth such as CoolSoft VirtualMIDISynth loaded with dreamsam_2695.sf2 sounds like the
real player's SAM2695 chip. Elsewhere only the 'capture' device exists (used by the tests).
"""
import sys, threading, time, struct, ctypes

IS_WIN = sys.platform == 'win32'
SOUNDFONT = None          # set by the programs: enables the built-in 'sf2' device (livesynth.py)
SF2_NAME = 'SoundFont synth (built in)'


def set_soundfont(path):
    global SOUNDFONT
    SOUNDFONT = path if path and __import__('os').path.isfile(path) else None


# ------------------------------------------------------------------------------------------------ devices
class _MIDIOUTCAPSW(ctypes.Structure):
    _fields_ = [('wMid', ctypes.c_ushort), ('wPid', ctypes.c_ushort), ('vDriverVersion', ctypes.c_uint),
                ('szPname', ctypes.c_wchar * 32), ('wTechnology', ctypes.c_ushort), ('wVoices', ctypes.c_ushort),
                ('wNotes', ctypes.c_ushort), ('wChannelMask', ctypes.c_ushort), ('dwSupport', ctypes.c_uint)]


class _MIDIHDR(ctypes.Structure):
    _fields_ = [('lpData', ctypes.c_char_p), ('dwBufferLength', ctypes.c_uint), ('dwBytesRecorded', ctypes.c_uint),
                ('dwUser', ctypes.c_void_p), ('dwFlags', ctypes.c_uint), ('lpNext', ctypes.c_void_p),
                ('reserved', ctypes.c_void_p), ('dwOffset', ctypes.c_uint), ('dwReserved', ctypes.c_void_p * 8)]


def devices():
    """[(id, name)] of the MIDI output devices."""
    out = []
    if SOUNDFONT:
        try:
            import numpy                                         # noqa: F401  (the synth needs it)
            out.append(('sf2', SF2_NAME))
        except Exception:
            pass
    if IS_WIN:
        try:
            w = ctypes.windll.winmm
            for i in range(w.midiOutGetNumDevs()):
                caps = _MIDIOUTCAPSW()
                if w.midiOutGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) == 0: out.append((i, caps.szPname))
        except Exception:
            pass
    out.append(('capture', 'No sound (capture only)'))
    return out


class Port:
    """An open MIDI output. send(msg bytes) for channel messages and SysEx."""
    def __init__(self, dev):
        self.dev = dev; self.h = None; self.lock = threading.Lock(); self.sent = 0; self.log = []; self.synth = None
        self.chans = [dict(prog=0, bank=0, drum=(c == 9), vol=100, notes=0, last=0.0) for c in range(16)]
        if dev == 'sf2':
            if not SOUNDFONT: raise RuntimeError('choose a SoundFont (.sf2) first')
            import livesynth
            self.synth = livesynth.LiveSynth(SOUNDFONT)
        elif dev != 'capture':
            if not IS_WIN: raise RuntimeError('MIDI devices are only available on Windows')
            h = ctypes.c_void_p()
            r = ctypes.windll.winmm.midiOutOpen(ctypes.byref(h), int(dev), None, None, 0)
            if r: raise RuntimeError('could not open MIDI device %s (error %d)' % (dev, r))
            self.h = h
        self.name = dict(devices()).get(dev, str(dev))

    def send(self, msg, t=None):
        """t: the message's time in seconds on the sender's clock (the emulated player's), if known: the built-in
        synth then plays it exactly that far after the previous ones, instead of when it happens to arrive."""
        if not msg: return
        self._track(msg)
        with self.lock:
            self.sent += 1
            if self.synth is not None:
                self.synth.send(msg, t); return
            if self.h is None:
                self.log.append(bytes(msg)); del self.log[:-5000]; return
            w = ctypes.windll.winmm
            if msg[0] == 0xF0:
                buf = ctypes.create_string_buffer(bytes(msg), len(msg))
                hdr = _MIDIHDR(); hdr.lpData = ctypes.cast(buf, ctypes.c_char_p); hdr.dwBufferLength = len(msg); hdr.dwBytesRecorded = len(msg)
                if w.midiOutPrepareHeader(self.h, ctypes.byref(hdr), ctypes.sizeof(hdr)) == 0:
                    w.midiOutLongMsg(self.h, ctypes.byref(hdr), ctypes.sizeof(hdr))
                    for _ in range(200):                                   # wait until the driver is done with it
                        if w.midiOutUnprepareHeader(self.h, ctypes.byref(hdr), ctypes.sizeof(hdr)) != 65: break   # 65 = still playing
                        time.sleep(0.001)
            elif msg[0] >= 0x80:
                v = msg[0] | ((msg[1] if len(msg) > 1 else 0) << 8) | ((msg[2] if len(msg) > 2 else 0) << 16)
                w.midiOutShortMsg(self.h, v)

    def _track(self, m):
        """What each channel plays (for the synthesizer controller's display)."""
        try:
            st = m[0]
            if st == 0xF0:
                if len(m) >= 10 and m[1] == 0x41 and m[4] == 0x12:
                    a = m[5:8]
                    if a == b'\x40\x00\x7f':
                        self.chans = [dict(prog=0, bank=0, drum=(c == 9), vol=100, notes=0, last=0.0) for c in range(16)]
                    elif a[0] == 0x40 and (a[1] & 0xF0) == 0x10 and a[2] == 0x15:
                        part = a[1] & 15; c = 9 if part == 0 else (part - 1 if part <= 9 else part)
                        self.chans[c]['drum'] = m[8] != 0
                return
            C = self.chans[st & 15]; k = st & 0xF0
            if k == 0xC0: C['prog'] = m[1]
            elif k == 0xB0 and m[1] == 0: C['bank'] = m[2]
            elif k == 0xB0 and m[1] == 7: C['vol'] = m[2]
            elif k == 0x90 and len(m) > 2 and m[2]: C['notes'] += 1; C['last'] = time.time()
        except Exception:
            pass

    def control(self, **kw):
        if self.synth is not None and hasattr(self.synth, 'control'): self.synth.control(**kw); return True
        return False

    def silence(self):
        for ch in range(16):
            self.send(bytes([0xB0 | ch, 123, 0])); self.send(bytes([0xB0 | ch, 120, 0])); self.send(bytes([0xB0 | ch, 121, 0]))

    def close(self):
        try: self.silence()
        except Exception: pass
        if self.synth is not None:
            self.synth.close(); self.synth = None
        if self.h is not None:
            try: ctypes.windll.winmm.midiOutClose(self.h)
            except Exception: pass
            self.h = None


class StreamParser:
    """Turns a raw MIDI byte stream (as the firmware sends it to its synth chip) into complete messages."""
    def __init__(self, out):
        self.out = out; self.status = 0; self.cur = []; self.need = 0; self.sysex = None; self.t = None
        try:
            import inspect
            self._timed = len(inspect.signature(out).parameters) >= 2
        except (TypeError, ValueError):
            self._timed = False
    def _emit(self, msg):
        if self._timed: self.out(msg, self.t)
        else: self.out(msg)
    def feed(self, b, t=None):
        self.t = t
        if b >= 0xF8: return                                    # realtime / the 0xFF chip reset: not forwarded
        if b == 0xF0: self.sysex = [b]; return
        if self.sysex is not None:
            if b & 0x80 and b != 0xF7: self.sysex = None
            else:
                self.sysex.append(b)
                if b == 0xF7: self._emit(bytes(self.sysex)); self.sysex = None
                return
        if b & 0x80:
            if b >= 0xF0: self.status = 0; return
            self.status = b; self.need = 1 if (b & 0xE0) == 0xC0 else 2; self.cur = [b]; return
        if not self.status: return
        if not self.cur: self.cur = [self.status]
        self.cur.append(b)
        if len(self.cur) == 1 + self.need: self._emit(bytes(self.cur)); self.cur = []


# ------------------------------------------------------------------------------------------------ SMF player
def parse_smf(data):
    """All events of a Standard MIDI File as [(seconds, message bytes)] (channel messages + SysEx), plus
    [(seconds, text)] lyric/text events and the duration."""
    if data[:4] != b'MThd': raise ValueError('not a MIDI file')
    hl = struct.unpack('>I', data[4:8])[0]; fmt, ntrk, div = struct.unpack('>HHH', data[8:14])
    p = 8 + hl; raw = []; order = 0
    for _ in range(ntrk):
        if data[p:p + 4] != b'MTrk': break
        ln = struct.unpack('>I', data[p + 4:p + 8])[0]; t = data[p + 8:p + 8 + ln]; p += 8 + ln
        i = 0; tick = 0; run = 0
        def vlq():
            nonlocal i
            v = 0
            while i < len(t):
                c = t[i]; i += 1; v = (v << 7) | (c & 0x7F)
                if not c & 0x80: break
            return v
        try:
            while i < len(t):
                tick += vlq(); s = t[i]
                if s & 0x80: i += 1
                else: s = run
                if s == 0xFF:
                    typ = t[i]; i += 1; n = vlq(); d = t[i:i + n]; i += n
                    if typ == 0x51 and n == 3: raw.append((tick, order, 'tempo', int.from_bytes(d, 'big')))
                    elif typ in (0x01, 0x05): raw.append((tick, order, 'text', d))
                    elif typ == 0x2F: break
                elif s in (0xF0, 0xF7):
                    n = vlq(); d = t[i:i + n]; i += n
                    raw.append((tick, order, 'msg', bytes([0xF0]) + d if s == 0xF0 else d))
                else:
                    run = s; hi = s & 0xF0
                    nb = 1 if hi in (0xC0, 0xD0) else 2
                    raw.append((tick, order, 'msg', bytes([s]) + t[i:i + nb])); i += nb
                order += 1
        except IndexError:
            pass
    raw.sort(key=lambda x: (x[0], x[1]))
    tempo = 500000; last = 0; sec = 0.0; ev = []; texts = []
    for tick, _, kind, v in raw:
        sec += (tick - last) * tempo / (div if div < 0x8000 else 480) / 1e6; last = tick
        if kind == 'tempo': tempo = v
        elif kind == 'text': texts.append((sec, v.decode('latin1', 'replace')))
        else: ev.append((sec, v))
    return ev, texts, sec


class Player:
    """Plays a MIDI file on a Port in a background thread."""
    def __init__(self, port):
        self.port = port; self.thread = None; self.stop_flag = False; self.pause_flag = False
        self.pos = 0.0; self.dur = 0.0; self.title = ''; self.state = 'idle'; self.lyrics = []

    def play(self, midi, title='', start=0.0):
        self.stop()
        ev, texts, dur = parse_smf(midi)
        self.dur = dur; self.title = title; self.pos = start; self.stop_flag = False; self.pause_flag = False
        self.lyrics = texts; self.state = 'playing'
        def run():
            t0 = time.time() - start; i = 0
            while i < len(ev) and ev[i][0] < start:                 # chase controllers/programs, skip notes
                m = ev[i][1]
                if m[0] == 0xF0 or (m[0] & 0xF0) in (0xB0, 0xC0, 0xE0): self.port.send(m)
                i += 1
            while i < len(ev) and not self.stop_flag:
                if self.pause_flag:
                    p0 = time.time()
                    while self.pause_flag and not self.stop_flag: time.sleep(0.02)
                    t0 += time.time() - p0
                    continue
                now = time.time() - t0; self.pos = now
                while i < len(ev) and ev[i][0] <= now:
                    self.port.send(ev[i][1], ev[i][0]); i += 1            # stamped: exact timing in the built-in synth
                time.sleep(max(0.0, min(0.01, (ev[i][0] - now) if i < len(ev) else 0.01)))
            self.port.silence(); self.state = 'idle' if self.stop_flag else 'done'
            if not self.stop_flag: self.pos = self.dur
        self.thread = threading.Thread(target=run, daemon=True); self.thread.start()

    def pause(self, on=None):
        self.pause_flag = (not self.pause_flag) if on is None else bool(on)
        self.state = 'paused' if self.pause_flag else 'playing'
        if self.pause_flag: self.port.silence()

    def stop(self):
        if self.thread and self.thread.is_alive():
            self.stop_flag = True; self.thread.join(2)
        self.state = 'idle'

    def status(self):
        return dict(state=self.state, title=self.title, pos=round(self.pos, 1), dur=round(self.dur, 1), device=self.port.name)
