# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional hand consumer lifecycle; head tracking keeps camera ownership."""
import subprocess,time

class HandSession:
    def __init__(self,profile):
        self.config=profile.get('hand_tracking',{})
        self.enabled=self.config.get('enabled',False)
        self.started=False
        self.last_check=0.0
        self.result={'requested':self.enabled,'started':False,'pass':True}
    def environment(self):
        return {'A081_HAND_INPUT_SOCKET':self.config['camera_socket']} if self.enabled else {}
    def start(self):
        if not self.enabled:return
        unit=self.config['unit']
        if subprocess.run(['systemctl','is-active','--quiet',unit]).returncode==0:
            raise RuntimeError('Hand consumer already owned by another session')
        subprocess.run(['systemctl','reset-failed',unit],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        subprocess.run(['systemctl','start',unit],check=True)
        self.started=True;self.result.update(started=True)
    def check(self):
        now=time.monotonic()
        if now-self.last_check<1 or not self.result['pass']:return
        self.last_check=now
        if self.started and subprocess.run(['systemctl','is-active','--quiet',self.config['unit']]).returncode:
            # The independent consumer's failure releases its input. It must not
            # tear down the sole camera/IMU owner or block head SLAM.
            self.result.update({'pass':False,'error':'hand_consumer_inactive'})
    def stop(self):
        if self.started:
            rc=subprocess.run(['systemctl','stop',self.config['unit']],timeout=20).returncode
            state=subprocess.check_output(['systemctl','show',self.config['unit'],'--property=Result,ExecMainStatus'],text=True)
            terminal=dict(line.split('=',1) for line in state.splitlines() if '=' in line)
            self.result.update(stop_exit=rc,terminal=terminal)
            self.result['pass']=self.result['pass'] and rc==0 and terminal.get('Result')=='success' and terminal.get('ExecMainStatus')=='0'
        return self.result
