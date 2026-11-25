"""
Cadence — MP3/m4a/m4b -> single .m4b build.

Pipeline (every step logged by the caller via cadence_logging;
ffmpeg commands, exit codes and stderr are captured verbatim —
never swallowed):
  1. Probe each input with ffprobe (duration, codec, sample rate).
     A file ffprobe cannot read is a hard failure: the job is
     marked FAILED with the exact file + ffprobe error, never
     silently skipped.
  2. Build an ffmetadata chapter file: one chapter per input file,
     title from the chapter name, start = cumulative duration.
  3. Run ONE ffmpeg invocation: concat demuxer, MP3 inputs
     re-encoded to AAC (source sample rate, configurable bitrate,
     96k stereo default), m4a/m4b inputs stream-copied when the
     codec is already AAC. Embed metadata tags + cover art.
  4. Duration check: output within 2% of the sum of inputs,
     otherwise FAILED with both numbers in the log.
  5. Caller (app.py) then places the file in the Plex tree and
     moves the originals per Settings.

Design note: a single ffmpeg run (rather than per-file encodes +
a second merge) keeps the job to one pass on a small home server
and keeps the
chapter times exact — the concat demuxer + -metadata from the
ffmetadata file is the standard m4b-tool-style approach.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile

AUDIO_EXTS = {".mp3", ".m4a", ".m4b", ".flac", ".aac", ".ogg", ".wav"}


class BuildError(Exception):
    """A build step failed. Message is the exact, user-facing reason
    (file + step + exit code + stderr tail) — shown in the UI."""


class CancelledBuild(Exception):
    """The user cancelled the job mid-build. Not a failure — the
    job is marked Cancelled, partial output is deleted."""


def probe(path: str) -> dict:
    """ffprobe one file. Raises BuildError with verbatim stderr."""
    cmd = ["ffprobe", "-v", "error", "-show_entries",
           "format=duration,size", "-show_entries", "stream=codec_name,sample_rate,channels",
           "-of", "json", path]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except FileNotFoundError as exc:
        raise BuildError(f"ffprobe not found on this system: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise BuildError(f"ffprobe timed out after 120s probing {path}") from exc
    if proc.returncode != 0:
        raise BuildError(
            f"ffprobe failed for {path} (exit {proc.returncode}): "
            f"{proc.stderr.strip()[-2000:] or '(no stderr)'}"
        )
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise BuildError(f"ffprobe returned unparseable output for {path}: {exc}") from exc
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    audio = next((s for s in streams if s.get("codec_name")), streams[0] if streams else {})
    try:
        duration = float(fmt.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        raise BuildError(
            f"ffprobe reports zero/unknown duration for {path} "
            f"(file may be corrupt or empty) — refusing to guess. "
            f"ffprobe output: {(proc.stdout or '').strip()[:500]}"
        )
    return {
        "path": path,
        "duration": duration,
        "size": int(fmt.get("size") or os.path.getsize(path) if os.path.exists(path) else 0),
        "codec": audio.get("codec_name", "unknown"),
        "sample_rate": int(audio.get("sample_rate") or 44100),
        "channels": int(audio.get("channels") or 2),
    }


def write_ffmetadata(chapters: list[dict], path: str) -> None:
    """Write an ffmetadata file: one [CHAPTER] per input file."""
    lines = [";FFMETADATA1"]
    start_ms = 0
    for ch in chapters:
        dur_ms = int(round(float(ch["duration"]) * 1000))
        end_ms = start_ms + dur_ms
        title = (ch.get("title") or "Chapter").replace("\n", " ").replace("=", "-")
        lines.append("[CHAPTER]")
        lines.append("TIMEBASE=1/1000")
        lines.append(f"START={start_ms}")
        lines.append(f"END={end_ms}")
        lines.append(f"title={title}")
        start_ms = end_ms
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def write_concat_list(files: list[str], path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for f in files:
            # ffmpeg concat demuxer: single-quoted, ' escaped as '\''
            safe = f.replace("'", "'\\''")
            fh.write(f"file '{safe}'\n")


def build_m4b(files: list[dict], metadata: dict, output_path: str,
              temp_dir: str, bitrate: str = "96k",
              log=None, progress_callback=None, cancel_check=None,
              register_proc=None) -> dict:
    """Merge ``files`` (in confirmed order) into one .m4b.

    files: [{path, duration, codec, sample_rate, chapter_title}]
    metadata: title/author/narrator/genre/year/description/cover_path
    log: callable(job-less) — log_event partial from app.py; if None,
         steps are not logged (tests use this path).
    progress_callback: callable(fraction 0.0-1.0) — called from REAL
         ffmpeg progress (out_time_ms / total duration), never faked.
         Fraction is of the ffmpeg encode only; the caller maps it
         onto the overall 30-90% converting band.
    cancel_check: callable() -> bool — checked while ffmpeg runs;
         True aborts the encode (process terminated, partial output
         deleted) and raises CancelledBuild.
    register_proc: callable(proc | None) — lets the caller terminate
         the live ffmpeg process on Cancel (app.py keeps the handle).

    Returns {"output_path", "duration", "size", "input_duration",
             "input_files": n, "ffmpeg_cmd": [...]}.
    Raises BuildError on ANY failure — with command, exit code and
    stderr tail in the message. Raises CancelledBuild on cancel.
    """
    def _log(stage, level, msg, **kw):
        if log is not None:
            log(stage, level, msg, **kw)

    if not files:
        raise BuildError("Build called with no input files — nothing to merge.")
    for f in files:
        if not os.path.exists(f["path"]):
            raise BuildError(f"Input file missing at build time: {f['path']}")

    os.makedirs(temp_dir, exist_ok=True)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    total_in = sum(float(f["duration"]) for f in files)
    _log("build", "INFO",
         f"Starting merge of {len(files)} files, "
         f"total input duration {total_in:.1f}s, target {output_path}")

    # Re-encode decision: if EVERY input is already AAC in m4a/m4b,
    # stream-copy; otherwise re-encode everything to AAC so the
    # concat output has one consistent codec.
    all_aac = all(
        (f.get("codec") == "aac" and
         os.path.splitext(f["path"])[1].lower() in (".m4a", ".m4b"))
        for f in files
    )
    sample_rate = str(files[0].get("sample_rate") or 44100)

    chapters = [{
        "title": f.get("chapter_title") or os.path.splitext(
            os.path.basename(f["path"]))[0],
        "duration": float(f["duration"]),
    } for f in files]

    tmp_meta = os.path.join(temp_dir, "ffmetadata.txt")
    tmp_list = os.path.join(temp_dir, "concat.txt")
    tmp_out = os.path.join(temp_dir, "output.m4b")
    write_ffmetadata(chapters, tmp_meta)
    write_concat_list([f["path"] for f in files], tmp_list)
    _log("build", "INFO",
         f"Wrote chapter metadata ({len(chapters)} chapters) to {tmp_meta}")

    cmd = ["ffmpeg", "-y", "-nostdin", "-v", "error",
           "-f", "concat", "-safe", "0", "-i", tmp_list,
           "-i", tmp_meta]
    cover_path = metadata.get("cover_path")
    cover_ok = bool(cover_path and os.path.exists(cover_path))
    if cover_ok:
        cmd += ["-i", cover_path]
    cmd += ["-map", "0:a"]
    if cover_ok:
        cmd += ["-map", "2:v"]
    cmd += ["-map_metadata", "1"]
    if all_aac:
        cmd += ["-c:a", "copy"]
        _log("build", "INFO", "All inputs already AAC — stream-copying audio (no re-encode)")
    else:
        cmd += ["-c:a", "aac", "-b:a", bitrate, "-ar", sample_rate, "-ac", "2"]
        _log("build", "INFO",
             f"Re-encoding audio to AAC {bitrate}, {sample_rate} Hz stereo")
    if cover_ok:
        cmd += ["-c:v", "copy", "-disposition:v", "attached_pic"]
    cmd += ["-metadata", f"title={metadata.get('title', '')}",
            "-metadata", f"album={metadata.get('title', '')}",
            "-metadata", f"album_artist={metadata.get('author', '')}",
            "-metadata", f"artist={metadata.get('narrator') or metadata.get('author', '')}",
            "-metadata", f"genre={metadata.get('genre', '')}",
            "-metadata", f"date={metadata.get('year', '')}",
            "-metadata", f"description={metadata.get('description', '')}",
            "-metadata", f"comment={metadata.get('description', '')}",
            "-f", "mp4", "-progress", "pipe:1", tmp_out]
    _log("ffmpeg", "INFO", f"Running: {' '.join(cmd)}")
    # Real progress, not a spinner: ffmpeg writes out_time_ms to the
    # -progress pipe (stdout); stderr stays clean for real errors.
    # Poll cancel_check between progress lines so Cancel lands fast.
    total_ms = max(1, int(round(total_in * 1000)))
    if progress_callback is not None:
        progress_callback(0.0)
    stderr_text = ""
    returncode = None
    proc = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                bufsize=1)
        if register_proc is not None:
            register_proc(proc)
        if cancel_check is not None and cancel_check():
            raise CancelledBuild("Cancelled by user before ffmpeg started.")
        # stdout: "out_time_ms=123456" lines (+ "progress=end" at the end).
        for line in proc.stdout or []:
            line = line.strip()
            if line.startswith("out_time_ms="):
                try:
                    ms = int(line.split("=", 1)[1])
                except ValueError:
                    ms = 0
                if progress_callback is not None:
                    progress_callback(max(0.0, min(1.0, ms / total_ms)))
            if cancel_check is not None and cancel_check():
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                raise CancelledBuild("Cancelled by user during ffmpeg encode.")
        _, stderr_text = proc.communicate(timeout=6 * 3600)
        returncode = proc.returncode
    except FileNotFoundError as exc:
        raise BuildError(f"ffmpeg not found on this system: {exc}") from exc
    except CancelledBuild:
        # Never leave a partial .m4b behind for Plex to pick up.
        try:
            if os.path.exists(tmp_out):
                os.remove(tmp_out)
        except OSError:
            pass
        raise
    except subprocess.TimeoutExpired as exc:
        if proc is not None:
            proc.kill()
        raise BuildError("ffmpeg timed out after 6 hours — job aborted") from exc
    finally:
        if register_proc is not None:
            register_proc(None)
        if proc is not None and proc.poll() is None:
            proc.kill()

    _log("ffmpeg", "INFO" if returncode == 0 else "ERROR",
         f"ffmpeg exited with code {returncode}",
         extra={"exit_code": returncode,
                "stderr_tail": (stderr_text or "")[-2000:]})
    if returncode != 0:
        raise BuildError(
            f"ffmpeg merge failed (exit {returncode}). "
            f"Command: {' '.join(cmd)}. "
            f"stderr: {(stderr_text or '').strip()[-2000:] or '(no stderr)'}"
        )
    if progress_callback is not None:
        progress_callback(1.0)
    if not os.path.exists(tmp_out) or os.path.getsize(tmp_out) == 0:
        raise BuildError(
            f"ffmpeg exited 0 but produced no output file at {tmp_out} "
            f"(stderr: {(stderr_text or '').strip()[-500:] or '(empty)'})"
        )

    # Duration check: output within 2% of summed inputs.
    out_probe = probe(tmp_out)
    out_dur = out_probe["duration"]
    diff_pct = abs(out_dur - total_in) / total_in * 100 if total_in else 100
    _log("build", "INFO",
         f"Duration check: input {total_in:.1f}s, output {out_dur:.1f}s, "
         f"difference {diff_pct:.2f}% (limit 2%)",
         outcome="pass" if diff_pct <= 2 else "FAIL")
    if diff_pct > 2:
        raise BuildError(
            f"Duration check FAILED: output {out_dur:.1f}s vs input "
            f"{total_in:.1f}s — difference {diff_pct:.2f}% exceeds 2%. "
            f"The merge is incomplete or padded; output kept at {tmp_out} "
            f"for inspection, NOT placed in the Plex library."
        )

    os.replace(tmp_out, output_path)
    size = os.path.getsize(output_path)
    _log("build", "INFO",
         f"Build complete: {output_path} ({size / 1e6:.1f} MB, {out_dur:.1f}s)",
         outcome="success")
    return {"output_path": output_path, "duration": out_dur, "size": size,
            "input_duration": total_in, "input_files": len(files),
            "ffmpeg_cmd": cmd}
