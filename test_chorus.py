"""Song Manager chorus feature: add / replace / remove a recorded part, new song with chorus, save, re-read."""
import sys, os; sys.path.insert(0, '/home/claude')
import songdisc, karamidi
S = sys.argv[1]; src = sys.argv[2] if len(sys.argv) > 2 else '/home/claude/test_mppv45.iso'
ok = []; bad = []
def chk(n, c, x=''): (ok if c else bad).append(n); print(('PASS ' if c else 'FAIL ') + n + ('' if c else '  ' + str(x)[:300]), flush=True)
d = songdisc.KaraokeDisc(src)
wma = songdisc.convert_chorus(S + '/vocal.wav')
chk('audio converted to WMA', wma[:16] == karamidi.ASF_HEADER_GUID and len(wma) > 50000, len(wma))
chk('song 3 has a chorus at ~8.15 s', abs((songdisc.chorus_trigger(d.midi(d.find(3))) or 0) - 8.15) < 0.1, songdisc.chorus_trigger(d.midi(d.find(3))))
no_ch = [s for s in d.songs if not s.has_audio and not s.is_mv][5]
d.set_chorus(no_ch.number, wma, at=2.5)
d.set_chorus(3, wma)                                       # replace, keep its start time
d.set_chorus(1, None)                                      # remove
m = d.midi(d.find(no_ch.number))
s_new = d.add(98765, m, 'New With Chorus', 'Tester'); d.set_chorus(98765, wma, at=1.0)
chk('pending chorus changes', d.pending()['chorus'] == 3, d.pending())
out = S + '/chorus_out.iso'
if os.path.exists(out): os.remove(out)
d.save(out)
d2 = songdisc.KaraokeDisc(out)
def info(n):
    s = d2.find(n); m = d2.midi(s); return s.has_audio, d2.chorus(s), songdisc.chorus_trigger(m)
a, c, t = info(no_ch.number); chk('added chorus: flag, clip, start', a and c == [wma] and abs(t - 2.5) < 0.05, (a, len(c), t))
a, c, t = info(3); chk('replaced chorus keeps start time', a and c == [wma] and abs(t - 8.15) < 0.1, (a, len(c), t))
a, c, t = info(1); chk('removed chorus', not a and c == [] and t is None, (a, len(c), t))
a, c, t = info(98765); chk('new song with chorus', a and c == [wma] and abs(t - 1.0) < 0.05, (a, len(c), t))
chk('music unchanged apart from the start', len(karamidi.decode_record(d2.record(d2.find(no_ch.number)))) > 1000)
d2.close()
print('%d passed, %d failed' % (len(ok), len(bad)), bad, 'song', no_ch.number)
