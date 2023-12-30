import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from queue import Empty, Queue
from subprocess import PIPE, Popen, STDOUT
from typing import List, Optional

import bpy
from .wakatime_downloader import getCliLocation
from .log import DEBUG, ERROR, INFO, log
from . import settings
from .utils import u
from .preferences import WakatimeProjectProperties


@lru_cache(maxsize=128)
def guess_project_name(
    filename: str,
    truncate_trail: str,
    use_project_folder: bool,
    project_prefix: str,
    project_postfix: str,
) -> str:
    # project-folder or blend-filename?
    if use_project_folder:
        # grab the name of the directory
        name = os.path.basename(os.path.dirname(filename))
    else:
        # cut away the (.blend) extension
        name = os.path.splitext(filename)[0]
        # remove (the full) path from the filename
        name = os.path.basename(name)
    # remove trailing characters (as configured in "Preferences")
    name = name.rstrip(truncate_trail)
    # tune project-name with pre- and postfix
    name = f"{project_prefix}{name}{project_postfix}"
    log(INFO, "project-name in Wakatime: {}", name)
    return name


@dataclass
class HeartBeat:
    entity: str
    project: str
    timestamp: float
    is_write: bool = False


class HeartbeatQueue(threading.Thread):
    POLL_INTERVAL = 1

    def __init__(self, version: str) -> None:
        super().__init__()
        self.daemon = True
        self._version = version
        self._queue = Queue()
        self._last_hb: Optional[HeartBeat] = None
        self._lock = threading.Lock()
        self._running = True

    def _enough_time_passed(self, now, is_write):
        props = WakatimeProjectProperties.instance()
        return self._last_hb is None or (
            now - self._last_hb.timestamp
            > (
                2
                if is_write
                else min(1, (2 if not props else props.heartbeat_frequency) * 60)
            )
        )

    def enqueue(self, filename: str, is_write=False):
        timestamp = time.time()
        last_file = self._last_hb.entity if self._last_hb is not None else ""
        if (
            not filename
            or filename == last_file
            and not self._enough_time_passed(timestamp, is_write)
        ):
            return
        props = WakatimeProjectProperties.instance()
        project_name = guess_project_name(
            filename,
            props.truncate_trail if props else "",
            props.use_project_folder if props else False,
            props.project_prefix if props else "",
            props.project_postfix if props else "",
        )
        self._last_hb = HeartBeat(filename, project_name, timestamp, is_write)
        self._queue.put_nowait(self._last_hb)

    def shutdown(self):
        self._stop()
        self._queue.put_nowait(None)

    @property
    def running(self):
        with self._lock:
            return self._running

    def _stop(self):
        with self._lock:
            self._running = False

    def _send_to_wakatime(
        self, heartbeat: HeartBeat, extra_heartbeats: Optional[List[HeartBeat]] = None
    ):
        ua = f"blender/{bpy.app.version_string.split()[0]} blender-wakatime/{self._version}"
        cmd = [
            getCliLocation(),
            "--entity",
            heartbeat.entity,
            "--time",
            f"{heartbeat.timestamp:f}",
            "--plugin",
            ua,
            "--category",
            "designing"
        ]
        props = WakatimeProjectProperties.instance()
        if props and props.always_overwrite_name:
            cmd.extend(["--project", heartbeat.project])
        else:
            cmd.extend(["--alternate-project", heartbeat.project])
        if heartbeat.is_write:
            cmd.append("--write")
        if settings.debug():
            cmd.append("--verbose")
        if extra_heartbeats:
            cmd.append("--extra-heartbeats")
            stdin = PIPE
        else:
            stdin = None
        log(DEBUG, " ".join(cmd))
        try:
            process = Popen(cmd, stdin=stdin, stdout=PIPE, stderr=STDOUT)
            inp = None
            if extra_heartbeats:
                inp = "{0}\n".format(
                    json.dumps([hb.__dict__ for hb in extra_heartbeats])
                )
                inp = inp.encode("utf-8")
            output, err = process.communicate(input=inp)
            output = u(output)
            retcode = process.poll()
            if (not retcode or retcode == 102 or retcode == 112) and not output:
                log(DEBUG, "OK")
            elif retcode == 104:  # wrong API key
                log(ERROR, "Wrong API key. Asking for a new one...")
                settings.set("api_key", "")
            else:
                log(ERROR, "Error")
            if retcode:
                log(
                    DEBUG if retcode == 102 or retcode == 112 else ERROR,
                    "wakatime-core exited with status: {}",
                    retcode,
                )
            if output:
                log(ERROR, "wakatime-core output: {}", u(output))
        except Exception:
            log(ERROR, u(sys.exc_info()[1]))

    def run(self):
        while self.running:
            time.sleep(self.POLL_INTERVAL)
            if not settings.api_key():
                continue
            try:
                heartbeat = self._queue.get_nowait()
            except Empty:
                continue
            if heartbeat is None:
                return
            extra_heartbeats = []
            try:
                while True:
                    extra = self._queue.get_nowait()
                    if extra is None:
                        self._stop()
                        break
                    extra_heartbeats.append(extra)
            except Empty:
                pass
            self._send_to_wakatime(heartbeat, extra_heartbeats)
