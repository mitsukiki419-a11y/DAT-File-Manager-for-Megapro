"""Lyrics for music videos: turn a karaoke MIDI (a Megapro song MIDI, a .kar file, or any MIDI with lyric/text
events) into the lyrics MIDI stored in a music video's song record.

Megapro lyrics, as in the disc's song MIDIs: a track named '@Title$3Artist' holding lyric meta events (FF 05), one
per syllable ('Ba ', 'yang^ ', ...); an event '\\r' ends a line. .kar files use text events (FF 01) where a leading
'/' starts a new line and '\\' a new verse; those are converted.

The result is rebuilt on a fixed clock (480 ticks per beat at 120 bpm, so 960 ticks = 1 s), which makes shifting the
lyrics against the video simple. With keep_music=False (the default) only the lyrics are kept, because the video
has its own sound.
"""
import struct

DIV = 480
TPS = 960          # ticks per second at 500000 us per beat


def _vlq(v):
    b = [v & 0x7F]; v >>= 7
    while v: b.append(0x80 | (v & 0x7F)); v >>= 7
    return bytes(reversed(b))


def read(data):
    """All timed events of a MIDI file: [(seconds, kind, data)] with kind 'msg' (channel message or SysEx bytes),
    'lyric' (FF 05), 'text' (FF 01), 'name' (FF 03, with the track index), plus the length in seconds."""
    if data[:4] != b'MThd': raise ValueError('not a MIDI file')
    hl = struct.unpack('>I', data[4:8])[0]; fmt, ntrk, div = struct.unpack('>HHH', data[8:14])
    if div & 0x8000: raise ValueError('SMPTE-timed MIDI files are not supported')
    p = 8 + hl; raw = []; order = 0
    for tn in range(ntrk):
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
                    elif typ == 0x05: raw.append((tick, order, 'lyric', bytes(d)))
                    elif typ == 0x01: raw.append((tick, order, 'text', bytes(d)))
                    elif typ == 0x03: raw.append((tick, order, 'name', (tn, bytes(d))))
                    elif typ == 0x2F: break
                elif s in (0xF0, 0xF7):
                    n = vlq(); d = t[i:i + n]; i += n
                    raw.append((tick, order, 'msg', bytes([0xF0]) + d if s == 0xF0 else d))
                else:
                    run = s; nb = 1 if (s & 0xF0) in (0xC0, 0xD0) else 2
                    raw.append((tick, order, 'msg', bytes([s]) + t[i:i + nb])); i += nb
                order += 1
        except IndexError:
            pass
    raw.sort(key=lambda x: (x[0], x[1]))
    tempo = 500000; last = 0; sec = 0.0; out = []
    for tick, _, kind, v in raw:
        sec += (tick - last) * tempo / div / 1e6; last = tick
        if kind == 'tempo': tempo = v
        else: out.append((sec, kind, v))
    return out, sec


def lyrics_of(events):
    """[(seconds, syllable bytes)] in Megapro style ('\\r' = end of line), from lyric events, or else from .kar text
    events. Title/info texts ('@...', '%...', 'K...' headers) are skipped."""
    lyr = [(s, d) for s, k, d in events if k == 'lyric']
    if any(d.strip(b' \r\n') for _, d in lyr):
        out = []
        for s, d in lyr:
            if d in (b'\r', b'\n', b'\r\n'): out.append((s, b'\r'))
            elif d.startswith((b'\r', b'\n')): out += [(s, b'\r'), (s, d.lstrip(b'\r\n'))]
            elif d.endswith((b'\r', b'\n')): out += [(s, d.rstrip(b'\r\n')), (s, b'\r')]
            else: out.append((s, d))
        return out
    out = []; started = False
    for s, k, d in events:
        if k != 'text' or not d: continue
        if d[:1] in (b'@', b'%') or (not started and d[:1] == b'K' and len(d) > 8): continue   # .kar header texts
        started = True
        if d[:1] in (b'/', b'\\'):
            if out: out.append((s, b'\r'))
            d = d[1:]
        if d: out.append((s, d))
    return out


def title_of(events):
    """(title, artist) from the '@Title$1$2$3Artist$TMTV' track name of Megapro MIDIs, or a .kar '@T' text."""
    for s_, k, d in events:
        if k == 'name' and d[1].startswith(b'@'):
            parts = d[1][1:].decode('latin1', 'replace').split('$')
            artist = next((x[1:] for x in parts[1:] if x[:1] == '3'), '')
            return parts[0].strip(), artist.strip()
    for s_, k, d in events:
        if k == 'text' and d.startswith(b'@T'): return d[2:].decode('latin1', 'replace').strip(), ''
    return '', ''


def lines_of(events):
    """Lyric lines [[(seconds, syllable text in Megapro style)]]: each syllable ends in a space; '^' marks the end of
    a word inside a line (the player shows it as a space). Megapro MIDIs keep their own syllables; .kar and other
    lyrics are converted (word ends come from the spaces around the syllables)."""
    lyr = lyrics_of(events)
    megapro = any(b'^' in d for _, d in lyr)
    lines = []; cur = []
    for t, d in lyr:
        if d == b'\r':
            if cur: lines.append(cur); cur = []
            continue
        if not d.strip(): continue
        cur.append((t, d))
    if cur: lines.append(cur)
    out = []
    for line in lines:
        conv = []
        for i, (t, d) in enumerate(line):
            txt = d.decode('latin1', 'replace')
            if megapro:
                syl = txt if txt.endswith(' ') else txt + ' '
            else:
                last = i == len(line) - 1
                nxt = line[i + 1][1].decode('latin1', 'replace') if not last else ''
                word_end = txt.endswith(' ') or nxt.startswith(' ')
                syl = txt.strip() + ('^ ' if word_end and not last else ' ')
            if i == len(line) - 1 and not megapro: syl = syl.replace('^ ', ' ')
            conv.append((t, syl))
        out.append(conv)
    return out


def text_of(lines):
    return [''.join(syl[:-1].replace('^', ' ') for _, syl in line).strip() for line in lines]


SECTION_GAP = 18.0          # a pause longer than this between two syllables starts a new lyric section (as on the disc)


def build(midi, title, artist, offset=0.0, keep_music=False):
    """The lyrics MIDI of a music video, in the layout of the disc's own music videos (MPPV45):
      format 1, 120 ticks per beat, 120 bpm (240 ticks = 1 s); track 0: port, time and key signature, tempo;
      track 1: port, name '@Title$1$2$3Artist$TMTV', channel-16 controllers 71=2, 72=13, 70=1, then per syllable a
      lyric event (FF 05) and a silent marker note (channel 16, note 64, velocity 1, one tick long) that drives the
      wipe; a line ends with the lyric events '' and '\\r'. Controller 20 steers the screen: 20 and 4 announce a
      section, four 0s half a second apart count in to its first syllable, 10 ends a section (2 s after its last
      syllable, before a pause of over 18 s) and 30 ends the song (3 s after the last syllable).
    offset (seconds): positive = lyrics later, negative = earlier. keep_music is ignored (the disc's music videos
    carry no music)."""
    events, length = read(midi)
    lines = [[(t + offset, syl) for t, syl in line if t + offset >= 0] for line in lines_of(events)]
    return build_lines(lines, title, artist)


def build_lines(lines, title, artist):
    """The lyrics MIDI from timed lines [[(seconds, syllable)]] (syllables end in ' ', '^ ' inside a line)."""
    lines = [l for l in lines if l]
    if not lines: raise ValueError('no lyrics')
    T = lambda sec: max(0, int(round(sec * 240)))
    ev = []                                            # (tick, order, bytes)
    def add(sec, data, order=1): ev.append((T(sec), order, data))
    def cc(sec, v): add(sec, b'\xbf\x14' + bytes([v]), 0)
    def lyric(sec, text): add(sec, b'\xff\x05' + _vlq(len(text)) + text)
    syls = [(t, syl, i == len(line) - 1) for line in lines for i, (t, syl) in enumerate(line)]
    syls[-1] = (syls[-1][0], syls[-1][1], False)          # the disc's MIDIs end without a line-end mark
    # sections
    starts = [0]
    for k in range(1, len(syls)):
        if syls[k][0] - syls[k - 1][0] > SECTION_GAP: starts.append(k)
    prev_end = 0.0
    for si, k in enumerate(starts):
        t0 = syls[k][0]; c0 = t0 - 2.0
        span = min(12.8, max(0.4, c0 - prev_end - 0.5))
        if c0 < prev_end + 0.2:                      # no room for the count-in (lyrics right at the start): only 20
            cc((prev_end + t0) / 2, 20)
        else:
            for v, at in ((20, c0 - span), (4, c0 - span / 2), (0, c0), (0, c0 + 0.5), (0, c0 + 1.0), (0, c0 + 1.5)):
                if at >= prev_end: cc(at, v)
        if si + 1 < len(starts):
            prev_end = syls[starts[si + 1] - 1][0] + 2.0; cc(prev_end, 10)
    for t, syl, eol in syls:
        lyric(t, syl.encode('latin1', 'replace'))
        if eol: lyric(t, b''); lyric(t, b'\r')
        ev.append((T(t), 2, b'\x9f\x40\x01')); ev.append((T(t) + 1, 0, b'\x9f\x40\x00'))
    end = syls[-1][0] + 3.0; cc(end, 30)
    ev.sort(key=lambda x: (x[0], x[1]))
    name = ('@' + (title or '') + '$1$2$3' + (artist or '') + '$TMTV').encode('latin1', 'replace')
    body = bytearray(b'\x00\xff\x21\x01\x00\x00\xff\x03' + _vlq(len(name)) + name +
                     b'\x01\xbf\x47\x02\x01\xbf\x48\x0d\x01\xbf\x46\x01')
    last = 3
    for tick, _, data in ev:
        tick = max(tick, last); body += _vlq(tick - last) + data; last = tick
    body += b'\x00\xff\x2f\x00'
    t0 = (b'\x00\xff\x21\x01\x00\x00\xff\x58\x04\x04\x02\x18\x08\x00\xff\x59\x02\x00\x00'
          b'\x00\xff\x51\x03\x07\xa1\x20\x00\xff\x06\x08untitled\x00\xff\x2f\x00')
    trk = lambda b: b'MTrk' + struct.pack('>I', len(b)) + bytes(b)
    return b'MThd' + struct.pack('>IHHH', 6, 1, 2, 120) + trk(t0) + trk(body)


def info(midi):
    """dict(syllables, lines, title, artist, length, first lyric time, notes, preview) for the dialogs."""
    events, length = read(midi)
    lines = lines_of(events); t, a = title_of(events)
    text = text_of(lines)
    return dict(syllables=sum(len(l) for l in lines), lines=len(lines), title=t, artist=a, length=round(length, 1),
                first=round(lines[0][0][0], 1) if lines else None,
                notes=sum(1 for _, k, d in events if k == 'msg' and d[0] & 0xF0 == 0x90 and len(d) > 2 and d[2] > 1),
                preview=' / '.join(text)[:160])


def text_lines(text):
    """The lyric lines of pasted text: blank lines dropped, spaces tidied."""
    return [' '.join(l.split()) for l in (text or '').splitlines() if l.strip()]


def from_text(text, times, end=None, title='', artist=''):
    """Lyrics MIDI from plain text and the start time (seconds) of each line, e.g. tapped while the video plays.
    Each line's words are wiped one after another from its start: about 0.45 s a word, squeezed so the line is
    done before the next one starts. end: when the last line ends (default: 0.45 s a word)."""
    lines = text_lines(text)
    if not lines: raise ValueError('no lyrics text')
    times = [float(t) for t in times][:len(lines)]
    if len(times) < len(lines): raise ValueError('%d lines but only %d times' % (len(lines), len(times)))
    if any(b <= a for a, b in zip(times, times[1:])): raise ValueError('line times must go up')
    out = []
    for i, line in enumerate(lines):
        words = line.split(' ')
        nxt = times[i + 1] if i + 1 < len(lines) else (end if end else times[i] + 0.45 * len(words) + 0.5)
        step = min(0.45, 0.85 * (nxt - times[i]) / len(words))
        out.append([(times[i] + k * step, w + (' ' if k == len(words) - 1 else '^ ')) for k, w in enumerate(words)])
    return build_lines(out, title, artist)


def even_times(text, start, end):
    """Line start times spread between start and end in proportion to each line's word count."""
    lines = text_lines(text)
    if not lines: return []
    w = [len(l.split(' ')) for l in lines]; tot = sum(w); t = float(start); out = []
    for n in w:
        out.append(round(t, 2)); t += (float(end) - float(start)) * n / tot
    return out
