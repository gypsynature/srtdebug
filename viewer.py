"""Live browser viewer. ffmpeg reads the SRT transport stream and writes an HLS playlist that a
browser can play. HDR sources (HLG or PQ) are tone-mapped to 8-bit SDR 4:2:0 so ordinary
browsers can show them. Audio is kept. The playlist is served by the monitor's web server."""

import os
import queue
import re
import shutil
import subprocess
import threading

HLS_TIME = 2                 # seconds per segment
HLS_LIST = 6                 # segments kept in the playlist
HDR_TRANSFERS = ("arib-std-b67", "smpte2084")
# Scale first: tone mapping in float is the expensive step, and a 540-line preview is enough to check
# the picture. Encoded at ultrafast so a small machine keeps up with real time.
SDR_CHAIN = "scale=-2:540,format=yuv420p"
HDR_CHAIN = ("scale=-2:540,zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
             "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p")
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")

_filters = None


def _ffmpeg_filters():
    global _filters
    if _filters is None:
        try:
            out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True,
                                 text=True, timeout=20).stdout
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        _filters = out
    return _filters


def has_filter(name):
    return f" {name} " in _ffmpeg_filters()


class Viewer:
    def __init__(self, outdir):
        self.dir = outdir
        self.q = queue.Queue(maxsize=600)     # bounded: if ffmpeg falls behind, chunks are dropped
        self.proc = None
        self.started = False
        self.state = "waiting for the stream"
        self.note = ""
        self.errors = []
        self.speed = None                      # encoder speed as a multiple of real time
        self.dropped = 0                       # chunks that could not be queued
        self._closing = False
        os.makedirs(outdir, exist_ok=True)

    def feed(self, chunk):
        """Called from the reader thread. Never blocks the monitor."""
        if self.proc is None or self._closing:
            return
        try:
            self.q.put_nowait(chunk)
        except queue.Full:
            # Dropping TS packets corrupts the picture for many seconds, so the preview is
            # stopped and the reason is shown instead of playing damaged video.
            self.dropped += 1
            self._closing = True
            self.state = (f"stopped: this machine cannot encode in real time"
                          + (f" (encoder at {self.speed:.2f}x)" if self.speed else "")
                          + ". The preview was stopped rather than shown corrupted.")
            # a preview that cannot keep up is killed at once: letting it drain its backlog only burns CPU
            threading.Thread(target=self.stop, kwargs={"now": True}, daemon=True).start()

    def start(self, transfer):
        """Start ffmpeg once the stream's colour transfer is known."""
        if self.started:
            return
        self.started = True
        if not shutil.which("ffmpeg"):
            self.state = "ffmpeg is not installed"
            return
        hdr = transfer in HDR_TRANSFERS
        if hdr and has_filter("zscale") and has_filter("tonemap"):
            vf, self.note = HDR_CHAIN, "HDR tone-mapped to SDR for browser playback"
        elif hdr:
            vf, self.note = SDR_CHAIN, "HDR shown without tone mapping: this ffmpeg has no zscale, so colours look washed out"
        else:
            vf, self.note = SDR_CHAIN, ""
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
               "-map", "0:v:0", "-map", "0:a:0?", "-vf", vf,
               "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
               "-g", "60", "-sc_threshold", "0", "-b:v", "1800k", "-maxrate", "1800k", "-bufsize", "3600k",
               "-c:a", "aac", "-b:a", "128k", "-ac", "2",
               "-progress", "pipe:1", "-nostats",
               "-f", "hls", "-hls_time", str(HLS_TIME), "-hls_list_size", str(HLS_LIST),
               "-hls_flags", "delete_segments+independent_segments",
               "-hls_segment_filename", os.path.join(self.dir, "seg%05d.ts"),
               os.path.join(self.dir, "index.m3u8")]
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE)
        except OSError as e:
            self.state = f"could not start ffmpeg: {e}"
            return
        self.state = "live"
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=self._read_errors, daemon=True).start()
        threading.Thread(target=self._read_progress, daemon=True).start()

    def _pump(self):
        while not self._closing:
            chunk = self.q.get()
            if chunk is None:
                break
            try:
                self.proc.stdin.write(chunk)
            except (OSError, ValueError):
                self.state = "stopped: ffmpeg ended"
                break

    def _read_progress(self):
        for line in self.proc.stdout:
            text = line.decode("utf-8", errors="replace").strip()
            if text.startswith("speed=") and text[6:].strip() not in ("", "N/A"):
                try:
                    self.speed = float(text[6:].rstrip("x"))
                except ValueError:
                    pass

    def _read_errors(self):
        for line in self.proc.stderr:
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                self.errors = (self.errors + [text])[-3:]

    def stop(self, now=False):
        """Stop ffmpeg. Normally it is allowed to finish the segment it is writing (up to 10 s). With now=True
        it is killed at once, for a preview that has already failed."""
        self._closing = True
        if self.proc is not None:
            if now and self.proc.poll() is None:
                try:
                    self.proc.kill()
                except OSError:
                    pass
            try:
                self.q.put_nowait(None)
            except queue.Full:
                pass
            try:
                self.proc.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def status(self):
        return {"state": self.state, "note": self.note, "speed": self.speed,
                "errors": list(self.errors) if self.state.startswith("stopped") else []}

    def file_path(self, name):
        """Only plain file names inside the HLS folder are served."""
        if not SAFE_NAME.match(name):
            return None
        path = os.path.join(self.dir, name)
        return path if os.path.isfile(path) else None
