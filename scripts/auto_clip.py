#!/usr/bin/env python3
"""ClipForge server auto-clip driver (runs inside GitHub Actions).

Downloads a YouTube video (VOD or live stream), finds the loudest
moments from the audio, and cuts vertical 1080x1920 MP4 clips.

Only the standard library + yt-dlp + ffmpeg are used. No API keys,
no paid services. On failure it writes clips/error.txt with a plain
message and exits non-zero, so the workflow can report the error
back through the release instead of failing silently.
"""
import argparse
import json
import math
import os
import re
import struct
import subprocess
import sys

YOUTUBE_RE = re.compile(r"^https?://(www\.|m\.)?(youtube\.com|youtu\.be)/")


def log(msg):
    print("[auto-clip] " + msg, flush=True)


def run(cmd):
    log("$ " + " ".join(cmd))
    subprocess.run(cmd, check=True)


def fail(outdir, msg):
    """Write a plain-language error file and exit non-zero."""
    log("ERROR: " + msg)
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "error.txt"), "w", encoding="utf-8") as f:
        f.write(msg + "\n")
    sys.exit(1)


def probe_duration(path):
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            text=True)
        return int(float(out.strip()))
    except Exception:
        return 0


def per_second_energy(audio_path):
    """Stream the audio as 8 kHz mono floats and return per-second RMS.

    Streaming keeps memory flat (~32 KB/s) no matter how long the video
    is, which is the whole point: a 2-hour VOD must not be decoded into
    RAM at full rate.
    """
    cmd = ["ffmpeg", "-v", "error", "-i", audio_path, "-vn",
           "-af", "aresample=8000,pan=mono|c0=0.5*c0+0.5*c1",
           "-f", "f32le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    sq = []   # sum of squares per second
    n = []    # samples per second
    idx = 0
    buf = b""
    while True:
        chunk = proc.stdout.read(1 << 16)
        if not chunk:
            break
        data = buf + chunk
        nf = len(data) // 4
        vals = struct.unpack("<%df" % nf, data[:nf * 4])
        buf = data[nf * 4:]
        for v in vals:
            s = idx // 8000
            while len(sq) <= s:
                sq.append(0.0)
                n.append(0)
            sq[s] += v * v
            n[s] += 1
            idx += 1
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg could not decode the audio")
    return [math.sqrt(sq[i] / n[i]) if n[i] else 0.0 for i in range(len(sq))]


def smooth3(x):
    out = []
    for i in range(len(x)):
        a = x[i - 1] if i > 0 else x[i]
        b = x[i]
        c = x[i + 1] if i + 1 < len(x) else x[i]
        out.append((a + b + c) / 3.0)
    return out


def pick_windows(energy, clip_len, count):
    """Pick the loudest non-overlapping windows.

    Same idea as the ClipForge page scan: each second is compared with
    its own neighbourhood (rolling +/-90 s local baseline), not with
    the whole video's average, so loud moments are found even in a
    uniformly loud long stream. Thresholds relax if too few are found;
    if still short, fill with evenly spaced windows.
    """
    dur = len(energy)
    if dur <= 0:
        return []
    if dur <= clip_len:
        return [(0, dur)]
    e = smooth3(energy)
    gmax = max(e) or 1.0
    pref = [0.0]
    for v in e:
        pref.append(pref[-1] + v)

    def local_mean(t, half=90):
        a = max(0, t - half)
        b = min(dur, t + half + 1)
        return (pref[b] - pref[a]) / (b - a)

    def window_for(t):
        s = t - clip_len // 2
        s = max(0, min(dur - clip_len, s))
        return (s, s + clip_len)

    chosen = []

    def overlaps(w):
        return any(not (w[1] <= c[0] or w[0] >= c[1]) for c in chosen)

    for mult, frac in ((1.35, 0.18), (1.20, 0.10), (0.0, 0.0)):
        cands = []
        for t in range(dur):
            thr = max(local_mean(t) * mult, gmax * frac) if mult else 0.0
            if e[t] >= thr:
                cands.append((e[t], t))
        cands.sort(reverse=True)
        for _, t in cands:
            if len(chosen) >= count:
                break
            w = window_for(t)
            if not overlaps(w):
                chosen.append(w)
        if len(chosen) >= count:
            break
    if len(chosen) < count:
        # Fill with evenly spaced windows, trying several candidate
        # centres across the whole video so the fill is not blocked by
        # one overlapping position.
        for k in range(count * 4):
            if len(chosen) >= count:
                break
            centre = int((k + 0.5) * dur / (count * 4))
            s = max(0, min(dur - clip_len, centre - clip_len // 2))
            w = (s, s + clip_len)
            if not overlaps(w):
                chosen.append(w)
    chosen.sort()
    return chosen[:count]


def cut_from_urls(vurl, aurl, start, length, out):
    """Cut one vertical clip from direct stream URLs (VOD path).

    ffmpeg seeks to 3 s before the cut on the stream (keyframe seek,
    so only the needed bytes are fetched), then seeks the remaining
    seconds on the output side for an exact start.
    """
    pre = max(0, start - 3)
    outseek = start - pre
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-ss", str(pre), "-i", vurl]
    if aurl and aurl != vurl:
        cmd += ["-ss", str(pre), "-i", aurl]
    if outseek > 0:
        cmd += ["-ss", str(outseek)]
    cmd += ["-t", str(length),
            "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", out]
    run(cmd)


def cut_from_file(path, start, length, out):
    """Cut one vertical clip from a local file (live-stream path)."""
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-ss", str(start), "-i", path,
           "-t", str(length),
           "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
           "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k",
           "-movflags", "+faststart", out]
    run(cmd)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--job-id", required=True)
    ap.add_argument("--clip-length", type=int, default=30)
    ap.add_argument("--count", type=int, default=3)
    ap.add_argument("--out", default="clips")
    args = ap.parse_args()

    clip_len = max(5, min(600, args.clip_length))
    count = max(1, min(5, args.count))
    outdir = args.out
    os.makedirs(outdir, exist_ok=True)

    if not YOUTUBE_RE.match(args.url):
        fail(outdir, "Not a YouTube link: " + args.url[:120])

    try:
        info = json.loads(subprocess.check_output(
            ["yt-dlp", "-j", "--skip-download", "--no-playlist",
             "--no-warnings", args.url], text=True))
    except subprocess.CalledProcessError:
        fail(outdir, "Could not read this YouTube link (yt-dlp failed — the video "
                     "may be private, removed, or YouTube is blocking downloads "
                     "right now). Try again later, or use the video file instead.")
    title = info.get("title", "") or ""
    is_live = bool(info.get("is_live"))
    live_note = ""
    local_video = None
    audio_path = None

    if is_live:
        # A live stream has no end, so take what is available so far,
        # capped at the first 2 hours from the stream start.
        live_note = ("Live stream: only the first 2 hours from the stream "
                     "start were used.")
        log("Live stream detected — downloading the first 2 hours from the start")
        try:
            run(["yt-dlp", "--live-from-start",
                 "--download-sections", "*00:00:00-02:00:00",
                 "-f", "bv*[height<=720]+ba/b[height<=720]",
                 "--merge-output-format", "mp4",
                 "--no-playlist", "-o", "live.mp4", args.url])
        except subprocess.CalledProcessError:
            fail(outdir, "Could not download the live stream (YouTube may be "
                         "blocking downloads right now). Try again later, or "
                         "use the video file instead.")
        local_video = "live.mp4"
        audio_path = "live.mp4"
    else:
        log("Downloading audio only for analysis")
        # 48 kbps is plenty: the analysis only needs per-second
        # loudness, not listening quality, and the smaller file
        # downloads roughly twice as fast as 96 kbps.
        try:
            run(["yt-dlp", "-f", "ba[abr<=48]/ba", "--no-playlist",
                 "-o", "audio.%(ext)s", args.url])
        except subprocess.CalledProcessError:
            fail(outdir, "Could not download this video's audio (yt-dlp failed — "
                         "YouTube may be blocking downloads right now). Try "
                         "again later, or use the video file instead.")
        audio_path = next((f for f in os.listdir(".")
                           if f.startswith("audio.")), None)
        if not audio_path:
            fail(outdir, "Audio download produced no file.")

    try:
        energy = per_second_energy(audio_path)
    except RuntimeError:
        fail(outdir, "No audio found in this video.")
    if not energy:
        fail(outdir, "No audio found in this video.")
    dur = len(energy)
    log("Audio analysed: %d seconds" % dur)

    windows = pick_windows(energy, clip_len, count)
    if not windows:
        fail(outdir, "No moments found in this video.")
    log("Windows: %s" % (windows,))

    gmax = max(energy) or 1.0
    moments = []
    clips_meta = []
    if is_live:
        for i, (s, e) in enumerate(windows, 1):
            out = os.path.join(outdir, "clip-%d.mp4" % i)
            cut_from_file(local_video, s, clip_len, out)
            mid = min(dur - 1, s + clip_len // 2)
            moments.append({"start": s, "end": e,
                            "energy": round(energy[mid] / gmax, 3)})
            clips_meta.append({"file": "clip-%d.mp4" % i, "start": s, "end": e})
    else:
        try:
            urls = subprocess.check_output(
                ["yt-dlp", "-g",
                 "-f", "bv*[height<=1080]+ba/b[height<=1080]",
                 "--no-playlist", args.url], text=True).strip().splitlines()
        except subprocess.CalledProcessError:
            fail(outdir, "Could not get the video stream links (yt-dlp failed — "
                         "YouTube may be blocking downloads right now). Try "
                         "again later, or use the video file instead.")
        vurl = urls[0]
        aurl = urls[1] if len(urls) > 1 else urls[0]
        for i, (s, e) in enumerate(windows, 1):
            out = os.path.join(outdir, "clip-%d.mp4" % i)
            cut_from_urls(vurl, aurl, s, clip_len, out)
            mid = min(dur - 1, s + clip_len // 2)
            moments.append({"start": s, "end": e,
                            "energy": round(energy[mid] / gmax, 3)})
            clips_meta.append({"file": "clip-%d.mp4" % i, "start": s, "end": e})

    job = {"job_id": args.job_id, "status": "done", "url": args.url,
           "title": title, "duration": dur, "is_live": is_live,
           "live_note": live_note, "clip_length": clip_len,
           "moments": moments, "clips": clips_meta}
    with open(os.path.join(outdir, "job.json"), "w", encoding="utf-8") as f:
        json.dump(job, f, indent=2)
    with open(os.path.join(outdir, "moments.json"), "w", encoding="utf-8") as f:
        json.dump({"moments": moments}, f, indent=2)
    log("Done: %d clips" % len(windows))


if __name__ == "__main__":
    main()
