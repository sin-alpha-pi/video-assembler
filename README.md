# Dialogue Assembler

Cuts speaker clips into single lines using the video script, puts them in script order,
and adds slide-left transitions with a whoosh plus extra audio for non-dialogue clips.

**Windows app:** download the latest `DialogueAssembler-windows.zip` from [Releases](https://github.com/sin-alpha-pi/video-assembler/releases/latest), unzip, run `DialogueAssembler.exe`. Everything (FFmpeg, speech recognition model) is included.

**Web version (slower):** https://sin-alpha-pi.github.io/video-assembler/
Runs entirely in the browser (Chrome or Edge recommended). Files are never uploaded.

## Folder contents
- `script.txt` – the script. Dialogue lines look like `Mentor: text`; everything else is ignored.
- Speaker clips with the speaker name in the filename: `mentor_1.mp4`, `husband_2.mp4`, ...
- `transition.mp3` – whoosh for every slide.
- Non-dialogue clips and their audio, referenced in the script.

## Script tags
```
[intro.mp4]                              whole clip, muted
[left]                                   slide-left + whoosh into the next item
[background.mp4 + background.mp3]        whole clip, muted, with the mp3
[background.mp4 + background.mp3 @1.2]   mp3 starts 1.2 s into the clip
```

## Source
- `app/` – the Windows app (core.py + gui.py), built by `.github/workflows/build-windows.yml`.

## Python version
`python/dialogue_assemble.py` does the same thing locally (needs Python, FFmpeg, openai-whisper).
