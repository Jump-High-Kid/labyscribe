# labyscribe

Open-source local-MCP share edition of a personal video-summary skill.
**Phase 0** ships the deterministic extraction core only: given a YouTube URL,
it downloads native subtitle tracks (`.vtt`, plus a `.json3` sample) with
yt-dlp — no ffmpeg. Transcript parsing arrives in Phase 1.

## Requirements

- Python >= 3.9
- **`yt-dlp` on your `PATH`** (self-contained binary bundling is a later phase)

## Run

```sh
python extract.py <youtube-url> --out <output-dir>
```

Raw subtitle files land in `<output-dir>/raw/`. Phase 0 exits `20`
(`NOT_IMPLEMENTED`) after capturing them — transcript generation is Phase 1.

## Test

```sh
pip install -e ".[dev]"
pytest
```
