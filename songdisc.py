"""Megapro karaoke disc song manager: read the song list of a disc (ISO image or folder), extract songs (MIDI and the
recorded chorus/vocal clips), add and delete songs, and write a new disc (ISO or folder). The original is never
modified.

Disc layout (KARA125/, reverse-engineered from the MP100NSP firmware, see PHASE19_NOTES.md):
  title.hdr  one sector: 16-byte header (word3 = key seed), then a 2048-byte contents table compressed with LZSS
             and word-scrambled (key = word3 ^ 0x47E39035, steps = key % 15 + 107). The table lists the sections of
             title.bnk as 32-byte entries [type, param, offset, size, a, b, c, hash].
  title.bnk  sections; type 5 = 'ML0' song list: header [ML0, 1, n, 8+3n, 0...], then three parallel lists of n
             words: number (bits 0-19) | flags (bit 23 = has recorded audio, 0x06000000 always set),
             location = bank file index << 28 | record offset >> 4 inside that file (0 = AUDIO.DAT,
             1 = AUDIO1.DA1, 2 = AUDIO2.DA2 ...), string offset (title\\0artist\\0) inside the section.
  AUDIO.DAT, AUDIO1.DA1, AUDIO2.DA2 ...  the song bank files. Each
             record: header [data offset, record size, MIDI size, n parts, compressed size L, 0 x5, parts n x
             (offset, size, flags)], MIDI = LZSS + word scramble at data offset + (3n+10)*4; parts = WMA clips with a
             per-sector word scramble; the first (3n+10)*4 bytes of the record moved to the data offset.
"""
import os, struct, shutil, re, subprocess, io, threading
import karamidi, isoread, mvlyrics

SECTOR = 2048
HDR_KEY_XOR = 0x47E39035
FLAG_BASE = 0x06000000
FLAG_AUDIO = 0x00800000
PAD_SECTORS = 1024            # 2 MB of zeros after appended data (see _write_iso)
MAX_NUMBER = 99999            # the firmware's number entry (0x080B26D4) resets anything above 99999


class DiscWriteError(Exception):
    pass


# ----------------------------------------------------------------------------------------------- title.hdr
def decode_hdr(raw):
    w = struct.unpack('<4I', raw[:16])
    key = w[3] ^ HDR_KEY_XOR
    count = _smod(key, 15) + 107
    table = karamidi.lzss_decode(karamidi.unscramble_kc(raw[16:], key, count, 2048), 2048)
    if len(table) != 2048: raise ValueError('title.hdr did not decode')
    return list(w), table


def encode_hdr(words, table):
    table = bytearray(table)
    _fix_hdr_checksum(table)
    key = words[3] ^ HDR_KEY_XOR
    count = _smod(key, 15) + 107
    comp = karamidi.lzss_encode(bytes(table))
    body = bytearray(comp) + bytes((-len(comp)) % 4)
    if len(body) < 2048: body += bytes(2048 - len(body))       # the scramble covers 2048 bytes (512 words)
    body = karamidi.scramble_kc(bytes(body), key, count, 2048)
    out = struct.pack('<4I', *words) + body
    if len(out) > SECTOR + 2048: raise DiscWriteError('title.hdr too large')
    check = karamidi.lzss_decode(karamidi.unscramble_kc(out[16:], key, count, 2048), 2048)
    if check != bytes(table): raise DiscWriteError('title.hdr re-encoding check failed')
    return out[:max(SECTOR, (len(comp) + 16 + 3) // 4 * 4)] if len(out) > SECTOR else out[:SECTOR]


def _fix_hdr_checksum(table):
    w = list(struct.unpack_from('<33I', table, 0)); w[1] = 0
    x = 0x44444444
    for v in w: x ^= v
    struct.pack_into('<I', table, 4, x)


def _smod(a, b):
    a = a - (1 << 32) if a & 0x80000000 else a
    r = abs(a) % b
    return -r if a < 0 else r


def hdr_entries(table):
    """[(index_in_table, type, param, offset, size, a, b, c, hash)] from the contents table."""
    out = []
    p = 0x88
    while p + 32 <= len(table):
        e = struct.unpack_from('<8I', table, p)
        if e == (0,) * 8: break
        out.append((p,) + e); p += 32
        if e[0] == 13: break                                     # end marker
    return out


def section_hash(data):
    """Hash stored with each title.bnk section in the contents table. Its algorithm is not known; the firmware does
    not check it (tested: a modified ML0 with the old hash loads and is used), so the old value is kept."""
    return None


# ----------------------------------------------------------------------------------------------- ML0
class Song:
    __slots__ = ('number', 'flags', 'bank', 'offset', 'loc_flags', 'title', 'artist', 'midi', 'parts', 'origin', 'deleted',
                 'kind', 'mvv', 'mvv_size', 'mvv_src', 'remove_file', 'converted', 'new_lyrics', 'lyr', 'new_chorus', 'chorus_at')

    def __init__(self, number, flags, bank, offset, title, artist, origin='disc', loc_flags=0):
        self.number, self.flags, self.bank, self.offset, self.title, self.artist = number, flags, bank, offset, title, artist
        self.loc_flags = loc_flags; self.kind = 0; self.mvv = None; self.mvv_size = 0; self.mvv_src = None; self.remove_file = True; self.converted = False
        self.new_lyrics = None; self.lyr = False
        self.new_chorus = None; self.chorus_at = None           # None = unchanged, b'' = removed, bytes = new WMA
        self.midi = None; self.parts = None; self.origin = origin; self.deleted = False

    @property
    def has_audio(self): return bool(self.flags & FLAG_AUDIO)

    @property
    def is_mv(self): return self.kind != 0

    def as_dict(self):
        chorus = bool(self.new_chorus) if self.new_chorus is not None else self.has_audio
        return dict(number=self.number, title=self.title, artist=self.artist, chorus=chorus,
                    chorus_changed=self.new_chorus is not None, origin=self.origin, deleted=self.deleted, mv=self.is_mv, mvv=self.mvv, mvv_size=self.mvv_size,
                    lyrics=self.lyr, lyrics_changed=self.new_lyrics is not None)


def parse_ml0(sec):
    if sec[:4] != b'ML0\x00': raise ValueError('not an ML0 song list')
    n = struct.unpack_from('<I', sec, 8)[0]
    L1 = struct.unpack_from('<%dI' % n, sec, 32)
    L2 = struct.unpack_from('<%dI' % n, sec, 32 + 4 * n)
    L3 = struct.unpack_from('<%dI' % n, sec, 32 + 8 * n)
    songs = []
    for a, b, c in zip(L1, L2, L3):
        e1 = sec.index(b'\0', c); t = sec[c:e1]
        e2 = sec.index(b'\0', e1 + 1); ar = sec[e1 + 1:e2]
        s = Song(a & 0x1FFFF, a & ~0xFFFFF, (b >> 28) & 7, (b & 0x0FFFFFFF) << 4, t.decode('latin1'), ar.decode('latin1'),
                 loc_flags=b & 0x80000000)
        s.kind = (a >> 17) & 7                     # 0 = karaoke song; 3 / 7 = music video KARA125/MVV/<number>.mvv
        songs.append(s)
    return songs


def build_ml0(songs):
    """songs in list order (disc songs keep their original order; the firmware searches the list linearly)."""
    n = len(songs)
    strings = bytearray(); offs = []
    base = 32 + 12 * n
    for s in songs:
        offs.append(base + len(strings))
        strings += _enc(s.title) + b'\0' + _enc(s.artist) + b'\0'
    head = struct.pack('<4s3I4I', b'ML0\0', 1, n, 8 + 3 * n, 0, 0, 0, 0)
    L1 = struct.pack('<%dI' % n, *[(s.flags & ~0xFFFFF) | (s.kind << 17) | s.number for s in songs])
    L2 = struct.pack('<%dI' % n, *[s.loc_flags | (s.bank << 28) | ((s.offset >> 4) & 0x0FFFFFFF) for s in songs])
    L3 = struct.pack('<%dI' % n, *offs)
    return head + L1 + L2 + L3 + bytes(strings)


def _enc(text):
    t = (text or '').replace('\0', ' ')
    return t.encode('latin1', 'replace')[:200]


# ----------------------------------------------------------------------------------------------- records
def make_record(midi, parts=None):
    """A song record: header, LZSS + scramble, padded to whole sectors. Without parts it is MIDI-only (like the
    ones in AUDIO1.DA1). parts = recorded audio clips (WMA/ASF files, the "chorus"), laid out as on the disc
    (AUDIO.DAT): each part scrambled per sector and sector-aligned from the start of the record, the MIDI after them;
    the record header (with the part table) covers the first bytes, which are kept at the MIDI's data offset."""
    if not karamidi.midi_ok(midi): raise ValueError('not a valid Standard MIDI File')
    comp = karamidi.lzss_encode(midi)
    L = len(comp)
    data = karamidi.scramble(comp, L)                         # inverse of the firmware's descramble
    if parts:
        n = len(parts); hdr = (3 * n + 10) * 4
        region = bytearray(); table = []
        for p in parts:
            if p[:16] != karamidi.ASF_HEADER_GUID: raise ValueError('a chorus part must be a WMA (ASF) file')
            if len(p) < hdr + 2048: raise ValueError('chorus clip too short')
            k, c = karamidi.part_key(len(p))
            table.append((len(region), len(p), 0x1000000))
            region += karamidi.scramble_audio(p, k, len(p), c)
            region += bytes(-len(region) % SECTOR)
        off = len(region)
        head = struct.pack('<10I', off, 0, len(midi), n, L, 0, 0, 0, 0, 0) + b''.join(struct.pack('<3I', *t) for t in table)
        body = region + bytes(region[:hdr]) + data
        body = bytearray(body); body[:hdr] = head
        rsize = (len(body) + SECTOR - 1) // SECTOR * SECTOR
        rec = bytearray(body + bytes(rsize - len(body)))
        struct.pack_into('<I', rec, 4, rsize)
        if karamidi.decode_record(bytes(rec)) != midi or [x[3] for x in karamidi.record_parts(bytes(rec))] != list(parts):
            raise DiscWriteError('song record re-encoding check failed')
        return bytes(rec)
    body = struct.pack('<10I', 0, 0, len(midi), 0, L, 0, 0, 0, 0, 0) + data
    rsize = (len(body) + SECTOR - 1) // SECTOR * SECTOR
    rec = bytearray(body + bytes(rsize - len(body)))
    struct.pack_into('<I', rec, 4, rsize)
    if karamidi.decode_record(bytes(rec)) != midi: raise DiscWriteError('song record re-encoding check failed')
    return bytes(rec)


# ----------------------------------------------------------------------------------------------- chorus (recorded voices)
CHORUS_CC = (0x46, 0x48)       # channel 16 CC70 = 1 starts the song's recorded part (after CC72 = 14), as on the disc


def _vlq(v):
    out = bytearray([v & 0x7F]); v >>= 7
    while v: out.insert(0, 0x80 | (v & 0x7F)); v >>= 7
    return bytes(out)


def _midi_tracks(midi):
    """(format, division, [[(abs tick, full event bytes)]]): every event with its status byte written out."""
    if midi[:4] != b'MThd': raise ValueError('not a MIDI file')
    hl = struct.unpack('>I', midi[4:8])[0]; fmt, ntrk, div = struct.unpack('>HHH', midi[8:14])
    p = 8 + hl; tracks = []
    for _ in range(ntrk):
        if midi[p:p + 4] != b'MTrk': break
        ln = struct.unpack('>I', midi[p + 4:p + 8])[0]; t = midi[p + 8:p + 8 + ln]; p += 8 + ln
        i = 0; tick = 0; run = 0; ev = []
        def vlq():
            nonlocal i
            v = 0
            while i < len(t):
                c = t[i]; i += 1; v = (v << 7) | (c & 0x7F)
                if not c & 0x80: break
            return v
        while i < len(t):
            tick += vlq(); st = t[i]
            if st & 0x80: i += 1
            else: st = run
            if st == 0xFF:
                typ = t[i]; i += 1; a = i; n = vlq(); d = t[i:i + n]; i += n
                ev.append((tick, bytes([0xFF, typ]) + _vlq(n) + d))
                if typ == 0x2F: break
            elif st in (0xF0, 0xF7):
                n = vlq(); d = t[i:i + n]; i += n; ev.append((tick, bytes([st]) + _vlq(n) + d))
            else:
                run = st; k = 1 if (st & 0xE0) == 0xC0 else 2
                ev.append((tick, bytes([st]) + t[i:i + k])); i += k
        tracks.append(ev)
    return fmt, div, tracks


def _midi_write(fmt, div, tracks):
    out = bytearray(b'MThd' + struct.pack('>IHHH', 6, 1 if len(tracks) > 1 else fmt, len(tracks), div))
    for ev in tracks:
        body = bytearray(); last = 0
        ev = [e for e in ev if e[1][:2] != b'\xff\x2f']
        for tick, b in ev:
            body += _vlq(max(0, tick - last)) + b; last = max(last, tick)
        body += b'\x00\xff\x2f\x00'
        out += b'MTrk' + struct.pack('>I', len(body)) + body
    return bytes(out)


def _seconds_to_tick(tracks, div, sec):
    tempos = sorted((tk, int.from_bytes(b[3:6], 'big')) for ev in tracks for tk, b in ev if b[:2] == b'\xff\x51')
    tick = 0; t = 0.0; tempo = 500000
    for tk, tp in tempos:
        dt = (tk - tick) * tempo / 1e6 / div
        if t + dt >= sec: break
        t += dt; tick = tk; tempo = tp
    return tick + int(round((sec - t) * 1e6 * div / tempo))


def chorus_trigger(midi):
    """When the song starts its recorded part (seconds), or None."""
    fmt, div, tracks = _midi_tracks(midi)
    hits = [tk for ev in tracks for tk, b in ev if len(b) == 3 and b[0] == 0xBF and b[1] == CHORUS_CC[0] and b[2] == 1]
    if not hits: return None
    tk = min(hits)
    tempos = sorted((t, int.from_bytes(b[3:6], 'big')) for ev in tracks for t, b in ev if b[:2] == b'\xff\x51')
    tick = 0; sec = 0.0; tempo = 500000
    for t, tp in tempos:
        if t >= tk: break
        sec += (t - tick) * tempo / 1e6 / div; tick = t; tempo = tp
    return sec + (tk - tick) * tempo / 1e6 / div


def set_chorus_trigger(midi, at):
    """The MIDI with its chorus start events removed and, when `at` (seconds) is given, one start at that time."""
    fmt, div, tracks = _midi_tracks(midi)
    tracks = [[e for e in ev if not (len(e[1]) == 3 and e[1][0] == 0xBF and e[1][1] in CHORUS_CC)] for ev in tracks]
    if at is not None:
        tk = max(1, _seconds_to_tick(tracks, div, max(0.0, float(at))))
        evs = [(tk, bytes([0xBF, CHORUS_CC[1], 14])), (tk, bytes([0xBF, CHORUS_CC[0], 1]))]
        # in the channel-16 track (the one named '@Title$3Artist' on the disc's songs), else in a new track
        home = [ev for ev in tracks if any(0x80 <= b[0] < 0xF0 and (b[0] & 15) == 15 for _, b in ev)]
        if home: home[0].extend(evs); home[0].sort(key=lambda e: e[0])
        else: tracks.append([(0, b'\xff\x03\x06chorus')] + evs)
    out = _midi_write(fmt, div, tracks)
    if not karamidi.midi_ok(out): raise ValueError('could not add the chorus start to this MIDI file')
    return out


def convert_chorus(src, ffmpeg=None):
    """Any audio file -> the disc's chorus format: WMA v2 in ASF, stereo, 48 kbps (bytes)."""
    import subprocess, tempfile
    data = open(src, 'rb').read()
    if data[:16] == karamidi.ASF_HEADER_GUID: return data                   # already WMA
    ff = ffmpeg or isoread.find_ffmpeg()
    if not ff: raise DiscWriteError('ffmpeg is needed to convert the chorus audio to WMA')
    fd, out = tempfile.mkstemp(suffix='.wma'); os.close(fd)
    try:
        r = subprocess.run([ff, '-v', 'error', '-y', '-i', src, '-vn', '-c:a', 'wmav2', '-b:a', '48k', '-ar', '44100', '-ac', '2',
                            '-f', 'asf', out], capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if r.returncode: raise DiscWriteError('could not convert the chorus audio: ' + r.stderr.decode(errors='replace')[-300:])
        return open(out, 'rb').read()
    finally:
        try: os.remove(out)
        except OSError: pass


def midi_title(midi):
    """(title, artist) from the '@Title$3Artist' text event the Megapro songs carry, or from a track name."""
    try:
        import sf2synth
        info = sf2synth.midi_info(midi)
        if info.get('title'): return info['title'], info.get('artist') or ''
    except Exception:
        pass
    return '', ''


# ----------------------------------------------------------------------------------------------- music videos
MV_KIND = 3                   # ML0 kind of the disc's music videos (kind 7 + flag 0x00100000: the "Just Dance" ones)
MVV_TAGS = (b'DX50', b'DIVX', b'XVID', b'FMP4', b'MP4V')


def avi_info(head):
    """(video fourcc, audio format tag, width, height) from the start of an AVI file, or None if it is not an AVI."""
    if head[:4] != b'RIFF' or head[8:12] != b'AVI ': return None
    vid = aud = None; w = h = 0; p = 12
    i = head.find(b'strh')
    while i >= 0 and i + 64 < len(head):
        typ = head[i + 8:i + 12]; handler = head[i + 12:i + 16]
        j = head.find(b'strf', i + 8)
        if j < 0: break
        if typ == b'vids':
            comp = head[j + 24:j + 28]; vid = (comp if comp.strip(b'\0') else handler).upper()
            w, h = struct.unpack_from('<iI', head, j + 12)
        elif typ == b'auds':
            aud = struct.unpack_from('<H', head, j + 8)[0]
        i = head.find(b'strh', j)
    return vid, aud, abs(w), h


def mvv_compatible(path):
    """True when a file can go on the disc as it is: AVI with MPEG-4 ASP video (DivX/Xvid) and MP3 audio, like the
    disc's own music videos (560x400, 25 fps, MP3 44.1 kHz stereo)."""
    with open(path, 'rb') as f: head = f.read(65536)
    info = avi_info(head)
    if not info: return False
    vid, aud, w, h = info
    return vid in MVV_TAGS and aud in (0x55, None) and 0 < w <= 720 and 0 < h <= 576


def video_duration(path, ffmpeg=None):
    """Length in seconds from ffmpeg's report (None if unknown)."""
    ff = ffmpeg or isoread.find_ffmpeg()
    if not ff: return None
    r = subprocess.run([ff, '-hide_banner', '-i', path], capture_output=True)
    m = re.search(rb'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)', r.stderr)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else None


def convert_mvv(src, dest, ffmpeg=None, progress=None):
    """Convert any video ffmpeg can read to the disc's music-video format: AVI, MPEG-4 ASP (DX50 tag), at most
    720x480 with the aspect kept, at most 30 fps, MP3 128 kb/s 44.1 kHz stereo. progress(fraction) is called."""
    ff = ffmpeg or isoread.find_ffmpeg()
    if not ff: raise DiscWriteError('converting this video needs ffmpeg (put ffmpeg.exe next to the program). '
                                    'Files that are already AVI with DivX/Xvid video and MP3 sound are added as they are.')
    dur = video_duration(src, ff) or 0
    scale = "scale='min(720,iw)':'min(480,ih)':force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2"
    def run(vf):
        cmd = [ff, '-v', 'error', '-nostats', '-progress', 'pipe:1', '-y', '-i', src, '-map', '0:v:0', '-map', '0:a:0?',
               '-vf', vf, '-c:v', 'mpeg4', '-vtag', 'DX50', '-q:v', '4', '-g', '250', '-bf', '0',
               '-c:a', 'libmp3lame', '-b:a', '128k', '-ar', '44100', '-ac', '2', '-f', 'avi', dest]
        pr = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        err = []
        t = threading.Thread(target=lambda: err.append(pr.stderr.read()), daemon=True); t.start()
        for line in pr.stdout:
            if progress and dur and line.startswith(b'out_time_us='):
                try: progress(min(0.99, int(line.split(b'=')[1]) / 1e6 / dur))
                except ValueError: pass
        pr.wait(); t.join(5)
        return pr.returncode, (err[0] if err else b'')
    rc, err = run(scale + ",fps='min(30,source_fps)'")
    if rc != 0: rc, err = run(scale)                       # older ffmpeg: no source_fps in the fps filter
    if rc != 0 or not os.path.exists(dest):
        raise DiscWriteError('ffmpeg could not convert the video: ' + err.decode('utf-8', 'replace').strip()[-300:])
    if not mvv_compatible(dest): raise DiscWriteError('the converted video is not in the expected format')
    if progress: progress(1.0)
    return dest


# The song record of a music video. The disc's own MV records are not in the samples; the emulated firmware plays a
# video from the start when its record is all zeros (tested), while other record contents make it seek elsewhere.
MV_RECORD = bytes(SECTOR)


# ----------------------------------------------------------------------------------------------- disc sources
class _IsoSource:
    def __init__(self, path):
        self.path = path; self.disc = isoread.Disc(path)
        src = self.disc.src
        self.plain = isinstance(src, isoread.SectorSource) and src.ss == 2048 and src.off == 0
        self.kara = _find_kara(self.listdir, '/')

    def listdir(self, d): return [(e['name'], e['is_dir'], e['size']) for e in self.disc.listdir(d)]
    def read(self, path, off=0, n=None): return self.disc.read(path, off, n)
    def size(self, path): return self.disc.lookup(path).size
    def lba(self, path): return self.disc.lookup(path).extents[0][0]
    def close(self): self.disc.close()


class _FolderSource:
    def __init__(self, path):
        self.path = path; self.plain = False
        self.kara = _find_kara(lambda p: self.listdir(p), '/')

    def _real(self, p): return os.path.join(self.path, *[x for x in p.split('/') if x])
    def listdir(self, d):
        r = self._real(d)
        return [(n, os.path.isdir(os.path.join(r, n)), os.path.getsize(os.path.join(r, n)) if os.path.isfile(os.path.join(r, n)) else 0)
                for n in sorted(os.listdir(r))]
    def read(self, path, off=0, n=None):
        with open(self._real(path), 'rb') as f:
            f.seek(off); return f.read() if n is None else f.read(n)
    def size(self, path): return os.path.getsize(self._real(path))
    def close(self): pass


def _find_kara(listdir, root):
    for name, is_dir, _ in listdir(root):
        if is_dir and name.upper().startswith('KARA'): return '/' + name
    raise ValueError('no KARA* folder on this disc - not a Megapro karaoke disc')


# ----------------------------------------------------------------------------------------------- the disc
class KaraokeDisc:
    def __init__(self, path):
        path = os.path.abspath(path)
        self.src = _FolderSource(path) if os.path.isdir(path) else _IsoSource(path)
        self.path = path
        k = self.src.kara
        names = {n.upper(): n for n, d, s in self.src.listdir(k) if not d}
        self.kara = k
        self.hdr_path = k + '/' + names['TITLE.HDR']; self.bnk_path = k + '/' + names['TITLE.BNK']
        # bank file index i: 0 = AUDIO.DAT, i = AUDIOi.DAi
        self.bank_files = []
        for i in range(8):
            nm = 'AUDIO.DAT' if i == 0 else 'AUDIO%d.DA%d' % (i, i)
            self.bank_files.append((k + '/' + names[nm], self.src.size(k + '/' + names[nm])) if nm in names else None)
        while self.bank_files and self.bank_files[-1] is None: self.bank_files.pop()
        self.banks = [b for b in self.bank_files if b]
        self.hdr_words, self.table = decode_hdr(self.src.read(self.hdr_path, 0, 2048 + 16))
        self.entries = hdr_entries(self.table)
        ml = [e for e in self.entries if e[1] == 5]
        if not ml: raise ValueError('song list (ML0) not found in title.hdr')
        self.ml0_entry = ml[0]
        _, typ, par, off, size = ml[0][:5]
        self.songs = parse_ml0(self.src.read(self.bnk_path, off, size))
        self.added = []                       # Song objects with .midi set
        # music videos: KARA125/MVV/<number>.mvv
        self.mvv_dir = None; self.mvv_files = {}
        dirs = {n.upper(): n for n, d, s in self.src.listdir(k) if d}
        if 'MVV' in dirs:
            self.mvv_dir = k + '/' + dirs['MVV']
            for n, d, sz in self.src.listdir(self.mvv_dir):
                m = re.match(r'^(\d+)\.mvv$', n, re.I)
                if m and not d: self.mvv_files[int(m.group(1))] = (n, sz)
        for s_ in self.songs:
            if s_.number in self.mvv_files and s_.is_mv: s_.mvv, s_.mvv_size = self.mvv_files[s_.number]
            if s_.is_mv: s_.lyr = self._has_lyrics(s_)

    # ---- reading
    def close(self): self.src.close()

    def bank_total(self): return sum(s for _, s in self.banks)

    def _locate(self, bank, offset):
        if bank >= len(self.bank_files) or self.bank_files[bank] is None: raise KeyError('song bank file %d missing on this disc' % bank)
        path, size = self.bank_files[bank]
        if offset >= size: raise KeyError('song offset beyond %s' % path.split('/')[-1])
        return path, offset

    def record(self, song):
        if song.new_lyrics is not None: return make_record(song.new_lyrics) if song.new_lyrics else MV_RECORD
        if song.mvv_src: return make_record(song.midi) if song.midi is not None else MV_RECORD
        if song.midi is not None: return make_record(song.midi)
        path, off = self._locate(song.bank, song.offset)
        h = self.src.read(path, off, 40)
        rsize = struct.unpack_from('<I', h, 4)[0]
        if not rsize or rsize > 64 << 20: raise ValueError('no song record at #%d\'s position' % song.number)
        return self.src.read(path, off, rsize)

    def find(self, number):
        for s in self.songs + self.added:
            if s.number == number and not s.deleted: return s
        return None

    def listing(self, query='', start=0, n=100, show_deleted=True, only=None):
        q = (query or '').strip().lower()
        allsongs = sorted(self.songs + self.added, key=lambda s: s.number)
        if only == 'mv': allsongs = [s for s in allsongs if s.is_mv]
        elif only == 'songs': allsongs = [s for s in allsongs if not s.is_mv]
        if q:
            allsongs = [s for s in allsongs if q in ('%06d' % s.number) or q in s.title.lower() or q in s.artist.lower()]
        if not show_deleted: allsongs = [s for s in allsongs if not s.deleted]
        return dict(total=len(allsongs), start=start, songs=[s.as_dict() for s in allsongs[start:start + n]],
                    disc_songs=len(self.songs), added=len(self.added), deleted=sum(s.deleted for s in self.songs),
                    mvs=sum(1 for s in self.songs + self.added if s.is_mv and not s.deleted), has_mvv=self.mvv_dir is not None)

    def midi(self, song):
        if song.new_lyrics is not None:
            if not song.new_lyrics: raise KeyError('#%d: lyrics removed' % song.number)
            return song.new_lyrics
        if song.mvv_src and song.midi is None: raise KeyError('#%d is a music video without lyrics' % song.number)
        if song.midi is not None: return song.midi
        return karamidi.decode_record(self.record(song))

    def chorus(self, song):
        """The recorded clips of a song as WMA files (bytes), in record order."""
        if song.new_chorus is not None: return [song.new_chorus] if song.new_chorus else []
        if song.midi is not None or song.mvv_src: return []
        rec = self.record(song)
        if not struct.unpack_from('<I', rec, 12)[0]: return []
        return [d for _, _, _, d in karamidi.record_parts(rec)]

    def extract(self, song, folder, wav=True):
        """Write <number> <title>.mid and the chorus clips (.wma, and .wav when ffmpeg is available)."""
        os.makedirs(folder, exist_ok=True)
        base = '%06d %s' % (song.number, _safe(song.title + (' - ' + song.artist if song.artist else '')))
        out = []
        if song.is_mv:
            return self.extract_mv(song, folder, base)
        p = os.path.join(folder, base + '.mid'); open(p, 'wb').write(self.midi(song)); out.append(p)
        clips = self.chorus(song)
        ff = isoread.find_ffmpeg() if wav else None
        for i, w in enumerate(clips, 1):
            suffix = ' chorus' + (' %d' % i if len(clips) > 1 else '')
            p = os.path.join(folder, base + suffix + '.wma'); open(p, 'wb').write(w); out.append(p)
            if ff:
                pw = p[:-4] + '.wav'
                r = subprocess.run([ff, '-v', 'error', '-y', '-i', p, pw], capture_output=True)
                if r.returncode == 0: out.append(pw)
        return out

    def extract_mv(self, song, folder, base=None):
        """Write the music video as <number> <title>.avi (the .mvv files are AVI), plus its lyrics MIDI when the
        song record holds one."""
        os.makedirs(folder, exist_ok=True)
        base = base or '%06d %s' % (song.number, _safe(song.title + (' - ' + song.artist if song.artist else '')))
        out = []
        p = os.path.join(folder, base + '.avi')
        if song.mvv_src:
            shutil.copyfile(song.mvv_src, p); out.append(p)
        elif song.mvv:
            path = self.mvv_dir + '/' + song.mvv; size = self.src.size(path); pos = 0
            with open(p, 'wb') as f:
                while pos < size:
                    b = self.src.read(path, pos, min(8 << 20, size - pos))
                    if not b: break
                    f.write(b); pos += len(b)
            out.append(p)
        try:
            m = self.midi(song)
            if m and karamidi.midi_ok(m):
                q = os.path.join(folder, base + ' lyrics.mid'); open(q, 'wb').write(m); out.append(q)
        except Exception:
            pass
        if not out: raise KeyError('the video file of #%d is not on this disc' % song.number)
        return out

    # ---- editing
    def delete(self, number, remove_file=True):
        s = self.find(number)
        if s is None: raise KeyError('no song #%d' % number)
        if s in self.added: self.added.remove(s)
        else: s.deleted = True; s.remove_file = bool(remove_file)

    def undelete(self, number):
        for s in self.songs:
            if s.number == number: s.deleted = False; return
        raise KeyError('no deleted song #%d' % number)

    def set_chorus(self, number, wma, at=None):
        """Give a karaoke song a recorded part ("chorus": WMA bytes, see convert_chorus) that starts `at` seconds
        into the song (None: where the song's current chorus starts, else 0), or remove it (wma=None)."""
        s = self.find(int(number))
        if s is None or s.deleted: raise KeyError('no song #%s' % number)
        if s.is_mv: raise ValueError('#%s is a music video - it has its own sound' % number)
        if wma is not None:
            if wma[:16] != karamidi.ASF_HEADER_GUID: raise ValueError('the chorus must be WMA (convert it first)')
            if at is None:
                try: at = chorus_trigger(self.midi(s))
                except Exception: at = None
            s.new_chorus = bytes(wma); s.chorus_at = float(at or 0.0)
            make_record(set_chorus_trigger(self.midi(s), s.chorus_at), [s.new_chorus])     # validates
        else:
            s.new_chorus = b'' if (s.has_audio or s.origin != 'added') else None; s.chorus_at = None
        return s

    def chorus_info(self, number):
        s = self.find(int(number))
        if s is None: raise KeyError('no song #%s' % number)
        at = s.chorus_at
        if at is None and s.has_audio:
            try: at = chorus_trigger(self.midi(s))
            except Exception: at = None
        clips = len(self.chorus(s)) if s.new_chorus is None and s.has_audio else (1 if s.new_chorus else 0)
        return dict(number=s.number, title=s.title, chorus=bool(clips), clips=clips, at=at,
                    changed=s.new_chorus is not None)

    def add(self, number, midi, title=None, artist=None):
        number = int(number)
        if not 1 <= number <= MAX_NUMBER: raise ValueError('song numbers are 1 to %d (the player\'s number entry stops at 5 digits)' % MAX_NUMBER)
        if self.find(number) is not None: raise ValueError('song #%d already exists (delete it first to replace it)' % number)
        make_record(midi)                                         # validates and checks the encoding
        t, a = midi_title(midi)
        s = Song(number, FLAG_BASE, 0, 0, title or t or 'Song %d' % number, artist if artist is not None else a, origin='added')
        s.midi = midi
        self.added.append(s); return s

    def _has_lyrics(self, song):
        try:
            h = self.record(song)[:48] if song.midi is None and song.new_lyrics is None and not song.mvv_src else None
            if h is not None and (len(h) < 20 or not 0 < struct.unpack_from('<I', h, 8)[0] < 1 << 20): return False
            m = self.midi(song)
            return bool(karamidi.midi_ok(m) and mvlyrics.lyrics_of(mvlyrics.read(m)[0]))
        except Exception:
            return False

    def set_lyrics(self, number, lyrics_midi):
        """Give a music video lyrics (a MIDI from mvlyrics.build) or remove them (None). Saved as a new song record."""
        s = self.find(int(number))
        if s is None or not s.is_mv: raise KeyError('#%s is not a music video' % number)
        if lyrics_midi is not None: make_record(lyrics_midi)               # validates
        if s in self.added: s.midi = lyrics_midi
        else: s.new_lyrics = lyrics_midi if lyrics_midi is not None else b''
        s.lyr = lyrics_midi is not None
        return s

    def add_mvv(self, number, video, title=None, artist=None, workdir=None, progress=None, lyrics=None):
        """Add a music video as KARA125/MVV/<number>.mvv with a song-list entry. video: a file path. AVI files with
        DivX/Xvid video and MP3 sound are used as they are; anything else is converted with ffmpeg."""
        number = int(number)
        if not 1 <= number <= MAX_NUMBER: raise ValueError('numbers are 1 to %d (the player\'s number entry stops at 5 digits)' % MAX_NUMBER)
        if self.find(number) is not None: raise ValueError('#%d already exists (delete it first to replace it)' % number)
        if self.mvv_dir is None: raise DiscWriteError('this disc has no KARA125/MVV folder, so its player software has no music videos')
        if not os.path.isfile(video): raise ValueError('video file not found')
        title = (title or os.path.splitext(os.path.basename(video))[0]).strip() or 'Video %d' % number
        artist = artist or ''
        if mvv_compatible(video):
            src = video; converted = False
        else:
            import tempfile
            wd = workdir or tempfile.mkdtemp(prefix='mvv_')
            src = convert_mvv(video, os.path.join(wd, '%05d.mvv' % number), progress=progress); converted = True
        s = Song(number, FLAG_BASE, 0, 0, title, artist, origin='added')
        s.kind = MV_KIND
        s.mvv_src = src; s.mvv = '%05d.mvv' % number; s.mvv_size = os.path.getsize(src)
        if lyrics is not None: make_record(lyrics); s.midi = lyrics; s.lyr = True
        self.added.append(s)
        s.converted = converted
        return s

    def pending(self):
        mv = [s for s in self.added if s.is_mv]
        return dict(added=len(self.added), deleted=sum(s.deleted for s in self.songs), added_mv=len(mv),
                    mv_bytes=sum(s.mvv_size for s in mv),
                    lyrics=sum(1 for s in self.songs if s.new_lyrics is not None and not s.deleted),
                    chorus=sum(1 for s in self.songs if s.new_chorus is not None and not s.deleted))

    # ---- writing
    def save(self, out, progress=None):
        """Write the edited disc. out = .iso path (from an ISO image) or a folder (from either)."""
        out = os.path.abspath(out)
        if os.path.exists(out) and os.path.samefile(out, self.path):
            raise DiscWriteError('choose a new name - the original disc is never overwritten')
        keep = [s for s in self.songs if not s.deleted]
        # new bank file for added songs
        new_bank = None; new_songs = []; moved = {}
        rehome = [s for s in keep if s.new_lyrics is not None or s.new_chorus is not None]   # new lyrics / chorus
        if self.added or rehome:
            idx = max(1, len(self.bank_files))                  # the next free AUDIOn.DAn
            if idx > 7: raise DiscWriteError('no free song bank file number left (AUDIO1..7)')
            name = 'AUDIO%d.DA%d' % (idx, idx)
            buf = bytearray()
            for s in sorted(self.added, key=lambda x: x.number):
                flags = s.flags
                if s.midi is not None and s.new_chorus:
                    rec = make_record(set_chorus_trigger(s.midi, s.chorus_at or 0.0), [s.new_chorus]); flags |= FLAG_AUDIO
                else:
                    rec = make_record(s.midi) if s.midi is not None else MV_RECORD
                ns = Song(s.number, flags, idx, len(buf), s.title, s.artist, origin='added'); ns.kind = s.kind; new_songs.append(ns)
                buf += rec
            for s in rehome:
                moved[id(s)] = (idx, len(buf), s.flags)
                if s.new_chorus is not None and s.new_lyrics is None:
                    midi = self.midi(s)
                    if s.new_chorus:
                        rec = make_record(set_chorus_trigger(midi, s.chorus_at or 0.0), [s.new_chorus])
                        moved[id(s)] = (idx, len(buf), s.flags | FLAG_AUDIO)
                    else:
                        rec = make_record(set_chorus_trigger(midi, None))
                        moved[id(s)] = (idx, len(buf), s.flags & ~FLAG_AUDIO)
                    buf += rec
                else:
                    buf += self.record(s)
            new_bank = (name, bytes(buf))
        listed = []
        for s in keep:
            if id(s) in moved:
                ns = Song(s.number, moved[id(s)][2], moved[id(s)][0], moved[id(s)][1], s.title, s.artist, loc_flags=s.loc_flags); ns.kind = s.kind
                listed.append(ns)
            else: listed.append(s)
        keep = listed
        ml0 = build_ml0(keep + sorted(new_songs, key=lambda x: x.number))
        bnk_old = self.src.read(self.bnk_path)
        bnk, table = self._new_bnk(bnk_old, ml0)
        hdr = encode_hdr(self.hdr_words, table)
        # music-video files: new ones in, deleted ones out (unless another kept entry still uses the number)
        kept_numbers = set(s.number for s in keep)
        mvv_add = [(s.mvv, s.mvv_src) for s in self.added if s.is_mv and s.mvv_src]
        mvv_remove = [self.mvv_files[s.number][0] for s in self.songs
                      if s.deleted and s.is_mv and s.remove_file and s.number in self.mvv_files and s.number not in kept_numbers]
        if isinstance(self.src, _IsoSource) and not os.path.isdir(out) and out.lower().endswith('.iso'):
            if not self.src.plain: raise DiscWriteError('writing an ISO needs a plain 2048-byte-sector .iso source image')
            _write_iso(self, out, hdr, bnk, new_bank, progress, mvv_add, mvv_remove)
        else:
            _write_folder(self, out, hdr, bnk, new_bank, progress, mvv_add, mvv_remove)
        return out

    def _new_bnk(self, bnk, ml0):
        """title.bnk with the new ML0 section: in place if it fits before the next section, else appended."""
        table = bytearray(self.table)
        pos, typ, par, off, size, a, b, c, h = self.ml0_entry
        offs = sorted(set(e[3] for e in self.entries if e[3] > off))
        room = (offs[0] if offs else len(bnk)) - off
        bnk = bytearray(bnk)
        if len(ml0) <= room:
            bnk[off:off + room] = ml0 + bytes(room - len(ml0)); new_off = off
        else:
            new_off = (len(bnk) + SECTOR - 1) // SECTOR * SECTOR
            bnk += bytes(new_off - len(bnk)) + ml0
        bnk += bytes((-len(bnk)) % SECTOR)
        hv = section_hash(ml0)
        struct.pack_into('<8I', table, pos, typ, par, new_off, len(ml0), a, b, c, h if hv is None else hv)
        for e in self.entries:                                     # end marker + total size
            if e[1] == 13: struct.pack_into('<I', table, e[0] + 8, len(bnk))
        struct.pack_into('<I', table, 19 * 4, len(bnk))         # header word 19 = title.bnk size
        return bytes(bnk), bytes(table)


def _safe(s):
    s = re.sub(r'[\\/:*?"<>|]+', ' ', s).strip()
    return s[:80] or 'song'


# ----------------------------------------------------------------------------------------------- folder output
def _write_folder(disc, out, hdr, bnk, new_bank, progress, mvv_add=(), mvv_remove=()):
    if os.path.exists(out) and os.listdir(out): raise DiscWriteError('the output folder must be new or empty')
    src = disc.src
    files = []
    skip = set((disc.mvv_dir or '') + '/' + n for n in mvv_remove)
    def walk(d):
        for name, is_dir, size in src.listdir(d):
            p = (d.rstrip('/') + '/' + name)
            if is_dir: walk(p)
            elif p not in skip: files.append((p, size))
    walk('/')
    total = sum(s for _, s in files) or 1; done = 0
    for p, size in files:
        dest = os.path.join(out, *[x for x in p.split('/') if x])
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if p == disc.hdr_path: open(dest, 'wb').write(hdr)
        elif p == disc.bnk_path: open(dest, 'wb').write(bnk)
        else:
            with open(dest, 'wb') as f:
                pos = 0
                while pos < size:
                    chunk = src.read(p, pos, min(8 << 20, size - pos)); f.write(chunk); pos += len(chunk); done += len(chunk)
                    if progress: progress(done / total)
                    if not chunk: break
    if new_bank:
        open(os.path.join(out, *[x for x in disc.kara.split('/') if x], new_bank[0]), 'wb').write(new_bank[1])
    for name, path in mvv_add:
        d = os.path.join(out, *[x for x in disc.mvv_dir.split('/') if x]); os.makedirs(d, exist_ok=True)
        shutil.copyfile(path, os.path.join(d, name))
    if progress: progress(1.0)


# ----------------------------------------------------------------------------------------------- ISO output
def _dirrecs(sector_bytes):
    """Parse ISO9660 directory records in a directory extent: [(offset, extent, size, flags, name_bytes)]."""
    out = []; p = 0
    while p < len(sector_bytes):
        ln = sector_bytes[p]
        if ln == 0:
            p = (p // SECTOR + 1) * SECTOR; continue
        ext = struct.unpack_from('<I', sector_bytes, p + 2)[0]; size = struct.unpack_from('<I', sector_bytes, p + 10)[0]
        nl = sector_bytes[p + 32]; name = sector_bytes[p + 33:p + 33 + nl]
        out.append((p, ext, size, sector_bytes[p + 25], name)); p += ln
    return out


def _both32(v): return struct.pack('<I', v) + struct.pack('>I', v)


def _write_iso(disc, out, hdr, bnk, new_bank, progress, mvv_add=(), mvv_remove=()):
    """mvv_add: [(file name, source path or bytes)], mvv_remove: [file name] in KARA125/MVV."""
    srcp = disc.path
    if os.path.exists(out) and os.path.samefile(out, srcp): raise DiscWriteError('choose a new file name')
    total = os.path.getsize(srcp)
    extra = sum((os.path.getsize(x) if isinstance(x, str) else len(x)) for _, x in mvv_add)
    with open(srcp, 'rb') as fi, open(out + '.part', 'wb') as fo:
        done = 0
        Z = bytes(16 << 20)
        while True:
            b = fi.read(16 << 20)
            if not b: break
            if b == Z[:len(b)]: fo.seek(len(b), 1)                # unused space: leave a hole (sparse copy)
            else: fo.write(b)
            done += len(b)
            if progress: progress(0.85 * done / (total + extra))
        fo.truncate(done)
    f = open(out + '.part', 'r+b')
    try:
        end_lba = (total + SECTOR - 1) // SECTOR
        def rd(lba, n=1): f.seek(lba * SECTOR); return f.read(n * SECTOR)
        def wr(lba, data): f.seek(lba * SECTOR); f.write(data)
        def append(data):
            nonlocal end_lba
            lba = end_lba; wr(lba, data + bytes((-len(data)) % SECTOR)); end_lba += (len(data) + SECTOR - 1) // SECTOR
            return lba
        def append_file(path):
            nonlocal end_lba
            lba = end_lba; n = 0
            with open(path, 'rb') as g:
                f.seek(lba * SECTOR)
                while True:
                    b = g.read(8 << 20)
                    if not b: break
                    f.write(b); n += len(b)
            f.write(bytes((-n) % SECTOR)); end_lba += (n + SECTOR - 1) // SECTOR
            return lba, n
        # title.hdr: in place (one sector)
        hdr_lba = disc.src.lba(disc.hdr_path)
        if len(hdr) > SECTOR: raise DiscWriteError('title.hdr grew beyond one sector')
        wr(hdr_lba, hdr + bytes(SECTOR - len(hdr)))
        # title.bnk: in place if it fits its sectors, else appended
        bnk_lba = disc.src.lba(disc.bnk_path); old_secs = (disc.src.size(disc.bnk_path) + SECTOR - 1) // SECTOR
        if (len(bnk) + SECTOR - 1) // SECTOR <= old_secs: wr(bnk_lba, bnk)
        else: bnk_lba = append(bnk)
        kara_set = {disc.bnk_path.split('/')[-1].upper(): (bnk_lba, len(bnk)), disc.hdr_path.split('/')[-1].upper(): (hdr_lba, len(hdr))}
        kara_add = []
        if new_bank:
            lba = append(new_bank[1]); kara_add.append((new_bank[0], lba, len(new_bank[1])))
        mv_add = []
        for name, data in mvv_add:
            if isinstance(data, str): lba, n = append_file(data)
            else: lba = append(data); n = len(data)
            mv_add.append((name, lba, n))
            if progress: progress(min(0.97, 0.85 + 0.12 * (end_lba * SECTOR - total) / max(1, extra)))
        mv_remove = set(x.upper() for x in mvv_remove)
        # every directory tree (ISO9660 primary + Joliet)
        vd_lba = 16
        while True:
            vd = bytearray(rd(vd_lba))
            if vd[0] == 255 or vd[1:6] != b'CD001': break
            if vd[0] in (1, 2):
                joliet = vd[0] == 2
                root = vd[156:156 + 34]; r_ext = struct.unpack_from('<I', root, 2)[0]; r_size = struct.unpack_from('<I', root, 10)[0]
                kdir = _find_entry(rd, r_ext, r_size, lambda nm: nm.upper().startswith('KARA'), joliet, want_dir=True)
                if kdir is None: raise DiscWriteError('KARA folder not found in the ISO directory')
                kset = dict(kara_set)
                if mv_add or mv_remove:
                    mdir = _find_entry(rd, kdir[0], kdir[1], lambda nm: nm.upper() == 'MVV', joliet, want_dir=True)
                    if mdir is None:
                        if mv_remove: raise DiscWriteError('KARA125/MVV folder not found')
                        raise DiscWriteError('this disc has no KARA125/MVV folder - adding the first music video needs one (not supported yet)')
                    new_ext, new_size = _rewrite_dir(rd, wr, append, mdir[0], mdir[1], {}, mv_add, mv_remove, joliet)
                    if (new_ext, new_size) != mdir:
                        kset['MVV'] = (new_ext, new_size)
                        if new_ext != mdir[0]: _patch_path_tables(rd, wr, vd, mdir[0], new_ext)
                k_ext, k_size = _rewrite_dir(rd, wr, append, kdir[0], kdir[1], kset, kara_add, (), joliet)
                if k_ext != kdir[0]:
                    # the KARA folder moved: its record in the root, the path tables and the '..' of its sub-folders
                    kname = _find_name(rd, r_ext, r_size, kdir[0], joliet)
                    _rewrite_dir(rd, wr, append, r_ext, r_size, {kname: (k_ext, k_size)}, (), (), joliet)
                    _patch_path_tables(rd, wr, vd, kdir[0], k_ext)
                    for p, e, sz, fl, nm in _dirrecs(rd(k_ext, k_size // SECTOR)):
                        if fl & 2 and nm not in (b'\x00', b'\x01'):
                            sub = bytearray(rd(e)); 
                            recs = _dirrecs(bytes(sub))
                            if len(recs) > 1 and recs[1][4] == b'\x01':
                                q = recs[1][0]; sub[q + 2:q + 10] = _both32(k_ext); sub[q + 10:q + 18] = _both32(k_size); wr(e, bytes(sub))
            vd_lba += 1
        # slack after the appended data: the player reads one sector past the end of a directory and streams
        # ahead past the end of a video file; at the very end of the disc those reads fail (tested: the last
        # directory sector is then dropped)
        if end_lba * SECTOR > total: end_lba += PAD_SECTORS
        # volume space size in every volume descriptor
        vd_lba = 16
        while True:
            vd = bytearray(rd(vd_lba))
            if vd[0] == 255 or vd[1:6] != b'CD001': break
            if vd[0] in (1, 2): vd[80:88] = _both32(end_lba); wr(vd_lba, bytes(vd))
            vd_lba += 1
        f.truncate(end_lba * SECTOR)
    finally:
        f.close()
    os.replace(out + '.part', out)
    if progress: progress(1.0)


def _name(rec_name, joliet):
    return (rec_name.decode('utf-16-be', 'replace') if joliet else rec_name.decode('latin1')).split(';')[0]


def _find_entry(rd, ext, size, match, joliet, want_dir=False):
    for p, e, sz, fl, nm in _dirrecs(rd(ext, (size + SECTOR - 1) // SECTOR)[:size]):
        if nm in (b'\x00', b'\x01'): continue
        if bool(fl & 2) == want_dir and match(_name(nm, joliet)): return (e, sz)
    return None


def _find_name(rd, ext, size, target, joliet):
    for p, e, sz, fl, nm in _dirrecs(rd(ext, (size + SECTOR - 1) // SECTOR)[:size]):
        if e == target and nm not in (b'\x00', b'\x01'): return _name(nm, joliet).upper()
    raise DiscWriteError('folder record not found')


def _rewrite_dir(rd, wr, append, ext, size, set_, add, remove, joliet):
    """Apply edits to one directory extent: set_ {NAME: (lba, size)} updates records, add [(name, lba, size)] adds
    file records, remove {NAME} drops them. Written in place when it fits, else moved to the end of the image (the
    '.' record follows; the caller fixes the parent record and the path tables). Returns (extent, size)."""
    nsec = (size + SECTOR - 1) // SECTOR
    blob = bytearray(rd(ext, nsec)[:size])
    recs = _dirrecs(bytes(blob))
    items = []
    for p, e, sz, fl, nm in recs:
        r = bytearray(blob[p:p + blob[p]])
        if nm not in (b'\x00', b'\x01'):
            key = _name(nm, joliet).upper()
            if key in remove: continue
            if key in set_:
                lba, ln = set_[key]; r[2:10] = _both32(lba); r[10:18] = _both32(ln)
        items.append((nm, r))
    tmpl = next((bytes(r) for nm, r in items if nm not in (b'\x00', b'\x01') and not r[25] & 2), None)
    tname = _name(tmpl[33:33 + tmpl[32]], joliet) if tmpl else ''
    tfull = (tmpl[33:33 + tmpl[32]].decode('utf-16-be', 'replace') if joliet else tmpl[33:33 + tmpl[32]].decode('latin1')) if tmpl else ';1'
    lower = tname != tname.upper() and tname == tname.lower()          # keep the disc's own name style
    ver = ';1' if tfull.endswith(';1') or not tmpl else ''
    for name, lba, ln in add:
        nm_ = (name.lower() if lower else name.upper()) + ver
        ident = nm_.encode('utf-16-be') if joliet else nm_.encode('ascii')
        L = 33 + len(ident); L += L & 1
        r = bytearray(L); r[0] = L
        r[2:10] = _both32(lba); r[10:18] = _both32(ln)
        r[18:25] = tmpl[18:25] if tmpl else bytes(7)
        r[28:32] = struct.pack('<H', 1) + struct.pack('>H', 1)
        r[32] = len(ident); r[33:33 + len(ident)] = ident
        items = [it for it in items if not (it[0] not in (b'\x00', b'\x01') and _name(it[0], joliet).upper() == name.upper())]
        items.append((ident, r))
    special = [it for it in items if it[0] in (b'\x00', b'\x01')]
    rest = sorted([it for it in items if it[0] not in (b'\x00', b'\x01')], key=lambda it: it[0])
    def layout():
        out = bytearray()
        for _, r in special + rest:
            if len(out) % SECTOR + len(r) > SECTOR: out += bytes(SECTOR - len(out) % SECTOR)
            out += bytes(r)
        return out
    out = layout()
    if len(out) <= nsec * SECTOR:
        new_ext, new_size = ext, nsec * SECTOR
        wr(ext, bytes(out) + bytes(nsec * SECTOR - len(out)))
        return new_ext, size
    # grow: move the directory to the end of the image
    new_secs = (len(out) + SECTOR - 1) // SECTOR
    new_size = new_secs * SECTOR
    dot = special[0][1] if special else None
    probe = bytes(out) + bytes(new_size - len(out))
    new_ext = append(probe)
    if dot is not None:                                   # the '.' record describes the directory itself
        dot[2:10] = _both32(new_ext); dot[10:18] = _both32(new_size)
    out = layout()
    wr(new_ext, bytes(out) + bytes(new_size - len(out)))
    return new_ext, new_size


def _patch_path_tables(rd, wr, vd, old_lba, new_lba):
    """Point the path-table entry of a moved directory at its new extent (L table little-endian, M table big-endian)."""
    size = struct.unpack_from('<I', vd, 132)[0]
    for off, endian in ((140, '<'), (144, '<'), (148, '>'), (152, '>')):
        loc = struct.unpack_from('<I' if endian == '<' else '>I', vd, off)[0]
        if not loc: continue
        nsec = (size + SECTOR - 1) // SECTOR
        t = bytearray(rd(loc, nsec)); p = 0; changed = False
        while p < size:
            ln = t[p]
            if ln == 0: break
            if struct.unpack_from(endian + 'I', t, p + 2)[0] == old_lba:
                struct.pack_into(endian + 'I', t, p + 2, new_lba); changed = True
            p += 8 + ln + (ln & 1)
        if changed: wr(loc, bytes(t))
