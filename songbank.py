"""The karaoke song bank on a Megapro disc (KARA125/audio.dat, audio1.da1, ...): list, decode and play songs."""
import os, struct, threading
import karamidi, sf2synth


class SongBank:
    def __init__(self, disc):
        self.disc = disc; self.files = []; self.index = {}; self.lock = threading.Lock()
        for d in disc.listdir('/'):
            if not d.get('is_dir') or not d['name'].upper().startswith('KARA'): continue
            for f in disc.listdir('/' + d['name']):
                n = f['name'].lower()
                if not f.get('is_dir') and (n.startswith('audio') and (n.endswith('.dat') or '.da' in n)):
                    self.files.append(('/' + d['name'] + '/' + f['name'], f['size']))
        self.files.sort(key=lambda x: (0 if x[0].lower().endswith('.da1') else 1, x[0]))

    def _read(self, path):
        return lambda off, n: self.disc.read(path, off, n)

    def records(self, path):
        """All record offsets of one bank file (cached; reads only the 40-byte headers)."""
        with self.lock:
            if path in self.index: return self.index[path]
        size = dict(self.files).get(path, 0); rd = self._read(path); out = []; pos = 0
        CH = 1 << 20; buf_at = -1; buf = b''; scanning = False
        while pos + 40 <= size:
            if not (buf_at <= pos and pos + 40 <= buf_at + len(buf)):
                # after a good record the next header is right there: read one sector; in gaps read 1 MB at a time
                buf_at = pos; buf = rd(pos, min(CH if scanning else 2048, size - pos))
            h = buf[pos - buf_at:pos - buf_at + 40]
            off, rsize, usize, nx, L = struct.unpack('<5I', h[:20])
            if rsize == 0 or rsize > 16 << 20 or L == 0 or L > rsize or rsize % 2048 or usize == 0:
                pos += 2048; scanning = True; continue
            scanning = False
            out.append((pos, rsize)); pos += rsize
        with self.lock: self.index[path] = out
        return out

    def midi(self, path, offset):
        """MIDI of the record at offset (read straight from its header; no scan of the bank)."""
        h = self.disc.read(path, offset, 40)
        if len(h) < 20: raise KeyError('no song record at that offset')
        o, rsize, usize, nx, L = struct.unpack('<5I', h[:20])
        if rsize == 0 or rsize > 64 << 20 or L == 0 or L > rsize or rsize % 2048 or usize == 0:
            raise KeyError('no song record at that offset')
        return karamidi.decode_record(self.disc.read(path, offset, rsize))

    def listing(self, path, start=0, n=40):
        recs = self.records(path); out = []
        for off, rsize in recs[start:start + n]:
            try:
                info = sf2synth.midi_info(self.midi(path, off)); out.append(dict(offset=off, title=info['title'], artist=info['artist'], ok=True))
            except Exception as ex:
                out.append(dict(offset=off, title='', artist='', ok=False, error=str(ex)[:80]))
        return dict(file=path, total=len(recs), start=start, songs=out)


_SF_CACHE = {}
def soundfont(path):
    p = os.path.abspath(path)
    if p not in _SF_CACHE: _SF_CACHE.clear(); _SF_CACHE[p] = sf2synth.SoundFont(p)
    return _SF_CACHE[p]


def default_soundfont(*dirs):
    for d in dirs:
        if not d: continue
        for name in ('dreamsam_2695.sf2',):
            p = os.path.join(d, name)
            if os.path.exists(p): return p
        try:
            for f in sorted(os.listdir(d)):
                if f.lower().endswith('.sf2'): return os.path.join(d, f)
        except OSError: pass
    return None
