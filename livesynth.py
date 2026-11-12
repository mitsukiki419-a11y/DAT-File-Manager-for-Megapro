"""Real-time SoundFont synthesizer: a MIDI output device built into the programs.

It plays MIDI messages as they come, with a SoundFont (for example dreamsam_2695.sf2, the sound of the real player's
SAM2695 chip), straight to the computer's speakers. There is no rendering step and no extra program to install.

The synthesizer runs in its own process, so it never competes with the emulator for Python's lock. Audio goes out
through winmm waveOut on Windows: four 16 ms buffers, about 60 ms latency. Elsewhere it can only write to a WAV
file (KE_SYNTH_WAV) or nowhere, which is what the tests use.

Voices: SoundFont regions by bank/program/key/velocity, sample loops, root key and tuning, the volume envelope,
attenuation and pan. Channel volume, expression, pan and pitch bend act live on sounding notes; the sustain pedal,
all-notes-off and reset-controllers work. At most 64 voices: the oldest released voice is taken first.
"""
import os, sys, time, math, struct

RATE = 32000
DRUM_SUB = {27: 37, 28: 39, 29: 75, 30: 75, 31: 37, 32: 37, 33: 76, 34: 53, 82: 70, 83: 54, 84: 81, 85: 75,
            86: 64, 87: 64, 88: 49}
STATS = dict(steals=0, steal_on=0, peak=0, silent={})          # voice use (tests)
BLOCK = 512                   # 16 ms
NBUF = 4
LOG_PATH = None                # the app's log file: a crash of the synth process is written there
MAX_VOICES = 256              # (layered presets use 2+ voices per note; 64 dropped notes in dense songs)


def _load_biquad():
    """The block filter from the emulator's C library (kecpu.dll / libkecpu.so), or None."""
    import ctypes
    names = ('kecpu.dll',) if os.name == 'nt' else ('libkecpu.so',)
    dirs = [os.path.dirname(os.path.abspath(__file__))]
    if getattr(sys, '_MEIPASS', None): dirs.insert(0, sys._MEIPASS)
    if getattr(sys, 'frozen', False): dirs.append(os.path.dirname(sys.executable))
    for d in dirs:
        for n in names:
            try:
                lib = ctypes.CDLL(os.path.join(d, n)); f = lib.synth_biquad
                f.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p]; f.restype = None
                return f
            except (OSError, AttributeError):
                continue
    return None


def _load_voice():
    """The C voice renderer (synth_voice in kecpu.dll / libkecpu.so), or None (then numpy renders)."""
    import ctypes
    names = ('kecpu.dll',) if os.name == 'nt' else ('libkecpu.so',)
    dirs = [os.path.dirname(os.path.abspath(__file__))]
    if getattr(sys, '_MEIPASS', None): dirs.insert(0, sys._MEIPASS)
    if getattr(sys, 'frozen', False): dirs.append(os.path.dirname(sys.executable))
    for d in dirs:
        for n in names:
            try:
                lib = ctypes.CDLL(os.path.join(d, n)); f = lib.synth_voice
                f.argtypes = [ctypes.c_void_p, ctypes.c_int64, ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p,
                              ctypes.c_void_p, ctypes.c_void_p]
                f.restype = ctypes.c_int32
                return f
            except (OSError, AttributeError):
                continue
    return None


def _synth_process(conn, sf_path, wav_path=None, log_path=None):
    try:
        _synth_main(conn, sf_path, wav_path)
    except (KeyboardInterrupt, SystemExit, EOFError, BrokenPipeError):
        return                                      # the program is closing: not an error
    except BaseException:
        import traceback, time as _t
        if log_path:
            try:
                with open(log_path, 'a', encoding='utf-8') as f:
                    f.write('%s SYNTH ERROR (SoundFont %s)\n%s\n' % (_t.strftime('%H:%M:%S'), os.path.basename(sf_path), traceback.format_exc()))
            except Exception: pass
        raise


def _synth_main(conn, sf_path, wav_path=None):
    try:
        import winperf; winperf.process('high'); winperf.thread('highest')    # audio: never 'efficiency mode'
    except Exception:
        pass
    import numpy as np
    import sf2synth
    from sf2synth import GEN, _default, _tc
    sf = sf2synth.SoundFont(sf_path)
    smp = sf.samples; nsmp = len(smp)
    ar_full = np.arange(BLOCK, dtype=np.float64)
    import ctypes
    biquad = None if os.environ.get('KE_NOFILTER') else _load_biquad()
    cvoice = None if os.environ.get('KE_PYSYNTH') else _load_voice()     # whole voices in C: ~20x faster
    out_dev = _WaveOut() if sys.platform == 'win32' and not wav_path else None
    wav = open(wav_path, 'wb') if wav_path else None
    if wav: wav.write(b'\0' * 44)
    chans = None
    def reset_channels():
        return [dict(prog=0, bank=128 if c == 9 else 0, vol=100, expr=127, pan=64, bend=0, sustain=False, brange=2,
                     drum=(c == 9), rpn=None, mod=0)
                for c in range(16)]
    chans = reset_channels()
    lim = [MAX_VOICES]; light = [False]; load = [0.0]  # voice limit / filters off, lowered on a slow computer
    voices = []                                      # dicts
    clock = 0                                        # samples rendered

    def start_note(c, key, vel):
        C = chans[c]
        pk = key if C['drum'] else min(127, max(0, key + int(ctl['transpose'])))     # the key that sounds
        ov = ctl['prog'][c] if c < len(ctl['prog']) else -1                           # instrument chosen in the panel
        bank, prog = (ov // 128, ov % 128) if ov is not None and ov >= 0 else (C['bank'], C['prog'])
        nv0 = len(voices)
        # the same key again on this channel: the earlier note goes to its release (as on the chip) instead of
        # piling up voices - dense songs otherwise ran out of voices and long notes were cut off
        for v in voices:
            if v['ch'] == c and v['key'] == key and v['rel_t'] is None:
                v['on'] = False; v['held'] = False; v['rel_t'] = v['t']; v['rel_lvl'] = v['lvl']
        regs = [r for r in sf.regions(bank, prog) if r['vr'][0] <= vel <= r['vr'][1]]
        hit = [r for r in regs if r['kr'][0] <= pk <= r['kr'][1]]
        if not hit and C['drum']:
            # GS drum notes this SoundFont's kits lack (27-34, 82-88: high Q, slap, scratch, sticks, metronome,
            # shaker, jingle bell, bell tree, castanets, surdo): the closest sound it has, so the part is not
            # left with holes. Then the standard kit if the chosen kit has no sample for the key.
            for k2 in (pk, DRUM_SUB.get(pk, pk)):
                hit = [r for r in regs if r['kr'][0] <= k2 <= r['kr'][1]]
                if not hit and prog != 0:
                    hit = [r for r in sf.regions(128, 0) if r['vr'][0] <= vel <= r['vr'][1] and r['kr'][0] <= k2 <= r['kr'][1]]
                if hit: pk = k2; break
        if not hit and regs and not C['drum']:
            # a key above / below every sample of the instrument: the nearest one, transposed (the chip plays these;
            # before, such notes were silent)
            dist = lambda r: r['kr'][0] - pk if pk < r['kr'][0] else pk - r['kr'][1]
            best = min(dist(r) for r in regs)
            if best <= 24: hit = [r for r in regs if dist(r) == best]
        for r in hit:
            s = r['s']; g = r['g']
            gv = lambda name: g.get(GEN[name], _default(GEN[name]))
            start = s['start'] + gv('startAddrsOffset') + 32768 * gv('startAddrsCoarseOffset')
            end = s['end'] + gv('endAddrsOffset') + 32768 * gv('endAddrsCoarseOffset')
            ls = s['ls'] + gv('startloopAddrsOffset') + 32768 * gv('startloopAddrsCoarseOffset')
            le = s['le'] + gv('endloopAddrsOffset') + 32768 * gv('endloopAddrsCoarseOffset')
            if end <= start + 1 or end > nsmp: continue
            mode = gv('sampleModes') & 3
            root = gv('overridingRootKey'); root = s['key'] if root < 0 else root
            cents = (pk - root) * gv('scaleTuning') + gv('coarseTune') * 100 + gv('fineTune') + s['corr']
            excl = gv('exclusiveClass')
            if excl:
                for v in voices:
                    if v['ch'] == c and v['excl'] == excl and v['rel_t'] is None: v['rel_t'] = v['t']; v['rel_lvl'] = v['lvl']; v['rel'] = 0.005
            v = dict(ch=c, key=key, vel=vel, pos=float(start), start=start, end=end, ls=ls, le=le,
                     looped=mode in (1, 3) and le > ls + 2, base_step=s['rate'] / RATE * 2.0 ** (cents / 1200.0),
                     dly=_tc(gv('delayVolEnv')), att=_tc(gv('attackVolEnv')), hold=_tc(gv('holdVolEnv')),
                     dec=_tc(gv('decayVolEnv')), sus=10 ** (-min(max(gv('sustainVolEnv'), 0), 1440) / 200.0),
                     rel=max(_tc(gv('releaseVolEnv')), 0.01), atten=10 ** (-(gv('initialAttenuation') * 0.4) / 200.0),
                     pan_off=gv('pan') / 500.0, t=0.0, rel_t=None, rel_lvl=0.0, lvl=0.0, held=False, excl=excl,
                     on=True)
            # low-pass filter (most of this SoundFont's instruments are filtered; unfiltered they sound harsh and
            # shrill) with its modulation envelope, and the vibrato / modulation LFOs on pitch
            # (no velocity-to-cutoff modulator: with it, soft notes of the darker instruments of this SoundFont were
            # filtered almost to silence - heard as missing / muted instruments)
            fc = gv('initialFilterFc')
            v.update(fc=fc, fq=gv('initialFilterQ'), me_fc=gv('modEnvToFilterFc'), me_p=gv('modEnvToPitch'),
                     mdly=_tc(gv('delayModEnv')), matt=_tc(gv('attackModEnv')), mhold=_tc(gv('holdModEnv')),
                     mdec=_tc(gv('decayModEnv')), msus=1.0 - min(max(gv('sustainModEnv'), 0), 1000) / 1000.0,
                     mrel=max(_tc(gv('releaseModEnv')), 0.01),
                     vib=gv('vibLfoToPitch'), vf=8.176 * 2.0 ** (gv('freqVibLFO') / 1200.0), vd=_tc(gv('delayVibLFO')),
                     mlp=gv('modLfoToPitch'), mlf=gv('modLfoToFilterFc'), mf=8.176 * 2.0 ** (gv('freqModLFO') / 1200.0),
                     md=_tc(gv('delayModLFO')), z=(ctypes.c_double * 2)(), filt=None)
            v['filt'] = biquad is not None and (fc < 13400 or v['me_fc'] > 0 or v['mlf'] != 0)
            voices.append(v)
        if len(voices) == nv0:
            k = (c, bank, prog, pk, vel); STATS.setdefault('silent', {})[k] = STATS['silent'].get(k, 0) + 1
        STATS['peak'] = max(STATS['peak'], len(voices))
        while len(voices) > lim[0]:                # drop the oldest released voice, else the oldest
            rel = [v for v in voices if v['rel_t'] is not None]
            x = min(rel, key=lambda v: v['lvl']) if rel else voices[0]      # the quietest fading note first
            STATS['steals'] += 1; STATS['steal_on'] += 0 if rel else 1
            if not rel: STATS.setdefault('stolen_ch', []).append(x['ch'])
            voices.remove(x)

    def note_off(c, key):
        drum = chans[c]['drum']
        for v in voices:
            if v['ch'] == c and v['key'] == key and v['on']:
                # drum parts ignore note-off (GS Rx.NoteOff off, as on the SAM2695): a hit plays its whole sample.
                # Released at once, drums with short notes were nearly silent ("muted" kick / snare in some songs)
                if drum and not v.get('looped'): continue
                v['on'] = False
                if chans[c]['sustain']: v['held'] = True
                elif v['rel_t'] is None: v['rel_t'] = v['t']; v['rel_lvl'] = v['lvl']

    def release_channel(c, kill=False):
        for v in list(voices):
            if v['ch'] != c: continue
            if kill: voices.remove(v)
            elif v['rel_t'] is None: v['on'] = False; v['rel_t'] = v['t']; v['rel_lvl'] = v['lvl']

    master = [1.0]
    # the synthesizer controller (the page's Synth panel): master volume, per-channel volume / mute, transpose,
    # filter on/off
    ctl = dict(master=1.0, mute=[False] * 16, chvol=[1.0] * 16, transpose=0, filter=True, prog=[-1] * 16, pcm=1.0)

    def sysex(m):
        nonlocal chans
        if m[:6] == b'\xf0\x7e\x7f\x09\x01\xf7' or m[:6] == b'\xf0\x7e\x7f\x09\x03\xf7':      # GM on
            chans = reset_channels(); return
        if len(m) >= 10 and m[1] == 0x41 and m[3] == 0x42 and m[4] == 0x12:                       # Roland GS DT1
            addr = m[5:8]; val = m[8]
            if addr == b'\x40\x00\x7f':                                                      # GS reset
                chans = reset_channels(); return
            if addr[0] == 0x40 and (addr[1] & 0xF0) == 0x10 and addr[2] == 0x15:              # use for rhythm part
                part = addr[1] & 0x0F
                c = 9 if part == 0 else (part - 1 if part <= 9 else part)
                C = chans[c]; C['drum'] = val != 0; C['bank'] = 128 if val else 0

    def handle(m):
        nonlocal chans
        if not m: return
        st = m[0]
        if st == 0xFF:                                                     # our own "reset" message
            chans = reset_channels(); voices.clear(); return
        if st == 0xF0:
            sysex(m); return
        if st < 0x80 or st > 0xF0: return
        kind = st & 0xF0; c = st & 15; a = m[1] if len(m) > 1 else 0; b = m[2] if len(m) > 2 else 0
        C = chans[c]
        if kind == 0x90 and b: start_note(c, a, b)
        elif kind == 0x80 or kind == 0x90: note_off(c, a)
        elif kind == 0xB0:
            if a == 0: C['bank'] = 128 if C['drum'] else b
            elif a == 1: C['mod'] = b
            elif a in (101, 100):                                       # RPN select
                r = C['rpn'] if isinstance(C['rpn'], list) else [127, 127]
                r[0 if a == 101 else 1] = b; C['rpn'] = r
            elif a in (99, 98):                                         # NRPN select
                r = C['rpn'] if isinstance(C['rpn'], tuple) else (127, 127)
                C['rpn'] = (b, r[1]) if a == 99 else (r[0], b)
            elif a == 6:                                                # data entry
                r = C['rpn']
                if isinstance(r, list) and r == [0, 0]: C['brange'] = min(b, 24)        # pitch bend sensitivity
                # (NRPN 37xx are the SAM2695's microphone/echo settings - the player's Vol keys change 37 07 -
                # not the music; they are left alone)
            elif a == 7: C['vol'] = b
            elif a == 11: C['expr'] = b
            elif a == 10: C['pan'] = b
            elif a == 64:
                C['sustain'] = b >= 64
                if not C['sustain']:
                    for v in voices:
                        if v['ch'] == c and v['held'] and v['rel_t'] is None:
                            v['held'] = False; v['rel_t'] = v['t']; v['rel_lvl'] = v['lvl']
            elif a == 120: release_channel(c, kill=True)
            elif a == 123: release_channel(c)
            elif a == 121: C.update(expr=127, bend=0, sustain=False, rpn=None, mod=0)
        elif kind == 0xC0: C['prog'] = a
        elif kind == 0xE0: C['bend'] = ((b << 7) | a) - 8192

    D2 = ctypes.c_double
    smp_ptr = None
    def render_part_c(N):
        nonlocal smp_ptr
        if smp_ptr is None: smp_ptr = smp.ctypes.data
        buf = np.zeros(N * 2, np.float32); bp = buf.ctypes.data
        dead = []
        for v in voices:
            C = chans[v['ch']]
            tb = v['t']
            if v['rel_t'] is None:
                a0 = v['mdly']; a1 = a0 + v['matt']; h1 = a1 + v['mhold']; d1 = h1 + v['mdec']
                me = 0.0 if tb < a0 else (tb - a0) / max(v['matt'], 1e-9) if tb < a1 else 1.0 if tb < h1 else \
                     1 - (1 - v['msus']) * (tb - h1) / max(v['mdec'], 1e-9) if tb < d1 else v['msus']
                v['mlvl'] = me
            else:
                me = v.get('mlvl', 0.0) * max(0.0, 1 - (tb - v['rel_t']) / v['mrel'])
            lfo_v = math.sin(2 * math.pi * v['vf'] * (tb - v['vd'])) if tb > v['vd'] else 0.0
            lfo_m = math.sin(2 * math.pi * v['mf'] * (tb - v['md'])) if tb > v['md'] else 0.0
            cents = (v['vib'] + C['mod'] * 50 / 127.0) * lfo_v + v['mlp'] * lfo_m + v['me_p'] * me
            step = v['base_step'] * 2.0 ** (C['bend'] / 8192 * C['brange'] / 12.0 + cents / 1200.0)
            amp = v['atten'] * (v['vel'] / 127.0) ** 2 * (C['vol'] / 127.0) ** 2 * (C['expr'] / 127.0) ** 2
            amp *= 0.0 if ctl['mute'][v['ch']] else ctl['chvol'][v['ch']]
            pan = min(max((C['pan'] - 64) / 64.0 + v['pan_off'], -1), 1)
            f_on = 0.0; cf = (0.0, 0.0, 0.0, 0.0, 0.0)
            if v['filt'] and ctl['filter'] and not light[0]:
                fcc = min(max(v['fc'] + v['me_fc'] * me + v['mlf'] * lfo_m, 1500.0), 13500.0)
                f = min(8.176 * 2.0 ** (fcc / 1200.0), 0.45 * RATE)
                q = max(0.5, 0.7071 * 10 ** (v['fq'] / 200.0))
                w0 = 2 * math.pi * f / RATE; cs = math.cos(w0); al = math.sin(w0) / (2 * q); a0n = 1 + al
                cf = ((1 - cs) / 2 / a0n, (1 - cs) / a0n, (1 - cs) / 2 / a0n, -2 * cs / a0n, (1 - al) / a0n); f_on = 1.0
            st_ = (D2 * 3)(v['pos'], v['t'], v['lvl'])
            prm = (D2 * 23)(step, v['end'], v['ls'], v['le'], 1.0 if v['looped'] else 0.0, v['dly'], v['att'], v['hold'],
                             v['dec'], v['sus'], v['rel'], -1.0 if v['rel_t'] is None else v['rel_t'], v['rel_lvl'], amp,
                             math.cos((pan + 1) * math.pi / 4), math.sin((pan + 1) * math.pi / 4), float(RATE), f_on, *cf)
            got = cvoice(smp_ptr, nsmp, bp, N, ctypes.addressof(st_), ctypes.addressof(prm), ctypes.addressof(v['z']))
            v['pos'], v['t'], v['lvl'] = st_[0], st_[1], st_[2]
            if got < N: dead.append(v)
        for v in dead:
            if v in voices: voices.remove(v)
        mix = buf.reshape(N, 2)
        mix *= 0.5 * master[0] * ctl['master']
        return mix

    def render_part(N):
        if cvoice is not None: return render_part_c(N)
        ar = ar_full[:N]
        mix = np.zeros((N, 2), np.float32)
        dead = []
        for v in voices:
            C = chans[v['ch']]
            tb = v['t']                                                     # this block's start (voice time)
            # modulation envelope (0..1) and LFOs, taken once per block
            if v['rel_t'] is None:
                a0 = v['mdly']; a1 = a0 + v['matt']; h1 = a1 + v['mhold']; d1 = h1 + v['mdec']
                me = 0.0 if tb < a0 else (tb - a0) / max(v['matt'], 1e-9) if tb < a1 else 1.0 if tb < h1 else \
                     1 - (1 - v['msus']) * (tb - h1) / max(v['mdec'], 1e-9) if tb < d1 else v['msus']
                v['mlvl'] = me
            else:
                me = v.get('mlvl', 0.0) * max(0.0, 1 - (tb - v['rel_t']) / v['mrel'])
            lfo_v = math.sin(2 * math.pi * v['vf'] * (tb - v['vd'])) if tb > v['vd'] else 0.0
            lfo_m = math.sin(2 * math.pi * v['mf'] * (tb - v['md'])) if tb > v['md'] else 0.0
            cents = (v['vib'] + C['mod'] * 50 / 127.0) * lfo_v + v['mlp'] * lfo_m + v['me_p'] * me
            step = v['base_step'] * 2.0 ** (C['bend'] / 8192 * C['brange'] / 12.0 + cents / 1200.0)
            pos = v['pos'] + ar * step
            n = N
            if v['looped']:
                over = pos >= v['le']
                if over.any(): pos[over] = v['ls'] + np.mod(pos[over] - v['ls'], v['le'] - v['ls'])
            else:
                lim = v['end'] - 1
                if pos[0] >= lim: dead.append(v); continue
                if pos[-1] >= lim: n = int(np.searchsorted(pos, lim)); pos = pos[:n]
            i0 = pos.astype(np.int64); fr = (pos - i0).astype(np.float32)
            wave = smp[i0] * (1 - fr) + smp[np.minimum(i0 + 1, nsmp - 1)] * fr
            t = v['t'] + ar[:n] / RATE
            # volume envelope
            a0 = v['dly']; a1 = a0 + v['att']; h1 = a1 + v['hold']; d1 = h1 + v['dec']; sus = v['sus']
            env = np.where(t < a0, 0.0, np.where(t < a1, (t - a0) / max(v['att'], 1e-9),
                           np.where(t < h1, 1.0, np.where(t < d1, 1 - (1 - sus) * (t - h1) / max(v['dec'], 1e-9), sus))))
            if v['rel_t'] is not None:
                env = v['rel_lvl'] * np.maximum(0.0, 1 - (t - v['rel_t']) / v['rel'])
                if env[-1] <= 0: dead.append(v)
            env = env.astype(np.float32)
            amp = v['atten'] * (v['vel'] / 127.0) ** 2 * (C['vol'] / 127.0) ** 2 * (C['expr'] / 127.0) ** 2
            amp *= 0.0 if ctl['mute'][v['ch']] else ctl['chvol'][v['ch']]
            pan = min(max((C['pan'] - 64) / 64.0 + v['pan_off'], -1), 1)
            sig = wave * env * amp
            if v['filt'] and ctl['filter'] and not light[0] and n:
                fcc = min(max(v['fc'] + v['me_fc'] * me + v['mlf'] * lfo_m, 1500.0), 13500.0)
                f = min(8.176 * 2.0 ** (fcc / 1200.0), 0.45 * RATE)
                q = max(0.5, 0.7071 * 10 ** (v['fq'] / 200.0))             # initialFilterQ: cB of resonance
                w0 = 2 * math.pi * f / RATE; cs = math.cos(w0); al = math.sin(w0) / (2 * q); a0n = 1 + al
                coef = np.array([(1 - cs) / 2 / a0n, (1 - cs) / a0n, (1 - cs) / 2 / a0n, -2 * cs / a0n, (1 - al) / a0n])
                x = np.ascontiguousarray(sig, dtype=np.float32)
                biquad(x.ctypes.data, n, coef.ctypes.data, ctypes.addressof(v['z']))
                sig = x
            mix[:n, 0] += sig * math.cos((pan + 1) * math.pi / 4)
            mix[:n, 1] += sig * math.sin((pan + 1) * math.pi / 4)
            v['lvl'] = float(env[-1]) if n else 0.0
            v['pos'] = float(pos[-1] + step) if n else v['pos']; v['t'] += N / RATE
            if n < N and v not in dead: dead.append(v)
        for v in dead:
            if v in voices: voices.remove(v)
        mix *= 0.5 * master[0] * ctl['master']
        return mix

    # Timing: a message stamped with the sender's clock (the emulated player's seconds) is played exactly that far
    # from the previous ones, LATENCY after its first arrival; unstamped messages play at the next sample.
    LATENCY = int(0.03 * RATE)
    SLACK_LATE = int(0.06 * RATE); SLACK_EARLY = int(0.06 * RATE)
    sched = []                                       # (due sample, order, message)
    offset = [None]; order = [0]
    def schedule(t, m):
        if t is None: due = clock
        else:
            due = int(t * RATE) + offset[0] if offset[0] is not None else None
            # (Re)lock when a message would play clearly late, or clearly later than it needs to. The upper bound
            # was 1 s: after the emulator fell behind for a moment the clocks re-locked late, and when it caught up
            # again the music stayed up to a second behind the lyrics for the rest of the song.
            if due is None or due < clock - SLACK_LATE or due > clock + LATENCY + SLACK_EARLY:
                offset[0] = clock + LATENCY - int(t * RATE); due = int(t * RATE) + offset[0]
            due = max(due, clock)
        order[0] += 1; sched.append((due, order[0], m))

    streams = []                                     # recorded audio (song chorus clips) being mixed in
    def mix_streams(out, clk0):
        g = 0.8 * ctl['pcm'] * ctl['master']
        done = []
        for k, st_ in enumerate(streams):
            start = max(clk0, st_['due']); a = st_['a']
            if start >= clk0 + BLOCK: continue
            src = start - st_['due']
            if src >= len(a):
                # a stream's piece may still come (late): kept 2 s so it goes on from there, not skipped
                if st_.get('sid') is None or clk0 - (st_['due'] + len(a)) > 2 * RATE: done.append(k)
                continue
            n = min(clk0 + BLOCK - start, len(a) - src)
            out[start - clk0:start - clk0 + n] += a[src:src + n] * g
        # (by position: list.remove compares the dicts, and so their sample arrays, which raised an error and
        # stopped the whole synth as soon as two clips of different lengths were playing)
        for k in reversed(done): del streams[k]

    def render_block():
        nonlocal clock
        clk0 = clock
        sched.sort()
        out = np.zeros((BLOCK, 2), np.float32); done = 0
        while True:
            nxt = sched[0][0] - clock if sched else BLOCK
            nxt = min(max(nxt, 0), BLOCK - done)
            if nxt: out[done:done + nxt] = render_part(nxt); done += nxt; clock += nxt
            while sched and sched[0][0] <= clock: handle(sched.pop(0)[2])
            if done >= BLOCK: break
        if streams: mix_streams(out, clk0)
        return (np.clip(out, -1, 1) * 32767).astype('<i2').tobytes()

    conn.send(('ready', sf.name))
    t_next = time.perf_counter()
    while True:
        while conn.poll():
            try: msg = conn.recv()
            except EOFError: msg = None
            if msg is None:
                if out_dev: out_dev.close()
                if wav: _finish_wav(wav)
                return
            t, m = msg
            if t == 'pcm':                                  # recorded audio to mix in: (seconds, 16-bit stereo bytes)
                ts, raw = m[0], m[1]; sid = m[2] if len(m) > 2 else None
                a = np.frombuffer(raw, dtype='<i2').astype(np.float32).reshape(-1, 2) / 32768.0
                due = (int(ts * RATE) + offset[0]) if (ts is not None and offset[0] is not None) else clock + LATENCY
                if sid is not None:
                    # a piece of a stream already known: straight after its previous piece, whatever its time
                    # stamp says (timing jitter left sample gaps / overlaps there: crackle). A stream that ran dry
                    # (the next piece came late) goes on from now, faded in, instead of skipping into the piece
                    st_ = next((x for x in streams if x.get('sid') == sid), None)
                    if st_ is not None:
                        end = st_['due'] + len(st_['a'])
                        if end > clock:
                            played = clock - st_['due']
                            if played > 0: st_['a'] = st_['a'][played:]; st_['due'] += played
                            st_['a'] = np.concatenate((st_['a'], a))
                            continue
                        del streams[next(i for i, x in enumerate(streams) if x is st_)]; due = max(due, clock + 64)
                        n = min(len(a), 96); a = a.copy(); a[:n] *= np.linspace(0.0, 1.0, n, dtype=np.float32)[:, None]
                    elif due < clock:
                        cut = clock - due                               # first piece late: skip what is past
                        if cut >= len(a): continue
                        a = a[cut:]; due = clock
                    streams.append(dict(due=due, a=a, sid=sid))
                    continue
                # a piece that continues a stream still playing (streamed sound comes in 20 ms pieces) is joined to
                # it: no click from a sample gap / overlap where two pieces meet, and nothing is skipped when a piece
                # arrives a little late
                for st_ in streams:
                    end = st_['due'] + len(st_['a'])
                    if abs(due - end) <= 64 and end > clock:
                        played = clock - st_['due']
                        if played > 0: st_['a'] = st_['a'][played:]; st_['due'] += played
                        st_['a'] = np.concatenate((st_['a'], a)); break
                else:
                    streams.append(dict(due=due, a=a))
                continue
            if t == 'pcmstop':
                streams.clear(); continue
            if t == 'ctl':
                for k, val in m.items():
                    if k in ctl: ctl[k] = list(val)[:16] if isinstance(ctl[k], list) else val
                continue
            schedule(t, m)
        t_r = time.perf_counter()
        data = render_block()
        load[0] = 0.95 * load[0] + 0.05 * (time.perf_counter() - t_r) / (BLOCK / RATE)
        # a slow computer: when rendering takes most of the real time, play fewer voices (oldest first) and skip
        # the filters, so the sound stays in time instead of falling behind; back up when there is room again
        if load[0] > 0.6 and lim[0] > 24:
            lim[0] = max(24, int(lim[0] * 0.75)); light[0] = True; load[0] = 0.45
        elif load[0] < 0.25 and lim[0] < MAX_VOICES:
            lim[0] = min(MAX_VOICES, lim[0] + 8)
            if lim[0] >= MAX_VOICES: light[0] = False
        if out_dev: out_dev.write(data)                 # waits while all buffers are playing
        else:
            if wav: wav.write(data)
            t_next += BLOCK / RATE                      # no device: keep real time
            d = t_next - time.perf_counter()
            if d > 0: time.sleep(d)


def _finish_wav(f):
    n = f.tell() - 44
    f.seek(0)
    f.write(b'RIFF' + struct.pack('<I', 36 + n) + b'WAVEfmt ' + struct.pack('<IHHIIHH', 16, 1, 2, RATE, RATE * 4, 4, 16) +
            b'data' + struct.pack('<I', n))
    f.close()


class _WaveOut:
    """winmm waveOut with NBUF reusable buffers."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ct = ctypes; self.w = ctypes.windll.winmm
        class WAVEFORMATEX(ctypes.Structure):
            _fields_ = [('wFormatTag', wintypes.WORD), ('nChannels', wintypes.WORD), ('nSamplesPerSec', wintypes.DWORD),
                        ('nAvgBytesPerSec', wintypes.DWORD), ('nBlockAlign', wintypes.WORD), ('wBitsPerSample', wintypes.WORD),
                        ('cbSize', wintypes.WORD)]
        class WAVEHDR(ctypes.Structure):
            _fields_ = [('lpData', ctypes.c_void_p), ('dwBufferLength', wintypes.DWORD), ('dwBytesRecorded', wintypes.DWORD),
                        ('dwUser', ctypes.c_void_p), ('dwFlags', wintypes.DWORD), ('dwLoops', wintypes.DWORD),
                        ('lpNext', ctypes.c_void_p), ('reserved', ctypes.c_void_p)]
        fmt = WAVEFORMATEX(1, 2, RATE, RATE * 4, 4, 16, 0)
        self.h = ctypes.c_void_p()
        r = self.w.waveOutOpen(ctypes.byref(self.h), 0xFFFFFFFF, ctypes.byref(fmt), None, None, 0)   # WAVE_MAPPER
        if r: raise RuntimeError('waveOutOpen failed (%d)' % r)
        self.bufs = [ctypes.create_string_buffer(BLOCK * 4) for _ in range(NBUF)]
        self.hdrs = []
        for b in self.bufs:
            h = WAVEHDR(); h.lpData = ctypes.cast(b, ctypes.c_void_p); h.dwBufferLength = BLOCK * 4
            self.w.waveOutPrepareHeader(self.h, ctypes.byref(h), ctypes.sizeof(h)); h.dwFlags |= 1   # mark free (DONE)
            self.hdrs.append(h)
        self.i = 0

    def write(self, data):
        h = self.hdrs[self.i]
        while not (h.dwFlags & 1): time.sleep(0.002)    # WHDR_DONE: free again
        self.ct.memmove(self.bufs[self.i], data, len(data))
        h.dwFlags &= ~1
        self.w.waveOutWrite(self.h, self.ct.byref(h), self.ct.sizeof(h))
        self.i = (self.i + 1) % NBUF

    def close(self):
        try:
            self.w.waveOutReset(self.h)
            for h in self.hdrs: self.w.waveOutUnprepareHeader(self.h, self.ct.byref(h), self.ct.sizeof(h))
            self.w.waveOutClose(self.h)
        except Exception:
            pass


class LiveSynth:
    """The parent side: send(msg) forwards MIDI messages to the synth process."""
    def __init__(self, sf_path, wav_path=None):
        import multiprocessing
        ctx = multiprocessing.get_context('spawn')
        self.conn, child = ctx.Pipe()
        import threading
        self._lock = threading.Lock()                 # the pipe is written from several threads (MIDI, chorus, panel)
        self.sf_path = sf_path; self.wav_path = wav_path; self._ctx = ctx; self._restarted = 0.0
        self.proc = ctx.Process(target=_synth_process, args=(child, sf_path, wav_path or os.environ.get('KE_SYNTH_WAV'), LOG_PATH), daemon=True)
        self.proc.start()
        if not self.conn.poll(60): raise RuntimeError('the SoundFont synthesizer did not start')
        tag, self.sf_name = self.conn.recv()

    def send(self, msg, t=None):
        try:
            with self._lock: self.conn.send((t, bytes(msg)))
        except Exception:
            self._revive()

    def _revive(self):
        """The synth process has gone (its error is in the log): start a new one, at most every 5 s."""
        import time as _t, threading as _th
        if self.proc.is_alive() or _t.time() - self._restarted < 5: return
        self._restarted = _t.time()
        def go():
            try:
                with self._lock:
                    self.conn, child = self._ctx.Pipe()
                    self.proc = self._ctx.Process(target=_synth_process, args=(child, self.sf_path, self.wav_path, LOG_PATH), daemon=True)
                    self.proc.start()
                if self.conn.poll(60): self.conn.recv()
            except Exception: pass
        _th.Thread(target=go, daemon=True).start()

    def reset(self):
        self.send(b'\xff')

    def pcm(self, t, raw, sid=None):
        """Mix recorded audio (16-bit stereo at RATE) in, starting at sender time t. Pieces with the same sid are
        one stream: played back to back, sample-continuous."""
        try:
            with self._lock: self.conn.send(('pcm', (t, bytes(raw), sid)))
        except Exception: pass

    def pcm_stop(self):
        try:
            with self._lock: self.conn.send(('pcmstop', None))
        except Exception: pass

    def control(self, **kw):
        """Synthesizer controller settings: master (0-2), mute (16 bools), chvol (16 x 0-2), transpose
        (semitones), filter (bool), prog (16 x bank*128+program, -1 = the song's own)."""
        try:
            with self._lock: self.conn.send(('ctl', kw))
        except Exception: pass

    def close(self):
        try:
            with self._lock: self.conn.send(None)
        except Exception: pass
        self.proc.join(3)
        if self.proc.is_alive(): self.proc.terminate()
