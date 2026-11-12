# SPDX-License-Identifier: GPL-3.0-or-later
"""Return the session dashboard after a real OpenVR scene application exits."""
import ctypes as c
from pathlib import Path
import subprocess


class SceneReturn:
    # The public IVRApplications_007 C function table has 30 entries. These
    # slots are GetSceneApplicationState and GetCurrentSceneProcessId.
    INTERFACE = b'FnTable:IVRApplications_007'

    def __init__(self, runtime, environment, logs):
        self.runtime = Path(runtime)
        self.environment = environment
        self.logs = Path(logs)
        self.library = c.CDLL(str(self.runtime / 'libopenvr_api.so'))
        init = self.library.VR_InitInternal2
        init.argtypes = [c.POINTER(c.c_int), c.c_int, c.c_char_p]
        init.restype = c.c_uint32
        get = self.library.VR_GetGenericInterface
        get.argtypes = [c.c_char_p, c.POINTER(c.c_int)]
        get.restype = c.c_void_p
        self.shutdown = self.library.VR_ShutdownInternal
        self.shutdown.argtypes = []
        self.shutdown.restype = None
        error = c.c_int()
        if not init(c.byref(error), 3, None) or error.value:
            raise RuntimeError('OpenVR scene observer initialization failed: ' + str(error.value))
        self.initialized = True
        table = get(self.INTERFACE, c.byref(error))
        if not table or error.value:
            self.close()
            raise RuntimeError('OpenVR applications interface unavailable: ' + str(error.value))
        functions = c.cast(table, c.POINTER(c.c_void_p * 30)).contents
        self.get_state = c.CFUNCTYPE(c.c_int)(functions[25])
        self.get_pid = c.CFUNCTYPE(c.c_uint32)(functions[29])
        self.seen_pid = 0
        self.idle_since = None
        self.next_poll = 0

    def poll(self, now):
        if now < self.next_poll:
            return None
        self.next_poll = now + 1
        pid, state = self.get_pid(), self.get_state()
        if pid:
            self.seen_pid = pid
            self.idle_since = None
        elif state != 0:
            # Starting/quitting/waiting are application transitions, not idle.
            self.idle_since = None
        elif self.seen_pid:
            if self.idle_since is None:
                self.idle_since = now
            elif now - self.idle_since >= 1:
                ended = self.seen_pid
                with (self.logs / ('scene-return-' + str(ended) + '.log')).open('w') as log:
                    result = subprocess.run(
                        [str(self.runtime / 'vrcmd'), '--showdashboard', 'valve.steam.gamepadui.main'],
                        env=self.environment, timeout=8, stdout=log, stderr=subprocess.STDOUT)
                if result.returncode:
                    raise RuntimeError('Returning the dashboard failed: ' + str(result.returncode))
                self.seen_pid = 0
                self.idle_since = None
                return {'phase': 'scene_dashboard_return', 'ended_scene_pid': ended,
                        'scene_state': state, 'exit': result.returncode}
        return None

    def close(self):
        if getattr(self, 'initialized', False):
            self.initialized = False
            self.shutdown()
