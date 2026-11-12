#!/usr/bin/env python3
"""isoread - dependency-free disc image reader (ISO9660 + Joliet + UDF), DVD-Video / VCD aware.

Supports: 2048-byte .iso, raw 2352-byte (Mode1 / Mode2-XA) and 2336/2448-byte images.
CLI:  python isoread.py disc.iso info|ls [path]|tree|extract <path> <dest>|thumb <out.png>

Status: ISO9660 + Joliet + raw-sector layouts are exercised by tests/test_iso.py.
        The UDF reader follows ECMA-167 / UDF 1.02 but has only been checked against
        images written by this project's own test writer, not against retail discs.
"""
import os, sys, struct, shutil, subprocess, datetime

SECTOR = 2048


class DiscError(Exception):
    pass


# ----------------------------------------------------------------------------- sector layer
class SectorSource:
    LAYOUTS = ((2048, 0), (2352, 16), (2352, 24), (2336, 8), (2448, 16), (2448, 24))

    def __init__(self, path):
        self.f = open(path, 'rb')
        self.total = os.fstat(self.f.fileno()).st_size
        self.ss, self.off = 2048, 0
        self._detect()

    def close(self):
        self.f.close()

    def _read_at(self, pos, n):
        self.f.seek(pos)
        return self.f.read(n)

    def _detect(self):
        # ISO9660: 'CD001' at byte 1 of sector 16;  UDF: 'BEA01' in sectors 16..32 of the volume recognition area
        for ss, off in self.LAYOUTS:
            if self.total < 17 * ss: continue
            b = self._read_at(16 * ss + off, 8)
            if b[1:6] == b'CD001':
                self.ss, self.off = ss, off
                return
        for ss, off in self.LAYOUTS:
            if self.total < 20 * ss: continue
            for s in range(16, 20):
                b = self._read_at(s * ss + off, 8)
                if b[1:6] in (b'BEA01', b'NSR02', b'NSR03'):
                    self.ss, self.off = ss, off
                    return
        self.ss, self.off = 2048, 0

    @property
    def sectors(self):
        return self.total // self.ss

    @property
    def is_xa(self):
        return self.ss in (2352, 2336, 2448) and self.off in (24, 8)

    def read(self, lba, count=1):
        """User data (2048 bytes/sector, Form1 / Mode1 / cooked)."""
        if self.ss == 2048:
            return self._read_at(lba * 2048, count * 2048)
        out = []
        for i in range(count):
            out.append(self._read_at((lba + i) * self.ss + self.off, 2048))
        return b''.join(out)

    def read_payload(self, lba, count=1):
        """Sector payload as stored: 2048 for Form1, 2324 for Mode2 Form2 (VCD video)."""
        if not self.is_xa:
            return self.read(lba, count)
        out = []
        sub = self.off - 8
        for i in range(count):
            base = (lba + i) * self.ss
            sh = self._read_at(base + sub, 8)
            form2 = len(sh) == 8 and (sh[2] & 0x20)
            out.append(self._read_at(base + self.off, 2324 if form2 else 2048))
        return b''.join(out)


# ----------------------------------------------------------------------------- node model
class Node:
    __slots__ = ('name', 'is_dir', 'size', 'extents', 'children_loader', 'mtime')

    def __init__(self, name, is_dir, size, extents, loader=None, mtime=None):
        self.name, self.is_dir, self.size, self.extents = name, is_dir, size, extents  # extents: [(lba, bytes)]
        self.children_loader, self.mtime = loader, mtime


def _norm(path):
    parts = [p for p in path.replace('\\', '/').split('/') if p and p != '.']
    return parts


# ----------------------------------------------------------------------------- ISO9660 / Joliet
class ISO9660:
    def __init__(self, src):
        self.src = src
        self.pvd = None
        self.joliet = None
        lba = 16
        while lba < 16 + 64:
            vd = src.read(lba)
            t, ident = vd[0], vd[1:6]
            if ident != b'CD001': break
            if t == 1 and self.pvd is None: self.pvd = vd
            elif t == 2 and vd[88:91] in (b'%/@', b'%/C', b'%/E'): self.joliet = vd
            elif t == 255: break
            lba += 1
        if self.pvd is None: raise DiscError('no ISO9660 primary volume descriptor')
        use = self.joliet or self.pvd
        self.utf16 = self.joliet is not None
        self.volume_id = self.pvd[40:72].decode('latin-1').strip()
        self.root = self._record_to_node(use[156:190], '/')
        self.root.is_dir = True

    def _decode(self, nb):
        if nb == b'\x00': return '.'
        if nb == b'\x01': return '..'
        s = nb.decode('utf-16-be', 'replace') if self.utf16 else nb.decode('latin-1')
        if ';' in s: s = s.rsplit(';', 1)[0]
        if s.endswith('.') and len(s) > 1: s = s[:-1]
        return s

    def _record_to_node(self, rec, name=None):
        lba = struct.unpack('<I', rec[2:6])[0]
        size = struct.unpack('<I', rec[10:14])[0]
        flags = rec[25]
        nm = name if name is not None else self._decode(rec[33:33 + rec[32]])
        mt = None
        try:
            y, mo, d, h, mi, s = rec[18:24]
            mt = datetime.datetime(1900 + y, mo or 1, d or 1, h, mi, s)
        except Exception:
            pass
        isdir = bool(flags & 2)
        n = Node(nm, isdir, size, [(lba, size)], mtime=mt)
        if isdir: n.children_loader = lambda n=n: self._load_dir(n)
        return n

    def _load_dir(self, node):
        lba, size = node.extents[0]
        data = self.src.read(lba, (size + 2047) // 2048)[:size]
        pos, out = 0, []
        pend = None
        while pos < len(data):
            l = data[pos]
            if l == 0:
                pos = (pos // 2048 + 1) * 2048
                continue
            rec = data[pos:pos + l]
            pos += l
            if len(rec) < 34: break
            name = self._decode(rec[33:33 + rec[32]])
            if name in ('.', '..'):
                continue
            n = self._record_to_node(rec, name)
            flags = rec[25]
            if pend is not None and pend.name == name:           # continuation extent
                pend.extents.append(n.extents[0]); pend.size += n.size
                if not flags & 0x80: pend = None
                continue
            if flags & 0x80 and not n.is_dir:
                pend = n
            out.append(n)
        return out


# ----------------------------------------------------------------------------- UDF
class UDF:
    def __init__(self, src):
        self.src = src
        avdp = None
        for cand in (256, max(0, src.sectors - 257), max(0, src.sectors - 1)):
            b = src.read(cand)
            if struct.unpack('<H', b[:2])[0] == 2:
                avdp = b; break
        if avdp is None: raise DiscError('no UDF anchor volume descriptor pointer')
        vds_len, vds_loc = struct.unpack('<II', avdp[16:24])
        self.part_start = {}
        self.lb_size = 2048
        fsd_ad = None
        for i in range(max(1, vds_len // 2048)):
            d = src.read(vds_loc + i)
            tag = struct.unpack('<H', d[:2])[0]
            if tag == 5:      # partition descriptor
                pnum = struct.unpack('<H', d[22:24])[0]
                self.part_start[pnum] = struct.unpack('<I', d[188:192])[0]
            elif tag == 6:    # logical volume descriptor
                self.lb_size = struct.unpack('<I', d[212:216])[0] or 2048
                fsd_ad = d[248:264]
            elif tag == 8:
                break
        if fsd_ad is None or not self.part_start: raise DiscError('UDF volume descriptors incomplete')
        ext_len, lbn, pref = struct.unpack('<IIH', fsd_ad[:10])
        fsd = self._read_lb(pref, lbn)
        if struct.unpack('<H', fsd[:2])[0] != 256: raise DiscError('bad UDF file set descriptor')
        root_ad = fsd[400:416]
        rl, rlbn, rpref = struct.unpack('<IIH', root_ad[:10])
        self.root = self._icb_to_node('/', rlbn, rpref, force_dir=True)
        self.volume_id = ''

    def _read_lb(self, part, lbn, count=1):
        base = self.part_start.get(part, next(iter(self.part_start.values())))
        return self.src.read(base + lbn, count)

    def _extents(self, fe):
        tag = struct.unpack('<H', fe[:2])[0]
        flags = struct.unpack('<H', fe[34:36])[0] & 7
        info_len = struct.unpack('<Q', fe[56:64])[0]
        if tag == 261:
            l_ea, l_ad = struct.unpack('<II', fe[168:176]); ad0 = 176 + l_ea
        elif tag == 266:
            l_ea, l_ad = struct.unpack('<II', fe[208:216]); ad0 = 216 + l_ea
        else:
            raise DiscError('unexpected ICB tag %d' % tag)
        ads = fe[ad0:ad0 + l_ad]
        return flags, info_len, ads

    def _icb_to_node(self, name, lbn, part, force_dir=False):
        fe = self._read_lb(part, lbn)
        ftype = fe[27]                      # ICB tag file type
        is_dir = force_dir or ftype == 4
        flags, info_len, ads = self._extents(fe)
        node = Node(name, is_dir, info_len, [])
        node.mtime = None
        if flags == 3:                      # embedded
            node.extents = [('embedded', ads)]
        else:
            ext = []
            step = 8 if flags == 0 else 16
            for i in range(0, len(ads) - step + 1, step):
                raw_len, pos = struct.unpack('<II', ads[i:i + 8])
                etype, elen = raw_len >> 30, raw_len & 0x3FFFFFFF
                if elen == 0: break
                p = part
                if flags == 1: p = struct.unpack('<H', ads[i + 8:i + 10])[0]
                if etype == 3: continue     # continuation of AD chain: not supported
                base = self.part_start.get(p, next(iter(self.part_start.values())))
                ext.append((None if etype == 1 else base + pos * (self.lb_size // 2048), elen))
            node.extents = ext
        if is_dir: node.children_loader = lambda n=node, p=part: self._load_dir(n, p)
        return node

    def _file_bytes(self, node):
        out = []
        for lba, ln in node.extents:
            if lba == 'embedded': out.append(ln[:node.size])
            elif lba is None: out.append(b'\x00' * ln)
            else: out.append(self.src.read(lba, (ln + 2047) // 2048)[:ln])
        return b''.join(out)[:node.size]

    def _load_dir(self, node, part):
        data = self._file_bytes(node)
        pos, out = 0, []
        while pos + 38 <= len(data):
            if struct.unpack('<H', data[pos:pos + 2])[0] != 257: break
            chars = data[pos + 18]; l_fi = data[pos + 19]
            lbn_len, lbn, pref = struct.unpack('<IIH', data[pos + 20:pos + 30])
            l_iu = struct.unpack('<H', data[pos + 36:pos + 38])[0]
            fid = data[pos + 38 + l_iu: pos + 38 + l_iu + l_fi]
            total = (38 + l_iu + l_fi + 3) & ~3
            pos += total
            if chars & 0x08 or chars & 0x04 or l_fi == 0: continue     # parent / deleted
            if fid[:1] == b'\x10': name = fid[1:].decode('utf-16-be', 'replace')
            else: name = fid[1:].decode('latin-1')
            out.append(self._icb_to_node(name, lbn, pref, force_dir=bool(chars & 0x02)))
        return out



class SampleSource:
    """Reads a .sample file made by make_sample.py: the kept sectors of a big image; all other sectors read as 0."""
    def __init__(self, path):
        import json, zlib, bisect
        self.f = open(path, 'rb'); head = self.f.read(12)
        if head[:8] != b'KEMUSPL1': raise DiscError('not a KaraokeEmu sample file')
        n = struct.unpack('<I', head[8:12])[0]; self.index = json.loads(zlib.decompress(self.f.read(n)))
        self.data0 = 12 + n; self.total = self.index['total_sectors'] * 2048
        self.ss, self.off = 2048, 0
        self.runs = []; pos = 0
        for lba, cnt in self.index['runs']:
            self.runs.append((lba, cnt, pos)); pos += cnt
        self.starts = [r[0] for r in self.runs]; self._bisect = bisect
    sectors = property(lambda self: self.index['total_sectors'])
    is_xa = property(lambda self: False)
    def close(self): self.f.close()
    def _one(self, lba):
        i = self._bisect.bisect_right(self.starts, lba) - 1
        if i >= 0:
            s, c, p = self.runs[i]
            if lba < s + c:
                self.f.seek(self.data0 + (p + lba - s) * 2048); return self.f.read(2048)
        return bytes(2048)
    def read(self, lba, count=1): return b''.join(self._one(lba + k) for k in range(count))
    read_payload = read
    def _read_at(self, pos, n): return self.read(pos // 2048, (n + 2047) // 2048)[pos % 2048: pos % 2048 + n]
    def present(self, lba): return self._one(lba) is not None


# ----------------------------------------------------------------------------- public API
class Disc:
    def __init__(self, path):
        self.path = path
        self.src = SampleSource(path) if open(path, 'rb').read(8) == b'KEMUSPL1' else SectorSource(path)
        self.fs, self.kind, self.errors = None, None, []
        for cls, nm in ((ISO9660, 'ISO9660'), (UDF, 'UDF')):
            try:
                self.fs = cls(self.src); self.kind = nm
                if nm == 'ISO9660' and self.fs.joliet: self.kind = 'ISO9660+Joliet'
                if self._has(self.fs, 'VIDEO_TS') or self._count(self.fs) > 0: break
            except Exception as e:      # try the next filesystem
                self.errors.append('%s: %s' % (nm, e))
                self.fs = None
        if self.fs is None: raise DiscError('unrecognised disc image (%s)' % '; '.join(self.errors))
        # DVD ISOs are usually UDF with an empty ISO9660 bridge: prefer whichever actually has files
        if self.kind.startswith('ISO9660') and self._count(self.fs) == 0:
            try:
                alt = UDF(self.src)
                if self._count(alt) > 0: self.fs, self.kind = alt, 'UDF'
            except Exception as e:
                self.errors.append('UDF: %s' % e)
        self.volume_id = getattr(self.fs, 'volume_id', '')

    def close(self):
        self.src.close()

    @staticmethod
    def _count(fs):
        try: return len(fs.root.children_loader())
        except Exception: return 0

    @staticmethod
    def _has(fs, name):
        try: return any(n.name.upper() == name for n in fs.root.children_loader())
        except Exception: return False

    # ---- navigation
    def _children(self, node):
        if node.children_loader is None: return []
        cache = getattr(node, '_cache', None)
        return node.children_loader()

    def lookup(self, path):
        node = self.fs.root
        for part in _norm(path):
            if not node.is_dir: raise DiscError('not a directory: %s' % part)
            for c in node.children_loader():
                if c.name.upper() == part.upper():
                    node = c; break
            else:
                raise DiscError('not found: %s' % path)
        return node

    def listdir(self, path='/'):
        node = self.lookup(path)
        if not node.is_dir: raise DiscError('not a directory')
        return [dict(name=c.name, is_dir=c.is_dir, size=c.size) for c in
                sorted(node.children_loader(), key=lambda c: (not c.is_dir, c.name.upper()))]

    def walk(self, path='/'):
        node = self.lookup(path)
        base = '/' + '/'.join(_norm(path))
        def rec(n, p):
            for c in sorted(n.children_loader(), key=lambda c: (not c.is_dir, c.name.upper())):
                cp = (p.rstrip('/') + '/' + c.name)
                yield cp, c
                if c.is_dir: yield from rec(c, cp)
        yield from rec(node, base if base != '/' else '/')

    def iter_chunks(self, path, chunk=1 << 20, raw_xa=False):
        node = self.lookup(path)
        if node.is_dir: raise DiscError('is a directory')
        if isinstance(self.fs, UDF):
            data = self.fs._file_bytes(node)
            for i in range(0, len(data), chunk): yield data[i:i + chunk]
            return
        remaining_total = node.size
        for lba, ln in node.extents:
            nsec = (ln + 2047) // 2048
            done = 0
            while done < nsec:
                n = min(nsec - done, max(1, chunk // 2048))
                if raw_xa and self.src.is_xa: buf = self.src.read_payload(lba + done, n)
                else:
                    buf = self.src.read(lba + done, n)
                    take = min(len(buf), remaining_total)
                    buf = buf[:take]; remaining_total -= take
                yield buf
                done += n

    def read(self, path, offset=0, length=None, raw_xa=False):
        out, pos = [], 0
        for c in self.iter_chunks(path, raw_xa=raw_xa):
            end = pos + len(c)
            if end > offset:
                s = max(0, offset - pos)
                e = len(c) if length is None else min(len(c), offset + length - pos)
                if e > s: out.append(c[s:e])
            pos = end
            if length is not None and pos >= offset + length: break
        return b''.join(out)

    def extract(self, path, dest, raw_xa=False):
        with open(dest, 'wb') as f:
            for c in self.iter_chunks(path, raw_xa=raw_xa): f.write(c)

    # ---- disc identification
    def identify(self):
        try: names = {p.upper(): n for p, n in self.walk()}
        except Exception as e: return dict(type='unknown', note=str(e))
        up = list(names)
        info = dict(type='data disc')
        if '/VIDEO_TS/VIDEO_TS.IFO' in names:
            info = dict(type='DVD-Video', **self._dvd_info(names))
        elif '/SVCD/INFO.SVD' in names or '/MPEG2' in {u.rsplit('/', 1)[0] for u in up if u.count('/') >= 2}:
            info = dict(type='SVCD', tracks=sorted(u for u in up if u.startswith('/MPEG2/')))
        elif '/VCD/INFO.VCD' in names or any(u.startswith('/MPEGAV/') for u in up):
            info = dict(type='Video CD', tracks=sorted(u for u in up if u.startswith('/MPEGAV/') and not names[u].is_dir),
                        raw_sector_image=self.src.is_xa)
        else:
            ext = {}
            for u, n in names.items():
                if not n.is_dir: ext[os.path.splitext(u)[1].lower()] = ext.get(os.path.splitext(u)[1].lower(), 0) + 1
            kinds = []
            if any(e in ext for e in ('.mp3', '.wma', '.ogg', '.flac', '.wav')): kinds.append('audio files')
            if any(e in ext for e in ('.mpg', '.mpeg', '.avi', '.mp4', '.mkv', '.dat', '.vob', '.divx')): kinds.append('video files')
            if any(e in ext for e in ('.kar', '.mid', '.cdg')): kinds.append('karaoke data (.kar/.mid/.cdg)')
            info = dict(type='data disc', contains=kinds or None, extensions=ext)
        info['volume'] = self.volume_id
        info['filesystem'] = self.kind
        info['sector_layout'] = '%d-byte sectors, %d-byte header' % (self.src.ss, self.src.off)
        return info

    def _dvd_info(self, names):
        res = {}
        try:
            ifo = self.read('/VIDEO_TS/VIDEO_TS.IFO', 0, 4096 * 4)
            res['ifo_id'] = ifo[:12].decode('latin-1')
            res['title_sets'] = struct.unpack('>H', ifo[0x3E:0x40])[0]
            tt = struct.unpack('>I', ifo[0xC4:0xC8])[0]
            if tt and tt * 2048 + 2 <= len(ifo):
                res['titles'] = struct.unpack('>H', ifo[tt * 2048:tt * 2048 + 2])[0]
        except Exception as e:
            res['ifo_error'] = str(e)
        vts = []
        for u in sorted(names):
            if u.startswith('/VIDEO_TS/VTS_') and u.endswith('_0.IFO'):
                try:
                    ifo = self.read(u, 0, 2048 * 64)
                    pg = struct.unpack('>I', ifo[0xCC:0xD0])[0]
                    durs = []
                    if pg and pg * 2048 + 8 <= len(ifo):
                        base = pg * 2048
                        npgc = struct.unpack('>H', ifo[base:base + 2])[0]
                        for i in range(min(npgc, 32)):
                            off = struct.unpack('>I', ifo[base + 12 + 8 * i:base + 16 + 8 * i])[0]
                            t = ifo[base + off + 4:base + off + 8]
                            bcd = lambda b: (b >> 4) * 10 + (b & 15)
                            durs.append('%d:%02d:%02d' % (bcd(t[0]), bcd(t[1]), bcd(t[2])))
                    vobs = [v for v in names if v.startswith(u[:-6]) and v.endswith('.VOB') and not v.endswith('_0.VOB')]
                    vts.append(dict(ifo=u, pgc_play_times=durs, vob_files=sorted(vobs),
                                    vob_bytes=sum(names[v].size for v in vobs)))
                except Exception as e:
                    vts.append(dict(ifo=u, error=str(e)))
        res['vts'] = vts
        return res

    # ---- thumbnails (needs ffmpeg)
    def thumbnail(self, out_png, path=None, seek_mb=2):
        ff = find_ffmpeg()
        if not ff: raise DiscError('ffmpeg not found (put ffmpeg next to this program or in PATH)')
        if path is None:
            ident = self.identify()
            if ident['type'] == 'DVD-Video':
                vobs = [v for t in ident.get('vts', []) for v in t.get('vob_files', [])]
                if not vobs: raise DiscError('no VOB files')
                path = max(vobs, key=lambda v: self.lookup(v).size)
            elif ident['type'] in ('Video CD', 'SVCD'):
                path = ident['tracks'][0]
            else:
                vids = [p for p, n in self.walk() if not n.is_dir and os.path.splitext(p)[1].lower() in
                        ('.mpg', '.mpeg', '.avi', '.mp4', '.mkv', '.dat', '.vob', '.divx')]
                if not vids: raise DiscError('no video file found on disc')
                path = vids[0]
        raw = self.src.is_xa
        data = self.read(path, 0, seek_mb * 1024 * 1024, raw_xa=raw)
        p = subprocess.run([ff, '-v', 'error', '-y', '-f', 'mpeg' if (raw or path.upper().endswith(('.VOB', '.DAT', '.MPG'))) else 'mpegts',
                            '-i', 'pipe:0', '-frames:v', '1', out_png], input=data, capture_output=True)
        if p.returncode != 0 and not os.path.exists(out_png):
            p = subprocess.run([ff, '-v', 'error', '-y', '-i', 'pipe:0', '-frames:v', '1', out_png], input=data, capture_output=True)
        if not os.path.exists(out_png): raise DiscError('could not decode a frame: ' + p.stderr.decode('latin-1')[:200])
        return path


def find_ffmpeg():
    here = os.path.dirname(sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__))
    dirs = [here, os.getcwd()]
    if getattr(sys, '_MEIPASS', None): dirs.insert(0, sys._MEIPASS)        # ffmpeg bundled with --add-binary
    for d in dirs:
        for name in ('ffmpeg.exe', 'ffmpeg'):
            cand = os.path.join(d, name)
            if os.path.isfile(cand): return cand
    return shutil.which('ffmpeg')


def fmt_size(n):
    for u in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or u == 'GB': return '%d %s' % (n, u) if u == 'B' else '%.1f %s' % (n, u)
        n /= 1024


def main(argv):
    if len(argv) < 3:
        print(__doc__); return 2
    d = Disc(argv[1]); cmd = argv[2]
    if cmd == 'info':
        import json; print(json.dumps(d.identify(), indent=2, default=str))
    elif cmd == 'ls':
        for e in d.listdir(argv[3] if len(argv) > 3 else '/'):
            print('%-4s %12s  %s' % ('DIR' if e['is_dir'] else '', '' if e['is_dir'] else fmt_size(e['size']), e['name']))
    elif cmd == 'tree':
        for p, n in d.walk(): print(p + ('/' if n.is_dir else '   (%s)' % fmt_size(n.size)))
    elif cmd == 'extract':
        d.extract(argv[3], argv[4], raw_xa=d.src.is_xa and argv[3].upper().endswith('.DAT')); print('wrote', argv[4])
    elif cmd == 'thumb':
        print('thumbnail from', d.thumbnail(argv[3]))
    else:
        print('unknown command'); return 2
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
