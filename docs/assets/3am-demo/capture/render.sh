#!/usr/bin/env bash
# Cuts the captured scenes (capture.mjs) into the demo assets:
#
#   scene-1..6-*.webm ──ffmpeg concat──► 3am-demo.mp4   (H.264, 1280x720, 60-90 s)
#   scene-4-ui + scene-5-after ────────► 3am-demo.gif   (README; must be < 5 MB)
#   screen-*.png are copied as-is                         (3-4 stills)
#
# Usage: DEMO_CAPTURE_DIR=dir render.sh   (writes into DEMO_CAPTURE_DIR/out)
# Needs ffmpeg + ffprobe. Fails if the MP4 is outside 60-90 s or the GIF is
# still >= 5 MB at the smallest size we try.
set -euo pipefail
DIR="${DEMO_CAPTURE_DIR:?set DEMO_CAPTURE_DIR}"
OUT="$DIR/out"; mkdir -p "$OUT"
SCENES=(1-title 2-agent 3-pending 4-ui 5-after 6-end)

inputs=(); filters=""
for i in "${!SCENES[@]}"; do
  f="$DIR/scene-${SCENES[$i]}.webm"
  [ -s "$f" ] || { echo "render: missing $f" >&2; exit 1; }
  inputs+=(-i "$f")
  # Drop the first 0.3 s of each scene (blank frame before the page paints),
  # normalise fps/size so concat accepts them.
  filters+="[$i:v]trim=start=0.3,setpts=PTS-STARTPTS,fps=30,scale=1280:720,setsar=1[v$i];"
done
for i in "${!SCENES[@]}"; do filters+="[v$i]"; done
filters+="concat=n=${#SCENES[@]}:v=1:a=0[out]"

ffmpeg -y -loglevel error "${inputs[@]}" -filter_complex "$filters" -map "[out]" \
  -c:v libx264 -pix_fmt yuv420p -crf 23 -preset medium -movflags +faststart "$OUT/3am-demo.mp4"

dur="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUT/3am-demo.mp4")"
echo "render: 3am-demo.mp4 ${dur}s, $(du -k "$OUT/3am-demo.mp4" | cut -f1) KB"
python3 -c "import sys; d=float(sys.argv[1]); sys.exit(0 if 60 <= d <= 90 else f'MP4 is {d:.1f}s, want 60-90s')" "$dur"

# GIF: the part that matters (Reject tap + after-listing), shrinking until < 5 MB.
ffmpeg -y -loglevel error -i "$DIR/scene-4-ui.webm" -i "$DIR/scene-5-after.webm" -filter_complex \
  "[0:v]trim=start=0.3,setpts=PTS-STARTPTS[a];[1:v]trim=start=0.3,setpts=PTS-STARTPTS[b];[a][b]concat=n=2:v=1:a=0[out]" \
  -map "[out]" -c:v libx264 -pix_fmt yuv420p -crf 18 "$DIR/gif-src.mp4"
limit=$((5 * 1024 * 1024))
for spec in "960:10" "800:8" "720:6" "640:5"; do
  w="${spec%%:*}"; fps="${spec##*:}"
  ffmpeg -y -loglevel error -i "$DIR/gif-src.mp4" -filter_complex \
    "fps=$fps,scale=$w:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=128:stats_mode=diff[p];[s1][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle" \
    "$OUT/3am-demo.gif"
  size="$(stat -c %s "$OUT/3am-demo.gif")"
  echo "render: 3am-demo.gif at ${w}px/${fps}fps = $((size / 1024)) KB"
  [ "$size" -lt "$limit" ] && break
done
[ "$size" -lt "$limit" ] || { echo "render: GIF still >= 5 MB at 640px/5fps" >&2; exit 1; }

cp "$DIR"/screen-*.png "$OUT/"
ls -l "$OUT"
