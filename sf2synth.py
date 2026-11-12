"""Small SoundFont 2 (.sf2) synthesizer: renders a Standard MIDI File to 16-bit stereo PCM with numpy.

Supports what karaoke MIDI files need: presets by bank/program (bank 128 = drums on channel 10), key/velocity
zones, sample loops, root key / tuning / scale tuning, attenuation, pan, a volume envelope (delay, attack, hold,
decay, sustain, release), channel volume / expression / pan, pitch bend (taken at note start), sustain pedal.
Not modelled: filters, modulation LFOs, chorus/reverb (CC91/93 are ignored), dynamic pitch bends.
"""
import struct, math
import numpy as np

# ------------------------------------------------------------------------------------------------ SF2 parsing
GEN = dict(startAddrsOffset=0, endAddrsOffset=1, startloopAddrsOffset=2, endloopAddrsOffset=3,
           startAddrsCoarseOffset=4, pan=17, delayVolEnv=33, attackVolEnv=34, holdVolEnv=35, decayVolEnv=36,
           sustainVolEnv=37, releaseVolEnv=38, instrument=41, keyRange=43, velRange=44,
           endAddrsCoarseOffset=12, startloopAddrsCoarseOffset=45, initialAttenuation=48,
           endloopAddrsCoarseOffset=50, coarseTune=51, fineTune=52, sampleID=53, sampleModes=54,
           scaleTuning=56, exclusiveClass=57, overridingRootKey=58,
           modLfoToPitch=5, vibLfoToPitch=6, modEnvToPitch=7, initialFilterFc=8, initialFilterQ=9,
           modLfoToFilterFc=10, modEnvToFilterFc=11, modLfoToVolume=13, delayModLFO=21, freqModLFO=22,
           delayVibLFO=23, freqVibLFO=24, delayModEnv=25, attackModEnv=26, holdModEnv=27, decayModEnv=28,
           sustainModEnv=29, releaseModEnv=30)
G = {v: k for k, v in GEN.items()}
DEFAULTS = dict(delayVolEnv=-12000, attackVolEnv=-12000, holdVolEnv=-12000, decayVolEnv=-12000,
                sustainVolEnv=0, releaseVolEnv=-12000, scaleTuning=100, overridingRootKey=-1,
                initialFilterFc=13500, delayModLFO=-12000, delayVibLFO=-12000, delayModEnv=-12000,
                attackModEnv=-12000, holdModEnv=-12000, decayModEnv=-12000, releaseModEnv=-12000)


def _chunks(b, pos, end):
    while pos + 8 <= end:
        cid = b[pos:pos + 4]; ln = struct.unpack('<I', b[pos + 4:pos + 8])[0]
        yield cid, pos + 8, ln
        pos += 8 + ln + (ln & 1)


class SoundFont:
    def __init__(self, path):
        b = open(path, 'rb').read()
        if b[:4] != b'RIFF' or b[8:12] != b'sfbk': raise ValueError('not a SoundFont 2 file')
        self.name = path; pd = {}; smpl = None
        for cid, p, ln in _chunks(b, 12, len(b)):
            if cid == b'LIST':
                kind = b[p:p + 4]
                for sid, sp, sln in _chunks(b, p + 4, p + ln):
                    if kind == b'sdta' and sid == b'smpl': smpl = (sp, sln)
                    if kind == b'pdta': pd[sid.decode()] = b[sp:sp + sln]
                    if kind == b'INFO' and sid == b'INAM': self.name = b[sp:sp + sln].split(b'\0')[0].decode('latin1')
        self.samples = np.frombuffer(b, dtype='<i2', count=smpl[1] // 2, offset=smpl[0]).astype(np.float32) / 32768.0
        rec = lambda name, size: [pd[name][i:i + size] for i in range(0, len(pd[name]) - size + 1, size)]
        self.phdr = [(r[:20].split(b'\0')[0].decode('latin1'),) + struct.unpack('<HHH', r[20:26]) for r in rec('phdr', 38)]
        self.pbag = [struct.unpack('<HH', r) for r in rec('pbag', 4)]
        self.pgen = [struct.unpack('<Hh', r) for r in rec('pgen', 4)]
        self.inst = [(r[:20].split(b'\0')[0].decode('latin1'), struct.unpack('<H', r[20:22])[0]) for r in rec('inst', 22)]
        self.ibag = [struct.unpack('<HH', r) for r in rec('ibag', 4)]
        self.igen = [struct.unpack('<Hh', r) for r in rec('igen', 4)]
        self.shdr = []
        for r in rec('shdr', 46):
            start, end, ls, le, rate = struct.unpack('<IIIII', r[20:40])
            self.shdr.append(dict(name=r[:20].split(b'\0')[0].decode('latin1'), start=start, end=end, ls=ls, le=le,
                                  rate=rate, key=r[40], corr=struct.unpack('<b', r[41:42])[0]))
        self.presets = {}
        for i, (name, prog, bank, bag) in enumerate(self.phdr[:-1]):
            self.presets[(bank, prog)] = i
        self._zone_cache = {}

    @staticmethod
    def _gens(genlist, a, b):
        d = {}
        for op, amt in genlist[a:b]:
            if op in (43, 44): d[op] = (amt & 0xFF, (amt >> 8) & 0xFF)
            else: d[op] = amt
        return d

    def _zones(self, bagtab, gentab, first, last, term):
        zones = []; glob = {}
        for zi in range(first, last):
            g = self._gens(gentab, bagtab[zi][0], bagtab[zi + 1][0])
            if term not in g:
                if zi == first: glob = g                       # global zone
                continue
            zones.append(g)
        return glob, zones

    def regions(self, bank, prog):
        """List of note regions for a preset: dicts with key/vel ranges, sample and merged generators."""
        k = (bank, prog)
        if k in self._zone_cache: return self._zone_cache[k]
        pi = self.presets.get(k)
        if pi is None:
            pi = self.presets.get((128, 0) if bank == 128 else (0, prog), self.presets.get((0, 0)))
        if pi is None: self._zone_cache[k] = []; return []
        pglob, pzones = self._zones(self.pbag, self.pgen, self.phdr[pi][3], self.phdr[pi + 1][3], 41)
        out = []
        for pz in pzones:
            pg = dict(pglob); pg.update(pz)
            ii = pg[41]
            iglob, izones = self._zones(self.ibag, self.igen, self.inst[ii][1], self.inst[ii + 1][1], 53)
            for iz in izones:
                ig = dict(iglob); ig.update(iz)
                kr = _isect(pg.get(43, (0, 127)), ig.get(43, (0, 127))); vr = _isect(pg.get(44, (0, 127)), ig.get(44, (0, 127)))
                if kr is None or vr is None: continue
                merged = {}
                for op, v in ig.items():
                    if op not in (43, 44, 53): merged[op] = v
                for op, v in pg.items():                        # preset generators add to instrument ones
                    if op in (41, 43, 44, 53, 0, 1, 2, 3, 4, 12, 45, 50, 54, 57, 58): continue
                    merged[op] = merged.get(op, _default(op)) + v
                out.append(dict(kr=kr, vr=vr, s=self.shdr[ig[53]], g=merged))
        self._zone_cache[k] = out
        return out


def _isect(a, b):
    lo, hi = max(a[0], b[0]), min(a[1], b[1])
    return (lo, hi) if lo <= hi else None


def _default(op):
    return DEFAULTS.get(G.get(op, ''), 0)


def _tc(v):                                   # timecents -> seconds
    return 0.0 if v <= -12000 else 2.0 ** (v / 1200.0)


# ------------------------------------------------------------------------------------------------ MIDI parsing
def read_midi(data):
    """Returns (events, ticks_per_beat): events = sorted list of (tick, order, channel, kind, a, b)."""
    if data[:4] != b'MThd': raise ValueError('not a MIDI file')
    hl = struct.unpack('>I', data[4:8])[0]
    fmt, ntrk, div = struct.unpack('>HHH', data[8:14])
    p = 8 + hl; ev = []; order = 0
    for _ in range(ntrk):
        if data[p:p + 4] != b'MTrk': break
        ln = struct.unpack('>I', data[p + 4:p + 8])[0]; t = data[p + 8:p + 8 + ln]; p += 8 + ln
        i = 0; tick = 0; run = None
        def vlq():
            nonlocal i
            v = 0
            while True:
                c = t[i]; i += 1; v = (v << 7) | (c & 0x7F)
                if not c & 0x80: return v
        try:
            while i < len(t):
                tick += vlq(); s = t[i]
                if s & 0x80: i += 1
                else: s = run
                if s == 0xFF:
                    typ = t[i]; i += 1; n = vlq(); d = t[i:i + n]; i += n
                    if typ == 0x51: ev.append((tick, order, -1, 'tempo', int.from_bytes(d, 'big'), 0))
                    elif typ == 0x2F: break
                elif s in (0xF0, 0xF7):
                    n = vlq(); i += n
                else:
                    run = s; hi = s & 0xF0; ch = s & 15
                    if hi in (0xC0, 0xD0): a = t[i]; b = 0; i += 1
                    else: a, b = t[i], t[i + 1]; i += 2
                    ev.append((tick, order, ch, hi, a, b))
                order += 1
        except IndexError:
            pass
    ev.sort(key=lambda x: (x[0], x[1]))
    return ev, div


# ------------------------------------------------------------------------------------------------ rendering
def render(midi_bytes, sf, rate=32000, max_seconds=600, gain=0.35, progress=None):
    """Render a MIDI file with SoundFont sf. Returns int16 stereo numpy array (n, 2)."""
    events, div = read_midi(midi_bytes)
    # tick -> seconds
    tempo = 500000; last_tick = 0; last_sec = 0.0; times = []
    for e in events:
        sec = last_sec + (e[0] - last_tick) * tempo / 1e6 / div
        times.append(sec); last_tick, last_sec = e[0], sec
        if e[3] == 'tempo': tempo = e[4]
    total = min((times[-1] if times else 0) + 3.0, max_seconds)
    out = np.zeros((int(total * rate) + rate, 2), np.float32)
    ch = [dict(prog=0, bank=128 if c == 9 else 0, vol=100, expr=127, pan=64, bend=0, sustain=False) for c in range(16)]
    active = {}                     # (channel, key) -> list of note dicts
    held = []
    notes = []

    def note_off(n, t):
        if n.get('off') is None: n['off'] = t

    for e, sec in zip(events, times):
        if sec >= max_seconds: break
        _, _, c, kind, a, b = e
        if kind == 'tempo': continue
        C = ch[c]
        if kind == 0x90 and b > 0:
            for n in active.pop((c, a), []): note_off(n, sec)
            regs = [r for r in sf.regions(C['bank'], C['prog']) if r['kr'][0] <= a <= r['kr'][1] and r['vr'][0] <= b <= r['vr'][1]]
            lst = []
            for r in regs:
                n = dict(t=sec, off=None, key=a, vel=b, r=r, ch=c, vol=C['vol'], expr=C['expr'], pan=C['pan'], bend=C['bend'])
                notes.append(n); lst.append(n)
            active[(c, a)] = lst
        elif kind == 0x80 or (kind == 0x90 and b == 0):
            for n in active.pop((c, a), []):
                if C['sustain']: held.append(n)
                else: note_off(n, sec)
        elif kind == 0xB0:
            if a == 0: C['bank'] = 128 if c == 9 else b
            elif a == 7: C['vol'] = b
            elif a == 11: C['expr'] = b
            elif a == 10: C['pan'] = b
            elif a == 64:
                C['sustain'] = b >= 64
                if not C['sustain']:
                    for n in [h for h in held if h['ch'] == c]: note_off(n, sec)
                    held[:] = [h for h in held if h['ch'] != c]
            elif a in (120, 123):
                for k in [k for k in active if k[0] == c]:
                    for n in active.pop(k): note_off(n, sec)
        elif kind == 0xC0:
            C['prog'] = a
        elif kind == 0xE0:
            C['bend'] = ((b << 7) | a) - 8192

    smp = sf.samples
    for idx, n in enumerate(notes):
        if progress and idx % 200 == 0: progress(idx / max(1, len(notes)))
        r = n['r']; s = r['s']; g = r['g']
        gv = lambda name: g.get(GEN[name], _default(GEN[name]))
        start = s['start'] + gv('startAddrsOffset') + 32768 * gv('startAddrsCoarseOffset')
        end = s['end'] + gv('endAddrsOffset') + 32768 * gv('endAddrsCoarseOffset')
        ls = s['ls'] + gv('startloopAddrsOffset') + 32768 * gv('startloopAddrsCoarseOffset')
        le = s['le'] + gv('endloopAddrsOffset') + 32768 * gv('endloopAddrsCoarseOffset')
        if end <= start + 1: continue
        mode = gv('sampleModes') & 3
        root = gv('overridingRootKey');  root = s['key'] if root < 0 else root
        cents = (n['key'] - root) * gv('scaleTuning') + gv('coarseTune') * 100 + gv('fineTune') + s['corr'] + n['bend'] / 8192 * 200
        step = s['rate'] / rate * 2.0 ** (cents / 1200.0)
        # envelope (seconds)
        dly, att, hold, dec = _tc(gv('delayVolEnv')), _tc(gv('attackVolEnv')), _tc(gv('holdVolEnv')), _tc(gv('decayVolEnv'))
        sus = 10 ** (-min(max(gv('sustainVolEnv'), 0), 1440) / 200.0)
        rel = max(_tc(gv('releaseVolEnv')), 0.01)
        off = n['off'] if n['off'] is not None else total - 1
        dur_on = max(off - n['t'], 0.005)
        looped = mode in (1, 3) and le > ls + 2
        if not looped: dur_on = min(dur_on, (end - start) / step / rate)
        length = int((dur_on + rel) * rate)
        if length <= 0: continue
        pos = start + np.arange(length, dtype=np.float64) * step
        if looped:
            over = pos >= le
            pos[over] = ls + np.mod(pos[over] - ls, le - ls)
        else:
            pos = pos[pos < end - 1]; length = len(pos)
            if length == 0: continue
        i0 = pos.astype(np.int64); fr = (pos - i0).astype(np.float32)
        wave = smp[i0] * (1 - fr) + smp[np.minimum(i0 + 1, len(smp) - 1)] * fr
        t = np.arange(length, dtype=np.float32) / rate
        env = np.ones(length, np.float32)
        a0 = dly; a1 = a0 + att; h1 = a1 + hold; d1 = h1 + dec
        env[t < a0] = 0
        m = (t >= a0) & (t < a1)
        if att > 0: env[m] = (t[m] - a0) / att
        m = (t >= h1) & (t < d1)
        if dec > 0: env[m] = 1 - (1 - sus) * (t[m] - h1) / dec
        env[t >= d1] = sus
        relm = t >= dur_on
        if relm.any():
            lvl = env[min(int(dur_on * rate), length - 1)] if int(dur_on * rate) < length else sus
            env[relm] = lvl * np.maximum(0, 1 - (t[relm] - dur_on) / rel)
        atten = 10 ** (-(gv('initialAttenuation') * 0.4) / 200.0)          # common 0.4 dB/cB practice
        amp = atten * (n['vel'] / 127.0) ** 2 * (n['vol'] / 127.0) ** 2 * (n['expr'] / 127.0) ** 2
        pan = min(max((n['pan'] - 64) / 64.0 + gv('pan') / 500.0, -1), 1)
        lg = math.cos((pan + 1) * math.pi / 4); rg = math.sin((pan + 1) * math.pi / 4)
        sig = wave * env * amp
        st = int(n['t'] * rate); e2 = min(st + length, len(out))
        if e2 <= st: continue
        out[st:e2, 0] += sig[:e2 - st] * lg
        out[st:e2, 1] += sig[:e2 - st] * rg
    peak = float(np.max(np.abs(out))) or 1.0
    out *= min(gain * 4, 0.95 / peak) if peak > 0.95 / (gain * 4) else gain * 4
    n_end = len(out)
    while n_end > rate and not np.any(np.abs(out[n_end - rate:n_end]) > 1e-4): n_end -= rate
    return (np.clip(out[:n_end], -1, 1) * 32767).astype(np.int16)


def wav_bytes(pcm, rate=32000):
    data = pcm.tobytes()
    return (b'RIFF' + struct.pack('<I', 36 + len(data)) + b'WAVEfmt ' +
            struct.pack('<IHHIIHH', 16, 1, 2, rate, rate * 4, 4, 16) + b'data' + struct.pack('<I', len(data)) + data)


def midi_info(midi_bytes):
    """Title/artist/lyrics text from the karaoke MIDI ('@Title$3Artist' style text events)."""
    events_text = []
    data = midi_bytes
    hl = struct.unpack('>I', data[4:8])[0]; ntrk = struct.unpack('>H', data[10:12])[0]; p = 8 + hl
    for _ in range(ntrk):
        if data[p:p + 4] != b'MTrk': break
        ln = struct.unpack('>I', data[p + 4:p + 8])[0]; t = data[p + 8:p + 8 + ln]; p += 8 + ln
        i = 0; run = None
        try:
            while i < len(t):
                while t[i] & 0x80: i += 1
                i += 1
                s = t[i]
                if s & 0x80: i += 1
                else: s = run
                if s == 0xFF:
                    typ = t[i]; i += 1; n = 0
                    while True:
                        c = t[i]; i += 1; n = (n << 7) | (c & 0x7F)
                        if not c & 0x80: break
                    if typ in (1, 3, 5): events_text.append((typ, t[i:i + n].decode('latin1', 'replace')))
                    i += n
                    if typ == 0x2F: break
                elif s in (0xF0, 0xF7):
                    n = 0
                    while True:
                        c = t[i]; i += 1; n = (n << 7) | (c & 0x7F)
                        if not c & 0x80: break
                    i += n
                else:
                    run = s; i += 1 if (s & 0xF0) in (0xC0, 0xD0) else 2
        except IndexError:
            pass
    title = artist = ''
    for typ, s in events_text:
        if s.startswith('@'):
            body = s[1:]
            if '$' in body: title, rest = body.split('$', 1); artist = rest[1:] if rest[:1].isdigit() else rest
            else: title = body
            break
    lyrics = ''.join(s for typ, s in events_text if typ == 5 or (typ == 1 and not s.startswith('@')))
    return dict(title=title.strip(), artist=artist.strip(), lyrics=lyrics)
