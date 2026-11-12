#!/usr/bin/env python3
"""Megapro Song Manager - open a karaoke disc (ISO image or a folder), browse and search its songs, extract them
(MIDI + the recorded chorus clips), add new songs (MIDI files) and music videos (KARA125/MVV), delete songs, and
write a new disc (ISO or folder).
The original disc is never changed.

  KaraokeSongManager [--disc PATH] [--port N] [--no-browser]
"""
import os, sys, json, threading, secrets, argparse, webbrowser, urllib.parse, traceback, time, io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FROZEN = getattr(sys, 'frozen', False)
APPDIR = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APPDIR)
import songdisc, isoread, karamidi, midiout, mvlyrics
try:
    import sf2synth, songbank
except Exception as _ex:
    sf2synth = None; songbank = None; SYNTH_ERR = str(_ex)

VERSION = '1.6.0'
VIDEO_TYPES = '*.avi *.mvv *.mp4 *.mkv *.mov *.wmv *.mpg *.mpeg *.vob *.flv *.webm *.m4v *.3gp *.ts'


class App:
    def __init__(self):
        self.token = secrets.token_hex(16)
        self.disc = None; self.lock = threading.Lock()
        self.job = dict(state='idle'); self.preview = dict(state='idle')
        self.sf = songbank.default_soundfont(APPDIR, os.getcwd()) if songbank else None
        self.last_dir = os.path.expanduser('~')
        midiout.set_soundfont(self.sf)          # enables the built-in live SoundFont synth (the default device)
        real = [d for d, _ in midiout.devices() if d != 'capture']
        self.sound = 'live'; self.midi_dev = real[0] if real else 'capture'
        self.port = None; self.mplayer = None; self.now = None

    def get_port(self):
        if self.port is None or self.port.dev != self.midi_dev:
            if self.mplayer: self.mplayer.stop()
            if self.port is not None: self.port.close()
            try: self.port = midiout.Port(self.midi_dev)
            except Exception as ex:
                self.midi_dev = 'capture'; self.port = midiout.Port('capture')
                raise RuntimeError('could not start the sound device: %s' % ex)
            finally:
                self.mplayer = midiout.Player(self.port) if self.port else None
        return self.port

    def sound_status(self):
        st = self.mplayer.status() if self.mplayer else dict(state='idle')
        if self.now: st.update(number=self.now[0], artist=self.now[1])
        return dict(sound=self.sound, device=str(self.midi_dev), windows=midiout.IS_WIN,
                    devices=[dict(id=str(d), name=n) for d, n in midiout.devices()], player=st,
                    soundfont=os.path.basename(self.sf) if self.sf else '')

    def set_sound(self, sound=None, dev=None):
        if sound in ('live', 'render'): self.sound = 'live'          # all sound is live now
        if sound == 'render' and dev is None and any(d == 'sf2' for d, _ in midiout.devices()): dev = 'sf2'
        if dev is not None:
            ids = {str(d): d for d, _ in midiout.devices()}
            if str(dev) not in ids: raise ValueError('no such MIDI device')
            self.midi_dev = ids[str(dev)]
        if self.mplayer: self.mplayer.stop()
        return self.sound_status()

    def mplay(self, number, start=0.0):
        d = self.need(); s = d.find(int(number))
        if not s: raise KeyError('no song #%d' % int(number))
        if s.is_mv: raise ValueError('#%d is a music video - use "Open video"' % s.number)
        with self.lock: midi = d.midi(s)
        self.get_port(); self.mplayer.play(midi, s.title, float(start or 0)); self.now = (s.number, s.artist)
        return self.sound_status()

    def open(self, path):
        path = os.path.abspath(os.path.expanduser(path.strip().strip('"')))
        if not os.path.exists(path): raise FileNotFoundError(path)
        d = songdisc.KaraokeDisc(path)
        with self.lock:
            if self.disc: self.disc.close()
            self.disc = d
        self.last_dir = os.path.dirname(path)
        if songbank and not self.sf:
            self.sf = songbank.default_soundfont(os.path.dirname(path)); midiout.set_soundfont(self.sf)
            if self.sf and self.midi_dev != 'sf2' and not any(d not in ('capture',) for d, _ in midiout.devices() if d != 'sf2'):
                self.midi_dev = 'sf2'
        return self.info()

    def info(self):
        d = self.disc
        if not d: return dict(open=False)
        return dict(open=True, path=d.path, kind='folder' if os.path.isdir(d.path) else 'iso', songs=len(d.songs),
                    banks=[dict(name=p.split('/')[-1], size=s) for p, s in d.banks], pending=d.pending(),
                    iso_writable=getattr(d.src, 'plain', False), next_free=self.next_free(), soundfont=self.sf or '',
                    has_mvv=d.mvv_dir is not None, mvs=sum(1 for x in d.songs if x.is_mv),
                    mv_lyrics=sum(1 for x in d.songs + d.added if x.is_mv and x.lyr and not x.deleted),
                    ffmpeg=bool(isoread.find_ffmpeg()))

    def next_free(self, start=None):
        d = self.disc
        used = set(s.number for s in d.songs + d.added if not s.deleted)
        n = start or (max(used) + 1 if used else 1)
        if n > songdisc.MAX_NUMBER: n = 1
        while n in used and n < songdisc.MAX_NUMBER: n += 1
        return n

    def need(self):
        if not self.disc: raise RuntimeError('open a disc first')
        return self.disc

    # ---- actions
    def extract(self, numbers, folder):
        d = self.need(); folder = os.path.abspath(os.path.expanduser(folder.strip().strip('"')))
        return self._run_job('extract', lambda prog: self._extract(d, numbers, folder, prog))

    def _extract(self, d, numbers, folder, prog):
        files = []; errors = []
        for i, n in enumerate(numbers):
            s = d.find(int(n))
            if not s: errors.append('#%d not found' % n); continue
            try: files += d.extract(s, folder)
            except Exception as ex: errors.append('#%06d: %s' % (s.number, ex))
            prog((i + 1) / max(1, len(numbers)))
        return dict(folder=folder, files=len(files), errors=errors[:50])

    def save(self, out):
        d = self.need(); out = os.path.abspath(os.path.expanduser(out.strip().strip('"')))
        return self._run_job('save', lambda prog: dict(out=d.save(out, progress=prog)))

    def _run_job(self, kind, fn):
        if self.job.get('state') == 'running': raise RuntimeError('busy: ' + self.job.get('kind', ''))
        job = dict(state='running', kind=kind, progress=0.0, started=time.time()); self.job = job
        def work():
            try:
                r = fn(lambda f: job.__setitem__('progress', round(f, 3)))
                job.update(state='done', result=r, progress=1.0)
            except Exception as ex:
                job.update(state='error', error='%s: %s' % (type(ex).__name__, ex))
        threading.Thread(target=work, daemon=True).start()
        return job

    def add_files(self, items):
        d = self.need(); added = []; errors = []
        for it in items:
            try:
                p = os.path.abspath(os.path.expanduser(it['path'].strip().strip('"')))
                midi = open(p, 'rb').read()
                num = int(it.get('number') or self.next_free())
                ch = (it.get('chorus') or '').strip()
                wma = self._chorus_bytes(ch) if ch else None             # convert first: a bad file adds nothing
                s = d.add(num, midi, (it.get('title') or '').strip() or None, (it.get('artist') or '').strip() or None)
                if not it.get('title') and s.title.startswith('Song '): s.title = os.path.splitext(os.path.basename(p))[0][:80]
                if wma is not None:
                    try: d.set_chorus(s.number, wma, float(it.get('chorus_at') or 0))
                    except Exception:
                        d.added.remove(s); raise
                added.append(s.as_dict())
            except Exception as ex:
                errors.append('%s: %s' % (os.path.basename(it.get('path', '?')), ex))
        return dict(added=added, errors=errors, info=self.info())

    # ---- chorus (recorded voices)
    AUDIO_EXT = ('.wma', '.mp3', '.wav', '.ogg', '.m4a', '.aac', '.flac', '.opus')
    def _chorus_bytes(self, path):
        path = os.path.abspath(os.path.expanduser(str(path).strip().strip('"')))
        if not os.path.isfile(path): raise FileNotFoundError(path)
        return songdisc.convert_chorus(path)

    def chorus_info(self, number):
        d = self.need()
        with self.lock: return d.chorus_info(int(number))

    def set_chorus(self, number, path, at=None):
        d = self.need()
        wma = self._chorus_bytes(path) if path else None
        with self.lock: s = d.set_chorus(int(number), wma, None if at in (None, '') else float(at))
        return dict(song=s.as_dict(), chorus=d.chorus_info(s.number), info=self.info())

    def _chorus_for(self, midi_path):
        """An audio file next to a MIDI that is meant as its chorus: <name> chorus.*, <name>.chorus.* or <name>.*"""
        base = os.path.splitext(midi_path)[0]
        for suffix in (' chorus', '.chorus', '_chorus', ' - chorus', ''):
            for ext in self.AUDIO_EXT:
                q = base + suffix + ext
                if os.path.isfile(q): return q
        return ''

    def midi_meta(self, paths):
        out = []; n = self.next_free()
        used = set()
        for p in paths:
            p = os.path.abspath(os.path.expanduser(p.strip().strip('"')))
            try:
                m = open(p, 'rb').read()
                ok = karamidi.midi_ok(m); t, a = songdisc.midi_title(m) if ok else ('', '')
            except Exception as ex:
                ok, t, a = False, '', ''
            while n in used: n = self.next_free(n + 1)
            out.append(dict(path=p, ok=ok, title=t or os.path.splitext(os.path.basename(p))[0], artist=a, number=n,
                            chorus=self._chorus_for(p), chorus_at=0))
            used.add(n); n = self.next_free(n + 1)
        return out

    # ---- music videos
    def next_free_mv(self):
        d = self.need()
        mv = [s.number for s in d.songs + d.added if s.is_mv and not s.deleted]
        return self.next_free(max(mv) + 1 if mv else None)

    def video_meta(self, paths):
        d = self.need(); ff = isoread.find_ffmpeg(); out = []; used = set()
        n = self.next_free_mv()
        for p in paths:
            p = os.path.abspath(os.path.expanduser(p.strip().strip('"')))
            it = dict(path=p, ok=os.path.isfile(p), size=os.path.getsize(p) if os.path.isfile(p) else 0)
            if it['ok']:
                try: it['as_is'] = songdisc.mvv_compatible(p)
                except Exception: it['as_is'] = False
                it['convert'] = not it['as_is']
                if it['convert'] and not ff: it.update(ok=False, problem='needs ffmpeg to convert (put ffmpeg.exe next to the program)')
                dur = songdisc.video_duration(p, ff) if ff else None
                if ff and dur is None: it.update(ok=False, problem='not a video file ffmpeg can read')
                it['duration'] = dur
            else:
                it['problem'] = 'file not found'
            base = os.path.splitext(os.path.basename(p))[0]
            base = __import__('re').sub(r'^\d{4,6}\s*[-_.]?\s*', '', base).replace('_', ' ').strip()
            if ' - ' in base: artist, title = [x.strip() for x in base.split(' - ', 1)]
            else: artist, title = '', base
            while n in used: n = self.next_free(n + 1)
            it.update(title=title[:80] or 'Video', artist=artist[:80], number=n, lyrics='')
            for ext in ('.kar', '.mid', '.midi', '.KAR', '.MID'):                 # lyrics file next to the video
                cand = os.path.splitext(p)[0] + ext
                if os.path.isfile(cand): it['lyrics'] = cand; break
            used.add(n); n = self.next_free(n + 1)
            out.append(it)
        return out

    def add_videos(self, items):
        d = self.need()
        if d.mvv_dir is None: raise RuntimeError('this disc has no KARA125/MVV folder, so its player software has no music videos')
        import tempfile
        work = tempfile.mkdtemp(prefix='songmgr_mvv_')
        def run(prog):
            added = []; errors = []; n = max(1, len(items))
            for i, it in enumerate(items):
                try:
                    title = (it.get('title') or '').strip() or None; artist = (it.get('artist') or '').strip()
                    lyr = self.lyrics_midi(it['lyrics'], title or '', artist, it.get('offset', 0), it.get('keep_music')) if (it.get('lyrics') or '').strip() else None
                    s = d.add_mvv(int(it['number']), os.path.abspath(it['path']), title, artist, workdir=work,
                                  progress=lambda f, i=i: prog((i + f) / n), lyrics=lyr)
                    added.append(dict(s.as_dict(), converted=s.converted))
                except Exception as ex:
                    errors.append('%s: %s' % (os.path.basename(it.get('path', '?')), ex))
                prog((i + 1) / n)
            return dict(added=len(added), converted=sum(1 for a in added if a['converted']), errors=errors)
        return self._run_job('addmv', run)

    def lyrics_midi(self, path, title, artist, offset=0, keep_music=False):
        path = os.path.abspath(os.path.expanduser(str(path).strip().strip('"')))
        midi = open(path, 'rb').read()
        return mvlyrics.build(midi, title, artist, float(offset or 0), bool(keep_music))

    def lyrics_info(self, path):
        path = os.path.abspath(os.path.expanduser(str(path).strip().strip('"')))
        if not os.path.isfile(path): raise FileNotFoundError(path)
        inf = mvlyrics.info(open(path, 'rb').read())
        if not inf['syllables']: raise ValueError('no lyrics in this MIDI file (it needs lyric or .kar text events)')
        inf['path'] = path
        return inf

    def set_lyrics(self, number, path, offset=0, keep_music=False):
        d = self.need(); s = d.find(int(number))
        if not s or not s.is_mv: raise KeyError('#%s is not a music video' % number)
        lyr = self.lyrics_midi(path, s.title, s.artist, offset, keep_music) if path else None
        with self.lock: d.set_lyrics(s.number, lyr)
        return dict(song=s.as_dict(), info=self.info())

    def mv_lyrics_text(self, number):
        d = self.need(); s = d.find(int(number))
        with self.lock: m = d.midi(s)
        inf = mvlyrics.info(m)
        ls = mvlyrics.lines_of(mvlyrics.read(m)[0])
        lines = [dict(t=round(l[0][0], 1), text=tx) for l, tx in zip(ls, mvlyrics.text_of(ls))]
        return dict(number=s.number, title=s.title, lines=lines, notes=inf['notes'])

    def open_video(self, number):
        """Copy a music video to a temporary .avi and open it in the computer's video player."""
        d = self.need(); s = d.find(int(number))
        if not s or not s.is_mv: raise KeyError('#%s is not a music video' % number)
        import tempfile
        folder = os.path.join(tempfile.gettempdir(), 'songmgr_preview'); os.makedirs(folder, exist_ok=True)
        with self.lock: files = d.extract_mv(s, folder)
        avi = [f for f in files if f.endswith('.avi')][0]
        if hasattr(os, 'startfile'):
            os.startfile(avi); opened = True
        else:
            opened = False
        return dict(path=avi, opened=opened)

    def play(self, number):
        d = self.need()
        if not sf2synth: raise RuntimeError('preview needs numpy (' + SYNTH_ERR + ')')
        if not self.sf: raise RuntimeError('no SoundFont: put dreamsam_2695.sf2 (or any .sf2) next to the program')
        if self.preview.get('state') == 'rendering': raise RuntimeError('already rendering')
        s = d.find(int(number))
        if s.is_mv: raise ValueError('#%d is a music video - use "Open video"' % s.number)
        midi = d.midi(s)
        p = dict(state='rendering', number=s.number, title=s.title, artist=s.artist, progress=0); self.preview = p
        def work():
            try:
                pcm = sf2synth.render(midi, songbank.soundfont(self.sf), progress=lambda f: p.__setitem__('progress', round(f, 2)))
                p['wav'] = sf2synth.wav_bytes(pcm); p['state'] = 'ready'
            except Exception as ex:
                p.update(state='error', error=str(ex))
        threading.Thread(target=work, daemon=True).start()
        return {k: v for k, v in p.items() if k != 'wav'}

    def chorus_wav(self, number, idx):
        d = self.need(); s = d.find(int(number)); clips = d.chorus(s)
        w = clips[int(idx)]
        ff = isoread.find_ffmpeg()
        if not ff: return w, 'audio/x-ms-wma'
        import subprocess, tempfile
        fd, tmp = tempfile.mkstemp(suffix='.wma'); os.write(fd, w); os.close(fd)
        try:
            r = subprocess.run([ff, '-v', 'error', '-i', tmp, '-f', 'wav', 'pipe:1'], capture_output=True)
            return (r.stdout, 'audio/wav') if r.returncode == 0 else (w, 'audio/x-ms-wma')
        finally:
            os.unlink(tmp)


def pick(kind, start):
    """Native file/folder dialog (tkinter)."""
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)
    try:
        if kind == 'iso': r = filedialog.askopenfilename(initialdir=start, title='Open karaoke disc image', filetypes=[('Disc image', '*.iso *.img'), ('All files', '*.*')])
        elif kind == 'folder': r = filedialog.askdirectory(initialdir=start, title='Folder (a disc folder with KARA125, or where to save)')
        elif kind == 'midi': r = list(filedialog.askopenfilenames(initialdir=start, title='MIDI songs to add', filetypes=[('MIDI', '*.mid *.midi *.kar'), ('All files', '*.*')]))
        elif kind == 'lyrics': r = filedialog.askopenfilename(initialdir=start, title='Lyrics: a karaoke MIDI (.kar / .mid with lyrics)', filetypes=[('Karaoke MIDI', '*.kar *.mid *.midi'), ('All files', '*.*')])
        elif kind == 'chorus': r = filedialog.askopenfilename(initialdir=start, title='Chorus: recorded voices for the song (audio file)', filetypes=[('Audio', '*.wma *.mp3 *.wav *.ogg *.m4a *.aac *.flac *.opus'), ('All files', '*.*')])
        elif kind == 'video': r = list(filedialog.askopenfilenames(initialdir=start, title='Music videos to add', filetypes=[('Video', VIDEO_TYPES), ('All files', '*.*')]))
        elif kind == 'saveiso': r = filedialog.asksaveasfilename(initialdir=start, title='Save new disc image as', defaultextension='.iso', filetypes=[('Disc image', '*.iso')])
        else: r = ''
    finally:
        root.destroy()
    return r


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Megapro Song Manager</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0f1218;--card:#171c26;--ink:#e9edf5;--mut:#8f99ad;--acc:#ff4fa3;--ok:#3ddc97;--bad:#ff6b6b;--line:#2a3142;--sel:#2b2140}
@media (prefers-color-scheme:light){:root{--bg:#f4f5f9;--card:#fff;--ink:#1b2130;--mut:#667089;--acc:#d1287a;--ok:#12805a;--bad:#c62828;--line:#dde2ec;--sel:#fbe7f1}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14.5px/1.45 system-ui,Segoe UI,sans-serif}
header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:center;flex-wrap:wrap}
h1{margin:0;font-size:19px}h1 b{color:var(--acc)}header .mut{font-size:12.5px}
main{padding:16px 20px;max-width:1280px;margin:auto;display:grid;gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
h2{margin:0 0 8px;font-size:13px;letter-spacing:.05em;text-transform:uppercase;color:var(--mut)}
select,input[type=text],input[type=number]{padding:8px 10px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--ink);font:inherit}
button{padding:8px 13px;border-radius:8px;border:1px solid var(--line);background:var(--acc);color:#fff;font-weight:600;cursor:pointer;font:inherit;font-weight:600}
button.sec{background:transparent;color:var(--ink)}button:disabled{opacity:.45;cursor:default}button.danger{background:var(--bad)}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:6px 0}.grow{flex:1;min-width:220px}
table{width:100%;border-collapse:collapse;font-size:14px}th{text-align:left;color:var(--mut);font-weight:500;font-size:12.5px;padding:6px;border-bottom:1px solid var(--line)}
td{padding:5px 6px;border-bottom:1px solid var(--line)}tr.s td{background:var(--sel)}tr.del td{opacity:.45;text-decoration:line-through}tr.add td{color:var(--ok)}
td.num{font-variant-numeric:tabular-nums;font-family:ui-monospace,Consolas,monospace}
.pill{display:inline-block;padding:0 8px;border-radius:99px;font-size:11.5px;border:1px solid var(--line)}.ok{color:var(--ok)}.bad{color:var(--bad)}.mut{color:var(--mut)}
.bar{height:7px;border-radius:5px;background:var(--bg);border:1px solid var(--line);overflow:hidden;flex:1;min-width:120px}.bar i{display:block;height:100%;background:var(--acc);width:0}
.note{font-size:12.5px;color:var(--mut)}#msg{min-height:20px}
dialog{border:1px solid var(--line);border-radius:12px;background:var(--card);color:var(--ink);max-width:900px;width:95%}
</style></head><body>
<header><h1>Megapro <b>Song Manager</b></h1><span class="mut">v__VERSION__ · open a disc, extract, add songs and music videos, delete, save a new disc · the original is never changed</span></header>
<main>
<section class="card"><h2>Disc</h2>
 <div class="row"><input type="text" id="path" class="grow" placeholder="disc image (.iso) or a disc folder (containing KARA125)">
  <button class="sec" id="pickiso">ISO…</button><button class="sec" id="pickfolder">Folder…</button><button id="open">Open</button></div>
 <div id="info" class="mut">No disc open.</div>
</section>
<section class="card"><h2>Songs</h2>
 <div class="row"><input type="text" id="q" class="grow" placeholder="search number, title or artist"><select id="only"><option value="">All</option><option value="songs">Karaoke songs</option><option value="mv">Music videos</option></select><button class="sec" id="prev">◀</button><button class="sec" id="next">▶</button><span class="mut" id="range"></span></div>
 <div class="row"><button class="sec" id="selall">Select page</button><button class="sec" id="selnone">Clear selection</button><span class="mut" id="selinfo">0 selected</span>
  <span class="grow"></span><button id="ext" disabled>Extract selected…</button><button class="danger" id="del" disabled>Delete selected</button><button class="sec" id="undel" disabled>Undo delete</button><button id="add">Add songs…</button><button id="addmv">Add music videos…</button></div>
 <table><thead><tr><th style="width:28px"></th><th style="width:80px">No.</th><th>Title</th><th>Artist</th><th style="width:90px">Type</th><th style="width:190px"></th></tr></thead><tbody id="tb"></tbody></table>
 <div class="row"><label>Play with <select id="sound"></select></label><span class="mut" id="soundinfo"></span></div>
 <div class="row" id="mbar" style="display:none"><button class="sec" id="mpause">⏯</button><button class="sec" id="mstop">■</button><b id="mtitle"></b><span class="mut" id="martist"></span>
  <input type="range" id="mpos" min="0" max="100" value="0" step="0.1" style="flex:1;min-width:160px"><span class="mut" id="mtime"></span></div>
 <div id="player" class="row"></div>
</section>
<section class="card"><h2>Save</h2>
 <div class="row"><span id="pend" class="mut">No changes.</span></div>
 <div class="row"><input type="text" id="out" class="grow" placeholder="new disc: C:\Discs\MPPV45 NEW.iso  (or a new empty folder)"><button class="sec" id="pickout">Save as ISO…</button><button class="sec" id="pickoutf">Folder…</button><button id="save">Save new disc</button></div>
 <div class="row"><div class="bar"><i id="prog"></i></div><span id="jobst" class="mut"></span></div>
 <div class="note">An ISO keeps every file and sector of the original (including the disc's karaoke signature); added songs go into a new song bank file KARA125\AUDIOn.DAn and the song list in title.bnk / title.hdr is rewritten. Deleted songs are removed from the song list (their data stays on the disc unused). Saving as a folder writes all disc files; burn it with a tool that keeps the original disc's boot area, or prefer ISO.</div>
</section>
<div id="msg"></div>
</main>
<dialog id="mvdlg"><h2>Add music videos</h2>
 <div class="note">Videos go into KARA125\MVV as &lt;number&gt;.mvv with an entry in the song list, like the disc's own music videos (enter the number on the remote to play). AVI files with DivX/Xvid video and MP3 sound are copied as they are; anything else (MP4, MKV, MOV, WMV, MPG…) is converted with ffmpeg to that format (up to 720×480, MP3 sound). Titles come from the file name ("Artist - Title").</div>
 <div class="row"><input type="text" id="mvpaths" class="grow" placeholder="video file paths, separated by ;"><button class="sec" id="pickmv">Choose videos…</button><button class="sec" id="loadmv">Read</button></div>
 <table><thead><tr><th>No.</th><th>Title</th><th>Artist</th><th>File</th><th>How</th><th>Lyrics (optional .kar / .mid)</th></tr></thead><tbody id="mvtb"></tbody></table>
 <div class="row"><label>Lyrics offset <input type="number" id="mvoff" value="0" step="0.1" style="width:80px"> s</label>  <span class="note">A lyrics file with the same name as the video is picked up automatically.</span></div>
 <div class="row"><span class="grow"></span><button class="sec" id="mvcancel">Cancel</button><button id="mvok">Add to disc</button></div>
</dialog>
<dialog id="lyrdlg"><h2>Music video lyrics</h2>
 <div class="row"><b id="lyrtitle"></b></div>
 <div id="lyrcur" class="note"></div>
 <div class="note">Choose a karaoke MIDI with lyrics: a .kar file, a Megapro song MIDI (extract one from the song list), or any MIDI with lyric events. Its lyrics are stored with the video and shown over it, timed from the start of the video. Use the offset if they come too early (+) or too late (−).</div>
 <div class="row"><input type="text" id="lyrpath" class="grow" placeholder="lyrics file (.kar / .mid)"><button class="sec" id="lyrpick">Choose…</button><button class="sec" id="lyrread">Read</button></div>
 <div id="lyrinfo" class="note"></div>
 <div class="row"><label>Offset <input type="number" id="lyroff" value="0" step="0.1" style="width:80px"> s</label><span class="note">Stored like the disc's own music videos: lyrics with wipe markers and count-in, no music.</span></div>
 <div class="row"><button class="danger" id="lyrdel">Remove lyrics</button><span class="grow"></span><button class="sec" id="lyrcancel">Cancel</button><button id="lyrok">Use these lyrics</button></div>
</dialog>
<dialog id="chdlg"><h2>Chorus (recorded voices)</h2>
 <div class="row"><b id="chtitle"></b></div>
 <div id="chcur" class="note"></div>
 <div class="note">Some songs on the disc come with recorded voices (backing vocals, a choir) that play along with the music. Choose an audio file (WMA, MP3, WAV, OGG, M4A, FLAC…); it is converted to the disc's format (WMA, 48 kbps) and starts at the time below, counted from the start of the song.</div>
 <div class="row"><input type="text" id="chpath" class="grow" placeholder="audio file"><button class="sec" id="chpick">Choose…</button></div>
 <div class="row"><label>Starts at <input type="number" id="chat" value="0" step="0.05" min="0" style="width:90px"> s into the song</label><span class="note" id="chathint"></span></div>
 <div class="row"><button class="danger" id="chdel">Remove chorus</button><span class="grow"></span><button class="sec" id="chcancel">Cancel</button><button id="chok">Use this chorus</button></div>
</dialog>
<dialog id="adddlg"><h2>Add songs</h2>
 <div class="note">Choose MIDI files. Numbers are suggested from the next free number; titles come from the file (Megapro-style @Title$3Artist text or the track name). Edit before adding.</div>
 <div class="row"><input type="text" id="midipaths" class="grow" placeholder="MIDI file paths, separated by ;"><button class="sec" id="pickmidi">Choose MIDI files…</button><button class="sec" id="loadmidi">Read</button></div>
 <table><thead><tr><th>No.</th><th>Title</th><th>Artist</th><th>File</th><th>Chorus (optional)</th></tr></thead><tbody id="addtb"></tbody></table>
 <div class="note">Chorus: an audio file of recorded voices to play along (an audio file named like the MIDI, or "&lt;name&gt; chorus", is picked automatically), and when it starts (seconds into the song).</div>
 <div class="row"><span class="grow"></span><button class="sec" id="addcancel">Cancel</button><button id="addok">Add to disc</button></div>
</dialog>
<footer style="text-align:center;color:var(--mut);font-size:12px;padding:18px 12px 22px;border-top:1px solid var(--line);margin-top:18px">Megapro Song Manager by <b style="color:var(--ink)">ChiakiMK</b> · built with Claude (Anthropic)</footer>
<script>
const TOKEN="__TOKEN__";const $=id=>document.getElementById(id);
async function api(path,body){const o={headers:{'X-Token':TOKEN}};if(body!==undefined){o.method='POST';o.body=JSON.stringify(body);o.headers['Content-Type']='application/json'}
 const r=await fetch(path,o);const t=await r.text();let j;try{j=JSON.parse(t)}catch(e){j={error:t}}if(!r.ok&&!j.error)j.error='HTTP '+r.status;return j}
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function fmt(n){const u=['B','KB','MB','GB'];let i=0;while(n>=1024&&i<3){n/=1024;i++}return (i?n.toFixed(1):n)+' '+u[i]}
function msg(t,bad){$('msg').innerHTML=t?'<span class="'+(bad?'bad':'ok')+'">'+esc(t)+'</span>':''}
let start=0, sel=new Set(), rows=[], info={};
function showInfo(i){info=i;if(!i.open){$('info').textContent='No disc open.';return}
 $('info').innerHTML='<b>'+esc(i.path)+'</b> · '+i.kind+' · '+i.songs.toLocaleString()+' entries ('+(i.mvs||0)+' music videos, '+(i.mv_lyrics||0)+' with lyrics'+(i.has_mvv?'':', no MVV folder')+') · banks: '+i.banks.map(b=>esc(b.name)+' ('+fmt(b.size)+')').join(', ')+(i.iso_writable?'':' · <span class="mut">(ISO output needs a plain .iso source; folder output always works)</span>');
 const p=i.pending||{};$('pend').innerHTML=(p.added||p.deleted)?('<b>'+p.added+'</b> to add'+(p.added_mv?' ('+p.added_mv+' music video'+(p.added_mv>1?'s':'')+', '+fmt(p.mv_bytes)+')':'')+', <b>'+p.deleted+'</b> to delete'+(p.lyrics?', lyrics changed on <b>'+p.lyrics+'</b> music video(s)':'')+(p.chorus?', chorus changed on <b>'+p.chorus+'</b> song(s)':'')+'.'):((p.lyrics||p.chorus)?((p.lyrics?'Lyrics changed on <b>'+p.lyrics+'</b> music video(s). ':'')+(p.chorus?'Chorus changed on <b>'+p.chorus+'</b> song(s).':'')):'No changes.');
 $('addmv').disabled=!i.has_mvv;$('addmv').title=i.has_mvv?'':'this disc has no KARA125/MVV folder';
 if(!$('out').value&&i.path){$('out').value=i.kind==='iso'?i.path.replace(/(\.iso)?$/i,' (edited).iso'):i.path+' (edited)'}}
async function list(){if(!info.open)return;const r=await api('/api/songs?start='+start+'&only='+$('only').value+'&q='+encodeURIComponent($('q').value));if(r.error){msg(r.error,1);return}
 rows=r.songs;$('range').textContent=r.total?((r.start+1)+'–'+(r.start+r.songs.length)+' of '+r.total.toLocaleString()):'no matches';
 $('tb').innerHTML=r.songs.map(s=>'<tr data-n="'+s.number+'" class="'+(sel.has(s.number)?'s ':'')+(s.deleted?'del ':'')+(s.origin==='added'?'add':'')+'"><td><input type="checkbox" '+(sel.has(s.number)?'checked':'')+'></td><td class="num">'+String(s.number).padStart(6,'0')+'</td><td>'+esc(s.title)+(s.origin==='added'?' <span class="pill ok">new</span>':'')+'</td><td class="mut">'+esc(s.artist)+'</td><td>'+(s.mv?'<span class="pill" title="'+esc((s.mvv||'video file missing')+(s.mvv_size?' · '+fmt(s.mvv_size):''))+'">MV'+(s.mvv?'':' ?')+'</span>'+(s.lyrics?' <span class="pill ok" title="lyrics are shown over the video">lyrics</span>':'')+(s.lyrics_changed?' <span class="pill ok">'+(s.lyrics?'new':'removed')+'</span>':''):'')+(s.chorus?'<span class="pill">chorus</span>':'')+(s.chorus_changed?' <span class="pill ok">'+(s.chorus?'new':'removed')+'</span>':'')+'</td><td>'+(s.deleted?'<span class="pill bad">deleted</span>':s.mv?((s.mvv?'<button class="sec vid" data-n="'+s.number+'">▶ Video</button> ':'')+'<button class="sec lyr" data-n="'+s.number+'">Lyrics…</button>'):'<button class="sec play" data-n="'+s.number+'">▶</button>'+(s.chorus?' <button class="sec ch" data-n="'+s.number+'">♪ chorus</button>':'')+' <button class="sec chs" data-n="'+s.number+'">'+(s.chorus?'Chorus…':'Add chorus…')+'</button>')+'</td></tr>').join('');
 document.querySelectorAll('#tb tr').forEach(tr=>{const n=+tr.dataset.n;tr.querySelector('input').onchange=e=>{e.target.checked?sel.add(n):sel.delete(n);tr.classList.toggle('s',e.target.checked);selUpd()}});
 document.querySelectorAll('button.play').forEach(b=>b.onclick=async()=>{
   {const r=await api('/api/mplay',{number:+b.dataset.n});if(r.error)msg(r.error,1);else{showSound(r);pollSound()}return}
   const r=await api('/api/play',{number:+b.dataset.n});if(r.error)msg(r.error,1);else pollPlay()});
 document.querySelectorAll('button.lyr').forEach(b=>b.onclick=()=>openLyrics(+b.dataset.n,rows.find(r=>r.number===+b.dataset.n)));
 document.querySelectorAll('button.vid').forEach(b=>b.onclick=async()=>{msg('opening video…');const r=await api('/api/openvideo',{number:+b.dataset.n});if(r.error)msg(r.error,1);else msg(r.opened?'opened in your video player':'saved to '+r.path)});
 document.querySelectorAll('button.ch').forEach(b=>b.onclick=async()=>{const r=await fetch('/api/chorus?n='+b.dataset.n+'&i=0',{headers:{'X-Token':TOKEN}});if(!r.ok){msg(await r.text(),1);return}
   const u=URL.createObjectURL(await r.blob());$('player').innerHTML='<span class="mut">chorus of #'+b.dataset.n+'</span><audio controls autoplay src="'+u+'" style="flex:1"></audio>'});
 document.querySelectorAll('button.chs').forEach(b=>b.onclick=()=>openChorus(+b.dataset.n,rows.find(r=>r.number===+b.dataset.n)));
 selUpd()}
let chNum=0;
async function openChorus(n,row){chNum=n;$('chtitle').textContent=String(n).padStart(6,'0')+'  '+(row?row.title:'');$('chpath').value='';$('chcur').textContent='reading…';$('chathint').textContent='';$('chdlg').showModal();
 const r=await api('/api/chorusinfo',{number:n});if(r.error){$('chcur').textContent=r.error;return}
 $('chcur').textContent=r.chorus?('This song has a chorus'+(r.clips>1?' ('+r.clips+' clips)':'')+(r.at!=null?', starting at '+r.at.toFixed(2)+' s':'')+(r.changed?' (changed, saved with the new disc)':'')+'. A new file replaces it.'):'No chorus yet.';
 $('chat').value=r.at!=null?r.at.toFixed(2):'0';$('chathint').textContent=r.at!=null?'(the current chorus starts at '+r.at.toFixed(2)+' s)':'';$('chdel').style.display=r.chorus?'':'none'}
$('chpick').onclick=()=>pickInto('chorus',$('chpath'));$('chcancel').onclick=()=>$('chdlg').close();
$('chok').onclick=async()=>{const p=$('chpath').value.trim();if(!p){alert('choose an audio file');return}msg('converting the chorus…');
 const r=await api('/api/setchorus',{number:chNum,path:p,at:+$('chat').value||0});if(r.error){alert(r.error);msg('');return}
 $('chdlg').close();msg('chorus set for #'+chNum+' (saved with the new disc)');showInfo(r.info);list()};
$('chdel').onclick=async()=>{const r=await api('/api/setchorus',{number:chNum,path:null});if(r.error){alert(r.error);return}$('chdlg').close();msg('chorus removed from #'+chNum);showInfo(r.info);list()};
function selUpd(){$('selinfo').textContent=sel.size+' selected';$('ext').disabled=$('del').disabled=$('undel').disabled=!sel.size}
let playTimer=null;async function pollPlay(){clearTimeout(playTimer);const p=await api('/api/preview');
 if(p.state==='rendering'){$('player').innerHTML='<span class="mut">rendering #'+p.number+' '+esc(p.title)+' … '+Math.round(100*(p.progress||0))+'%</span>';playTimer=setTimeout(pollPlay,700)}
 else if(p.state==='ready'){const r=await fetch('/api/preview.wav',{headers:{'X-Token':TOKEN}});const u=URL.createObjectURL(await r.blob());
   $('player').innerHTML='<b>#'+p.number+' '+esc(p.title)+'</b> <span class="mut">'+esc(p.artist)+'</span><audio controls autoplay src="'+u+'" style="flex:1"></audio>'}
 else if(p.state==='error')$('player').innerHTML='<span class="bad">'+esc(p.error)+'</span>'}
async function openDisc(){msg('opening…');const r=await api('/api/open',{path:$('path').value});if(r.error){msg(r.error,1);return}msg('');sel.clear();start=0;showInfo(r);list()}
async function pickInto(kind,el){const r=await api('/api/pick',{kind});if(r.error){msg(r.error,1);return null}if(r.path&&el)el.value=Array.isArray(r.path)?r.path.join(';'):r.path;return r.path}
$('pickiso').onclick=async()=>{if(await pickInto('iso',$('path')))openDisc()};
$('pickfolder').onclick=async()=>{if(await pickInto('folder',$('path')))openDisc()};
$('open').onclick=openDisc;$('path').onkeydown=e=>{if(e.key==='Enter')openDisc()};
$('only').onchange=()=>{start=0;list()};
let qt=null;$('q').oninput=()=>{clearTimeout(qt);qt=setTimeout(()=>{start=0;list()},250)};
$('prev').onclick=()=>{start=Math.max(0,start-100);list()};$('next').onclick=()=>{start+=100;list()};
$('selall').onclick=()=>{rows.forEach(s=>sel.add(s.number));list()};$('selnone').onclick=()=>{sel.clear();list()};
$('del').onclick=async()=>{const r=await api('/api/delete',{numbers:[...sel]});if(r.error)msg(r.error,1);else{msg(r.done+' song(s) marked for deletion');sel.clear();showInfo(r.info);list()}};
$('undel').onclick=async()=>{const r=await api('/api/undelete',{numbers:[...sel]});if(r.error)msg(r.error,1);else{showInfo(r.info);list()}};
$('ext').onclick=async()=>{const p=await pickInto('folder',null);if(!p)return;const r=await api('/api/extract',{numbers:[...sel],folder:p});if(r.error)msg(r.error,1);else pollJob()};
$('pickout').onclick=()=>pickInto('saveiso',$('out'));$('pickoutf').onclick=()=>pickInto('folder',$('out'));
$('save').onclick=async()=>{const r=await api('/api/save',{out:$('out').value});if(r.error)msg(r.error,1);else pollJob()};
let jobTimer=null;async function pollJob(){clearTimeout(jobTimer);const j=await api('/api/job');$('prog').style.width=Math.round(100*(j.progress||0))+'%';
 if(j.state==='running'){$('jobst').textContent=({addmv:'adding music videos',save:'saving',extract:'extracting'}[j.kind]||j.kind)+' … '+Math.round(100*(j.progress||0))+'%';jobTimer=setTimeout(pollJob,600)}
 else if(j.state==='done'){const r=j.result||{};if(j.kind==='addmv'&&!j.shown){j.shown=1;api('/api/info').then(i=>{showInfo(i);list()})}
  $('jobst').innerHTML='<span class="ok">'+(j.kind==='save'?'saved '+esc(r.out):j.kind==='addmv'?r.added+' music video(s) added'+(r.converted?' ('+r.converted+' converted)':''):'extracted '+r.files+' file(s) to '+esc(r.folder))+'</span>'+((r.errors||[]).length?' <span class="bad">'+r.errors.length+' problem(s): '+esc(r.errors.slice(0,3).join('; '))+'</span>':'')}
 else if(j.state==='error')$('jobst').innerHTML='<span class="bad">'+esc(j.error)+'</span>'}
let addItems=[];
$('add').onclick=()=>{if(!info.open){msg('open a disc first',1);return}$('addtb').innerHTML='';addItems=[];$('adddlg').showModal()};
$('addcancel').onclick=()=>$('adddlg').close();
async function readMidi(){const paths=$('midipaths').value.split(';').map(s=>s.trim()).filter(Boolean);if(!paths.length)return;const r=await api('/api/midimeta',{paths});if(r.error){alert(r.error);return}
 addItems=r.items;$('addtb').innerHTML=addItems.map((it,i)=>'<tr><td><input type="number" min="1" max="99999" value="'+it.number+'" data-i="'+i+'" data-f="number" style="width:100px"></td><td><input type="text" value="'+esc(it.title)+'" data-i="'+i+'" data-f="title" style="width:100%"></td><td><input type="text" value="'+esc(it.artist)+'" data-i="'+i+'" data-f="artist" style="width:100%"></td><td class="mut">'+esc(it.path.split(/[\\/]/).pop())+(it.ok?'':' <span class="bad">not a MIDI file</span>')+'</td>'+
  '<td style="white-space:nowrap"><input type="text" value="'+esc(it.chorus||'')+'" data-i="'+i+'" data-f="chorus" placeholder="none" style="width:180px"> <button class="sec addch" data-i="'+i+'">…</button> at <input type="number" step="0.05" min="0" value="'+(it.chorus_at||0)+'" data-i="'+i+'" data-f="chorus_at" style="width:70px"> s</td></tr>').join('');
 document.querySelectorAll('button.addch').forEach(b=>b.onclick=()=>pickInto('chorus',document.querySelector('#addtb input[data-f=chorus][data-i="'+b.dataset.i+'"]')))}
$('pickmidi').onclick=async()=>{if(await pickInto('midi',$('midipaths')))readMidi()};$('loadmidi').onclick=readMidi;
$('addok').onclick=async()=>{document.querySelectorAll('#addtb input').forEach(inp=>{addItems[+inp.dataset.i][inp.dataset.f]=inp.value});
 const r=await api('/api/add',{items:addItems.filter(x=>x.ok)});if(r.error){alert(r.error);return}
 $('adddlg').close();msg(r.added.length+' song(s) added'+(r.errors.length?' · problems: '+r.errors.join('; '):''),r.errors.length&&!r.added.length);showInfo(r.info);$('q').value='';start=0;list()};
let mvItems=[];
$('addmv').onclick=()=>{if(!info.open){msg('open a disc first',1);return}$('mvtb').innerHTML='';mvItems=[];$('mvdlg').showModal()};
$('mvcancel').onclick=()=>$('mvdlg').close();
const dur=t=>t?Math.floor(t/60)+':'+String(Math.round(t%60)).padStart(2,'0'):'';
async function readMv(){const paths=$('mvpaths').value.split(';').map(s=>s.trim()).filter(Boolean);if(!paths.length)return;$('mvtb').innerHTML='<tr><td colspan=5 class="mut">reading…</td></tr>';
 const r=await api('/api/videometa',{paths});if(r.error){alert(r.error);return}mvItems=r.items;
 $('mvtb').innerHTML=mvItems.map((it,i)=>'<tr><td><input type="number" min="1" max="99999" value="'+it.number+'" data-i="'+i+'" data-f="number" style="width:100px"></td><td><input type="text" value="'+esc(it.title)+'" data-i="'+i+'" data-f="title" style="width:100%"></td><td><input type="text" value="'+esc(it.artist)+'" data-i="'+i+'" data-f="artist" style="width:100%"></td><td class="mut">'+esc(it.path.split(/[\\/]/).pop())+' · '+fmt(it.size)+(it.duration?' · '+dur(it.duration):'')+'</td><td>'+(it.ok?(it.as_is?'<span class="ok">as is</span>':'<span class="mut">convert</span>'):'<span class="bad">'+esc(it.problem||'not usable')+'</span>')+'</td><td style="white-space:nowrap"><input type="text" value="'+esc(it.lyrics||'')+'" data-i="'+i+'" data-f="lyrics" id="mvl'+i+'" placeholder="none" style="width:170px"> <button class="sec mvlp" data-i="'+i+'">…</button></td></tr>').join('');
 document.querySelectorAll('button.mvlp').forEach(b=>b.onclick=()=>pickInto('lyrics',$('mvl'+b.dataset.i)))}
$('pickmv').onclick=async()=>{if(await pickInto('video',$('mvpaths')))readMv()};$('loadmv').onclick=readMv;
$('mvok').onclick=async()=>{document.querySelectorAll('#mvtb input').forEach(inp=>{mvItems[+inp.dataset.i][inp.dataset.f]=inp.value});
 const items=mvItems.filter(x=>x.ok).map(x=>Object.assign(x,{offset:+$('mvoff').value||0,keep_music:false}));if(!items.length){alert('no usable videos');return}
 const r=await api('/api/addvideo',{items});if(r.error){alert(r.error);return}$('mvdlg').close();msg('adding '+items.length+' music video(s)…');pollJob()};
let lyrNum=0;
async function openLyrics(n,row){lyrNum=n;$('lyrtitle').textContent=String(n).padStart(6,'0')+' '+(row?row.title+(row.artist?' — '+row.artist:''):'');$('lyrpath').value='';$('lyrinfo').textContent='';$('lyroff').value=0;
 $('lyrdel').style.display=row&&row.lyrics?'':'none';$('lyrcur').textContent=row&&row.lyrics?'reading current lyrics…':'No lyrics yet.';$('lyrdlg').showModal();
 if(row&&row.lyrics){const r=await api('/api/lyricstext',{number:n});$('lyrcur').innerHTML=r.error?esc(r.error):'Current lyrics ('+r.lines.length+' lines'+(r.notes?', with music':'')+'): <span class="mut">'+r.lines.slice(0,6).map(l=>mmss(l.t)+' '+esc(l.text)).join(' · ')+(r.lines.length>6?' …':'')+'</span>'}}
async function readLyr(){const p=$('lyrpath').value.trim();if(!p)return;const r=await api('/api/lyricsinfo',{path:p});
 $('lyrinfo').innerHTML=r.error?'<span class="bad">'+esc(r.error)+'</span>':'<span class="ok">'+r.lines+' lines, '+r.syllables+' syllables</span> · first lyric at '+mmss(r.first||0)+' · '+(r.notes?r.notes+' notes of music (left out)':'lyrics only')+(r.title?' · from "'+esc(r.title)+'"':'')+'<br>'+esc(r.preview)}
$('lyrpick').onclick=async()=>{if(await pickInto('lyrics',$('lyrpath')))readLyr()};$('lyrread').onclick=readLyr;$('lyrcancel').onclick=()=>$('lyrdlg').close();
$('lyrok').onclick=async()=>{if(!$('lyrpath').value.trim()){alert('choose a lyrics file');return}const r=await api('/api/setlyrics',{number:lyrNum,path:$('lyrpath').value.trim(),offset:+$('lyroff').value||0,keep_music:false});
 if(r.error){alert(r.error);return}$('lyrdlg').close();msg('lyrics set for #'+lyrNum+' (saved with the new disc)');showInfo(r.info);list()};
$('lyrdel').onclick=async()=>{const r=await api('/api/setlyrics',{number:lyrNum,path:null});if(r.error){alert(r.error);return}$('lyrdlg').close();msg('lyrics removed from #'+lyrNum);showInfo(r.info);list()};
let soundMode='render',sndTimer=null,seeking=false;
const mmss=t=>Math.floor(t/60)+':'+String(Math.floor(t%60)).padStart(2,'0');
function showSound(r){soundMode=r.sound;
 $('sound').innerHTML=r.devices.filter(d=>d.id!=='capture').map(d=>'<option value="live:'+d.id+'"'+(r.sound==='live'&&r.device===d.id?' selected':'')+'>MIDI: '+esc(d.name)+'</option>').join('')+'';
 $('soundinfo').textContent=r.windows?'':'(MIDI devices: Windows only)';
 const p=r.player||{};if(p.state&&p.state!=='idle'){$('mbar').style.display='';$('mtitle').textContent=(p.number?String(p.number).padStart(6,'0')+' ':'')+(p.title||'');$('martist').textContent=p.artist||'';
   if(!seeking){$('mpos').max=p.dur||1;$('mpos').value=p.pos||0}$('mtime').textContent=mmss(p.pos||0)+' / '+mmss(p.dur||0)+(p.state==='paused'?' (paused)':'')}
 else if(p.state==='idle')$('mbar').style.display='none'}
async function pollSound(){clearTimeout(sndTimer);const r=await api('/api/sound');if(!r.error){showSound(r);if(r.player&&(r.player.state==='playing'||r.player.state==='paused'))sndTimer=setTimeout(pollSound,500)}}
$('sound').onchange=async()=>{const v=$('sound').value;const r=v==='render'?await api('/api/sound',{sound:'render'}):await api('/api/sound',{sound:'live',device:v.slice(5)});if(r.error)msg(r.error,1);else showSound(r)};
$('mpause').onclick=async()=>{showSound(await api('/api/mpause',{}));pollSound()};$('mstop').onclick=async()=>{showSound(await api('/api/mstop',{}))};
$('mpos').oninput=()=>{seeking=true};$('mpos').onchange=async()=>{seeking=false;const n=+$('mtitle').textContent.slice(0,6);if(n){showSound(await api('/api/mplay',{number:n,start:+$('mpos').value}));pollSound()}};
pollSound();
(async()=>{const i=await api('/api/info');showInfo(i);if(i.open){$('path').value=i.path;list()}})();
</script></body></html>"""


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        server_version = 'SongManager/' + VERSION
        def log_message(self, *a): pass
        def _local(self): return self.headers.get('Host', '').split(':')[0] in ('127.0.0.1', 'localhost')
        def _send(self, code, body, ctype='application/json'):
            if isinstance(body, (dict, list)): body = json.dumps(body, default=str).encode()
            elif isinstance(body, str): body = body.encode()
            self.send_response(code); self.send_header('Content-Type', ctype); self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store'); self.end_headers(); self.wfile.write(body)
        def _auth(self, q):
            tok = self.headers.get('X-Token') or (q.get('t') or [''])[0]
            return self._local() and secrets.compare_digest(tok or '', app.token)
        def do_GET(self):
            u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query)
            if u.path == '/':
                if not self._local(): return self._send(403, 'forbidden', 'text/plain')
                return self._send(200, PAGE.replace('__TOKEN__', app.token).replace('__VERSION__', VERSION), 'text/html; charset=utf-8')
            if not self._auth(q): return self._send(403, {'error': 'forbidden'})
            try:
                if u.path == '/api/info': return self._send(200, app.info())
                if u.path == '/api/songs':
                    d = app.need()
                    only = (q.get('only') or [''])[0] or None
                    with app.lock: return self._send(200, d.listing((q.get('q') or [''])[0], int((q.get('start') or ['0'])[0]), 100, only=only))
                if u.path == '/api/job': return self._send(200, {k: v for k, v in app.job.items()})
                if u.path == '/api/sound': return self._send(200, app.sound_status())
                if u.path == '/api/preview': return self._send(200, {k: v for k, v in app.preview.items() if k != 'wav'})
                if u.path == '/api/preview.wav':
                    if not app.preview.get('wav'): return self._send(404, {'error': 'nothing rendered'})
                    return self._send(200, app.preview['wav'], 'audio/wav')
                if u.path == '/api/chorus':
                    with app.lock: data, ct = app.chorus_wav(int(q['n'][0]), int((q.get('i') or ['0'])[0]))
                    return self._send(200, data, ct)
            except (RuntimeError, KeyError, ValueError, IndexError) as ex:
                return self._send(400, {'error': str(ex)})
            except Exception as ex:
                return self._send(500, {'error': '%s: %s' % (type(ex).__name__, ex)})
            self._send(404, {'error': 'not found'})
        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            if not self._auth({}): return self._send(403, {'error': 'forbidden'})
            try: body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
            except Exception: return self._send(400, {'error': 'bad json'})
            try:
                if u.path == '/api/open': return self._send(200, app.open(body.get('path', '')))
                if u.path == '/api/pick':
                    try: return self._send(200, dict(path=pick(body.get('kind'), app.last_dir)))
                    except Exception as ex: return self._send(400, {'error': 'file dialog not available (%s) - type the path instead' % ex})
                if u.path == '/api/delete':
                    d = app.need(); done = 0
                    with app.lock:
                        for n in body.get('numbers', []):
                            try: d.delete(int(n)); done += 1
                            except KeyError: pass
                    return self._send(200, dict(done=done, info=app.info()))
                if u.path == '/api/undelete':
                    d = app.need()
                    with app.lock:
                        for n in body.get('numbers', []):
                            try: d.undelete(int(n))
                            except KeyError: pass
                    return self._send(200, dict(info=app.info()))
                if u.path == '/api/extract': return self._send(200, app.extract([int(x) for x in body.get('numbers', [])], body.get('folder', '')))
                if u.path == '/api/save':
                    if not (body.get('out') or '').strip(): return self._send(400, {'error': 'enter where to save the new disc'})
                    return self._send(200, app.save(body['out']))
                if u.path == '/api/midimeta': return self._send(200, dict(items=app.midi_meta(body.get('paths', []))))
                if u.path == '/api/add':
                    with app.lock: return self._send(200, app.add_files(body.get('items', [])))
                if u.path == '/api/videometa': return self._send(200, dict(items=app.video_meta(body.get('paths', []))))
                if u.path == '/api/addvideo': return self._send(200, app.add_videos(body.get('items', [])))
                if u.path == '/api/lyricsinfo': return self._send(200, app.lyrics_info(body.get('path', '')))
                if u.path == '/api/chorusinfo': return self._send(200, app.chorus_info(body.get('number')))
                if u.path == '/api/setchorus': return self._send(200, app.set_chorus(body.get('number'), body.get('path'), body.get('at')))
                if u.path == '/api/setlyrics': return self._send(200, app.set_lyrics(body.get('number'), body.get('path'), body.get('offset', 0), body.get('keep_music')))
                if u.path == '/api/lyricstext': return self._send(200, app.mv_lyrics_text(body.get('number')))
                if u.path == '/api/openvideo': return self._send(200, app.open_video(body.get('number')))
                if u.path == '/api/play': return self._send(200, app.play(body.get('number')))
                if u.path == '/api/sound': return self._send(200, app.set_sound(body.get('sound'), body.get('device')))
                if u.path == '/api/mplay': return self._send(200, app.mplay(body.get('number'), body.get('start', 0)))
                if u.path == '/api/mpause':
                    if app.mplayer: app.mplayer.pause()
                    return self._send(200, app.sound_status())
                if u.path == '/api/mstop':
                    if app.mplayer: app.mplayer.stop()
                    return self._send(200, app.sound_status())
            except (RuntimeError, KeyError, ValueError, FileNotFoundError, songdisc.DiscWriteError, isoread.DiscError) as ex:
                return self._send(400, {'error': str(ex)})
            except Exception as ex:
                return self._send(500, {'error': '%s: %s' % (type(ex).__name__, ex), 'trace': traceback.format_exc()[-800:]})
            self._send(404, {'error': 'not found'})
    return H


def main(argv=None):
    import multiprocessing
    multiprocessing.freeze_support()            # the built-in SoundFont synth runs in its own process (also in the .exe)
    ap = argparse.ArgumentParser(prog='KaraokeSongManager')
    ap.add_argument('--disc'); ap.add_argument('--port', type=int, default=8780); ap.add_argument('--no-browser', action='store_true')
    a = ap.parse_args(argv)
    app = App()
    if a.disc:
        try: app.open(a.disc)
        except Exception as ex: print('could not open', a.disc, ':', ex)
    srv = None
    for p in range(a.port, a.port + 20):
        try: srv = ThreadingHTTPServer(('127.0.0.1', p), make_handler(app)); break
        except OSError: continue
    if not srv: raise SystemExit('could not bind a local port')
    url = 'http://127.0.0.1:%d/' % srv.server_address[1]
    print('Megapro Song Manager %s - open %s  (Ctrl+C to quit)' % (VERSION, url))
    print('SoundFont:', app.sf or 'none (put dreamsam_2695.sf2 next to the program for song preview)')
    print('ffmpeg   :', isoread.find_ffmpeg() or 'not found (chorus clips are saved as .wma only; music videos must already be DivX/Xvid AVI)')
    if not a.no_browser: threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try: srv.serve_forever()
    except KeyboardInterrupt: print('bye')


if __name__ == '__main__':
    main()
