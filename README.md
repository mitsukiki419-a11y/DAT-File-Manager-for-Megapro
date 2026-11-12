Karaoke Song Manager

A disc editor for Megapro MP100NSP karaoke discs. Open a disc image, browse and search its songs, add your own songs and music videos, give songs a chorus, add lyrics to music videos, remove songs, and save a new disc that plays on the real player and in Chiaki Karaoke Emu.

Your original disc is never changed: everything is written to a new ISO or folder.

Made by ChiakiMK · built with Claude (Anthropic)

What you need
File	What it is	Where to get it
KaraokeSongManager.exe	the Song Manager	this release
a karaoke disc (.iso, or a folder with KARA125)	the disc to edit	your own disc (not included)
ffmpeg.exe	converts videos and chorus audio	free download, see below
a SoundFont (.sf2)	to listen to songs in the Song Manager	optional, see below
Get ffmpeg
Go to https://www.gyan.dev/ffmpeg/builds/
Download ffmpeg-release-essentials.zip.
Open the zip, go into bin and copy ffmpeg.exe next to KaraokeSongManager.exe.

Without ffmpeg you can still add songs and AVI (DivX/Xvid) music videos. You need it to add MP4, MKV, MOV, WMV, MPG and other videos, and to add a chorus from MP3, WAV, OGG, FLAC and similar audio files. ffmpeg is a separate free program by the FFmpeg project (https://ffmpeg.org).

SoundFont (optional)

To hear songs with ▶ inside the Song Manager, put a General MIDI SoundFont (.sf2, for example dreamsam_2695.sf2 or GeneralUser GS) next to the exe. You can also play through a Windows MIDI device instead.

Your folder:

C:\KaraokeSongManager\
    KaraokeSongManager.exe
    ffmpeg.exe
    dreamsam_2695.sf2      (optional)
Start
Double-click KaraokeSongManager.exe. A console window opens (leave it open; closing it quits) and your browser opens the Song Manager page (http://127.0.0.1:8780).
In Disc, click ISO… to pick a disc image, or Folder… for a disc copied to a folder, then Open.
The song list fills with every song on the disc.

If Windows SmartScreen says "Windows protected your PC", click More info → Run anyway (the exe is not signed).

The song list
Search by number, title or artist. The filter switches between All, Karaoke songs and Music videos. ◀ ▶ change page.
Click rows to select them; Select page and Clear selection help with many songs.
▶ plays a karaoke song (⏯ pause, ■ stop, and you can seek). ▶ Video opens a music video.
♪ chorus marks songs that have a chorus.
Add songs
Click Add songs… → Choose MIDI files… and pick one or more .mid / .kar files.
Each file gets a song number (the next free numbers), a title and an artist, read from the file. Change them as you like before adding.
Optional: give a song a chorus in the same list (see below). An audio file named like the MIDI, or <name> chorus, is picked up automatically.
Click Add to disc.

The lyrics come from the MIDI file's lyric events (a .kar file has them). Songs without lyric events play without lyrics.

Add music videos
Click Add music videos… → Choose videos….
AVI files with DivX/Xvid video and MP3 sound are added as they are. MP4, MKV, MOV, WMV, MPG and other formats are converted with ffmpeg (this takes a while for long videos).
Lyrics: a .kar or .mid file with the same name as the video is used automatically. Use Lyrics offset if the lyrics come too early or too late.
Set the number, title and artist, then Add to disc.

On the player, type the music video's number like any song.

Chorus (recorded voices)

Some songs on Megapro discs come with recorded voices (backing vocals, a choir) that play along with the music. You can add one to any song, old or new:

Click Add chorus… (or Chorus… if it has one) on the song.
Choose… an audio file (WMA, MP3, WAV, OGG, M4A, FLAC…). It is converted to the disc's format.
Set Starts at: how many seconds into the song the voices begin.
Click Use this chorus. Remove chorus takes it off again.

In Chiaki Karaoke Emu, turn on ⚙ Settings → Player → Chorus to hear it.

Lyrics for music videos
Click Lyrics… on a music video.
Choose a karaoke MIDI with lyrics (a .kar file, a Megapro song MIDI you extracted, or any MIDI with lyric events) and click Read.
Set the Offset if needed: + if the lyrics come too early, − if too late.
Click Use these lyrics. Remove lyrics takes them off.
Extract and delete
Extract selected… saves the selected songs to a folder: karaoke songs as MIDI (plus their chorus clips), music videos as .avi.
Delete selected removes songs from the new disc's song list. Undo delete brings them back until you save.
Save the new disc
In Save, click Save as ISO… (recommended) and choose a new file name, or Folder… for a new empty folder.
Click Save new disc. The changes waiting to be saved are listed above the button.
An ISO keeps every file and sector of the original, including the disc's karaoke signature, so it plays on the real player. Burn it as a disc image (for example with ImgBurn → "Write image file to disc").
A folder holds all disc files; to burn it, use a tool that keeps the original disc's boot area. The ISO is the safer choice.
Added songs go into a new song bank file (KARA125\AUDIOn.DAn) and the song list (title.bnk / title.hdr) is rewritten.

Test the new ISO in Chiaki Karaoke Emu before burning.

Troubleshooting
Problem	Fix
Can't add MP4/MKV/… videos, or an MP3 chorus	ffmpeg.exe is missing next to the exe
▶ plays no sound	put a .sf2 next to the exe, or choose a MIDI device under "Play with"
A new song has no lyrics	the MIDI has no lyric events; use a .kar file
Music video lyrics early / late	Lyrics… → Offset
The page didn't open	open http://127.0.0.1:8780 in your browser
Another program uses the port	start it from a command prompt with KaraokeSongManager.exe --port 8790

Command line: KaraokeSongManager.exe [--disc PATH] [--port N] [--no-browser]

Build from source (Windows)
Install Python 3.9+ from https://www.python.org (tick Add python.exe to PATH).
Double-click build_songmgr.bat → dist\KaraokeSongManager.exe.

If ffmpeg.exe is in the source folder when you build, it is packed inside the exe. For a public release leave it out and let people download ffmpeg themselves.

Run without building: python songmgr.py.

Notes
Not affiliated with Megapro. Megapro and other names belong to their owners.
Only add and share songs, videos and discs you have the rights to.
Keep a copy of your original disc. The Song Manager never changes it, but burning discs can still go wrong.
