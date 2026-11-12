"""Megapro karaoke song data: the MIDI songs inside KARA125/audio1.da1 (and friends).

Each song record (reconstructed from the firmware's song loader 0x080BA454):
  header (LE32 words): +0 offset of the data (usually 0), +4 record size, +8 MIDI size, +12 n (extra words = 3n),
                       +16 compressed size L, ... ; data at  +0 + (3n + 10) * 4
  data: a word-permutation scramble (0x080BACB0, key = L*153 % 84317, swaps = L/57 + 17) over the compressed
        bytes, then LZSS (0x080AFEA0): flag byte LSB first (1 = literal), reference = 2 bytes
        offset = b0 | (b1 & 0xF0) << 4, length = (b1 & 0x0F) + 3, 4 KB ring starting at 0xFEE, spaces.
  result: a Standard MIDI File (Roland GS).
The scramble's position generator uses the CPU's 16x16-bit multiplier; with a 32-bit multiply it goes wrong."""
import struct


def lzss_decode(src, out_len=None, fill=0x20, start=0xFEE):
    ring = bytearray([fill]) * 4096; r = start
    out = bytearray(); i = 0; n = len(src)
    while i < n and (out_len is None or len(out) < out_len):
        flags = src[i]; i += 1
        for bit in range(8):
            if i >= n or (out_len is not None and len(out) >= out_len): break
            if flags & (1 << bit):
                c = src[i]; i += 1
                out.append(c); ring[r] = c; r = (r + 1) & 0xFFF
            else:
                if i + 1 >= n: i = n; break
                b0, b1 = src[i], src[i + 1]; i += 2
                off = b0 | ((b1 & 0xF0) << 4); ln = (b1 & 0x0F) + 3
                for k in range(ln):
                    c = ring[(off + k) & 0xFFF]
                    out.append(c); ring[r] = c; r = (r + 1) & 0xFFF
    return bytes(out)


def midi_ok(m):
    """True if m parses as a Standard MIDI File (header + all track chunks)."""
    if m[:4] != b'MThd' or len(m) < 14: return False
    hl = struct.unpack('>I', m[4:8])[0]; ntrk = struct.unpack('>H', m[10:12])[0]
    p = 8 + hl
    for _ in range(ntrk):
        if m[p:p + 4] != b'MTrk': return False
        p += 8 + struct.unpack('>I', m[p + 4:p + 8])[0]
        if p > len(m): return False
    return True


def scan_songs(data, base=0):
    """Find compressed MIDI entries in a block of song-bank data. Yields (offset, header, midi_bytes)."""
    i = data.find(b'\xffMThd')
    while i >= 0:
        h = i - 40
        if h >= 0:
            csize, usize = struct.unpack('<II', data[h + 4:h + 12])
            m = lzss_decode(data[i:i + csize], usize)
            yield base + h, data[h:h + 40], m
        i = data.find(b'\xffMThd', i + 1)


M32 = 0xFFFFFFFF
def _mul16(a, b): return ((a & 0xFFFF) * (b & 0xFFFF)) & M32


def unscramble(buf, L):
    """The firmware's word-rotation scramble 0x080BACB0 over the first L bytes of buf (returns new bytes)."""
    key = (L * 153) % 84317; count = L // 57 + 17
    if L < 2048 or count < 100: return bytes(buf)     # the firmware leaves short records unscrambled
    return unscramble_kc(buf, key, count, L)


def scramble(buf, L):
    """Inverse of unscramble() (records shorter than 2048 bytes / fewer than 100 steps are stored as they are)."""
    key = (L * 153) % 84317; count = L // 57 + 17
    if L < 2048 or count < 100: return bytes(buf)
    return scramble_kc(buf, key, count, L)


def _chain(key, count, L):
    """Word positions visited by the 0x080BACB0 rotation (key, count, length in bytes)."""
    s3, s2, v1, mod = 1003, 1391, 0, (L >> 2) - 16
    out = []
    for i in range(count):
        a3 = _mul16(s2, s3 >> 16); t0 = _mul16(s2 >> 16, s3)
        v0 = (_mul16(s2, s3) + (((a3 + t0) & M32) << 16)) & M32
        s0 = (v0 + 7) & M32
        if i: s0 = (s0 + v1) & M32
        out.append(s0 % mod + 8); v1 = s0
        s2 = (s2 + key) & M32; s3 = (s3 + count) & M32
    return out


def scramble_kc(buf, key, count, L):
    """Inverse of unscramble_kc (what the disc authoring tool does)."""
    n = len(buf) // 4
    w = list(struct.unpack('<%dI' % n, bytes(buf[:n * 4])))
    P = _chain(key, count, L)
    last = w[P[-1]]
    for i in range(len(P) - 1, 0, -1): w[P[i]] = w[P[i - 1]]
    w[P[0]] = last
    return struct.pack('<%dI' % n, *w) + bytes(buf[n * 4:])


def unscramble_kc(buf, key, count, L):
    n = len(buf) // 4
    w = list(struct.unpack('<%dI' % n, bytes(buf[:n * 4])))
    s3, s2, v1, mod = 1003, 1391, 0, (L >> 2) - 16
    first = prev = None
    for i in range(count):
        a3 = _mul16(s2, s3 >> 16); t0 = _mul16(s2 >> 16, s3)
        v0 = (_mul16(s2, s3) + (((a3 + t0) & M32) << 16)) & M32
        s0 = (v0 + 7) & M32
        if i: s0 = (s0 + v1) & M32
        pos = s0 % mod + 8
        if i == 0: first = w[pos]
        else: w[prev] = w[pos]
        prev = pos; v1 = s0
        s2 = (s2 + key) & M32; s3 = (s3 + count) & M32
    w[prev] = first
    return struct.pack('<%dI' % n, *w) + bytes(buf[n * 4:])


def decode_record(rec):
    """rec = bytes of one song record (starting at its header). Returns the MIDI file bytes (or raises)."""
    off, rsize, usize, nextra, L = struct.unpack('<5I', rec[:20])
    start = off + (3 * nextra + 10) * 4
    data = unscramble(rec[start:start + L], L)
    m = lzss_decode(data, usize)
    if len(m) != usize or not midi_ok(m): raise ValueError('song record did not decode to a valid MIDI file')
    return m


def iter_records(read, size, start=0, limit=None):
    """Walk the records of a song bank file. read(offset, n) -> bytes. Yields (offset, header, record_bytes)."""
    pos = start; k = 0
    while pos + 40 <= size and (limit is None or k < limit):
        h = read(pos, 40)
        off, rsize, usize, nextra, L = struct.unpack('<5I', h[:20])
        if rsize == 0 or rsize > 16 << 20 or L == 0 or L > rsize:
            pos += 2048; continue                     # not a record header (padding): next sector
        yield pos, h, read(pos, rsize)
        pos += rsize; k += 1


def unscramble_audio(buf, key, size, count, start=0):
    """The firmware's sector scramble for recorded audio parts (0x080BAAF0(key, size, buf, start, [size, count])):
    in every full 2048-byte sector it rotates `count` words (positions 16..511) chosen by a 32-bit LCG.
    key = (size * 14) % 493815 (0x788F7), count = size % 7 + 21 (from the song loader 0x080BA454)."""
    b = bytearray(buf)
    first_sec = start >> 11; nsec = size >> 11
    limit = (size - 73) >> 11
    if first_sec + nsec > limit: nsec = limit - first_sec
    for k in range(nsec):
        sec = first_sec + k; base = k * 2048
        if base + 2048 > len(b): break
        w = list(struct.unpack_from('<512I', b, base))
        fp = 17 * sec; s5 = 3 * sec
        A = 1391; B = (1003 - s5) & M32; t0 = 0; prev = None; first = None
        for i in range(count):
            v0 = (A * B + fp) & M32
            s1 = (v0 + 7) & M32
            if i: s1 = (s1 + t0) & M32
            pos = s1 % 496 + 16
            if i == 0: first = w[pos]
            else: w[prev] = w[pos]
            A = (A + key) & M32; t0 = s1; prev = pos; B = (B + count) & M32
        w[prev] = first
        struct.pack_into('<512I', b, base, *w)
    return bytes(b)


def scramble_audio(buf, key, size, count, start=0):
    """Inverse of unscramble_audio (what the disc authoring tool does to a recorded audio part)."""
    b = bytearray(buf)
    first_sec = start >> 11; nsec = size >> 11
    limit = (size - 73) >> 11
    if first_sec + nsec > limit: nsec = limit - first_sec
    for k in range(max(0, nsec)):
        sec = first_sec + k; base = k * 2048
        if base + 2048 > len(b): break
        w = list(struct.unpack_from('<512I', b, base))
        fp = 17 * sec; s5 = 3 * sec
        A = 1391; B = (1003 - s5) & M32; t0 = 0; P = []
        for i in range(count):
            v0 = (A * B + fp) & M32
            s1 = (v0 + 7) & M32
            if i: s1 = (s1 + t0) & M32
            P.append(s1 % 496 + 16)
            A = (A + key) & M32; t0 = s1; B = (B + count) & M32
        last = w[P[-1]]
        for i in range(len(P) - 1, 0, -1): w[P[i]] = w[P[i - 1]]
        w[P[0]] = last
        struct.pack_into('<512I', b, base, *w)
    return bytes(b)


def part_key(size):
    """(key, count) of the audio part scramble for a part of `size` bytes (from the song loader 0x080BA454)."""
    return (size * 14) % 493815, size % 7 + 21


ASF_HEADER_GUID = bytes.fromhex('3026b2758e66cf11a6d900aa0062ce6c')


def record_parts(rec):
    """The extra parts of a song record (recorded audio, e.g. the chorus): [(offset, size, flags, bytes)]. Each part is
    returned descrambled, the way the firmware hands it to its decoder. The first (3n+10)*4 bytes of the record are
    its header; the data they cover was moved to the record's data offset, so it is put back first."""
    off, rsize, usize, n, L = struct.unpack('<5I', rec[:20])
    hdr = (3 * n + 10) * 4
    buf = bytearray(rec); buf[0:hdr] = rec[off:off + hdr]
    out = []
    for i in range(min(n, 32)):
        poff, psize, flags = struct.unpack_from('<3I', rec, 40 + 12 * i)
        size = psize & 0x0FFFFFFF
        key = (size * 14) % 493815; count = size % 7 + 21
        data = unscramble_audio(bytes(buf[poff:poff + size]), key, size, count, 0)
        out.append((poff, size, flags, data))
    return out


def lzss_encode(data, start=0xFEE, max_chain=64):
    """Okumura LZSS (4 KB ring starting at 0xFEE, 3..18-byte matches, 1 = literal flag bit) - the format
    lzss_decode() and the firmware's 0x080AFEA0 read. Greedy, with hash chains."""
    out = bytearray(); n = len(data); i = 0
    heads = {}
    MAXD = 4096 - 18
    while i < n:
        flag_pos = len(out); out.append(0); flags = 0
        for bit in range(8):
            if i >= n: break
            best_len = 0; best_pos = 0
            if i + 3 <= n:
                key = data[i:i + 3]; chain = heads.get(key)
                if chain:
                    lim = min(18, n - i); tries = 0
                    for p in reversed(chain):
                        if i - p > MAXD: break
                        l = 3
                        while l < lim and data[p + l] == data[i + l]: l += 1
                        if l > best_len: best_len = l; best_pos = p
                        if l == lim: break
                        tries += 1
                        if tries >= max_chain: break
            if best_len >= 3:
                off = (best_pos + start) & 0xFFF
                out.append(off & 0xFF); out.append(((off >> 4) & 0xF0) | (best_len - 3))
                step = best_len
            else:
                flags |= 1 << bit; out.append(data[i]); step = 1
            for k in range(i, min(i + step, n - 2)):
                heads.setdefault(data[k:k + 3], []).append(k)
            i += step
        out[flag_pos] = flags
    return bytes(out)
