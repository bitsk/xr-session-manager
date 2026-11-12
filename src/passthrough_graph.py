# SPDX-License-Identifier: GPL-3.0-or-later
"""Configure IMX inputs on XR2 spare resources inside the camera owner's rig."""
import json
import os
import re


class PassthroughGraph:
    def __init__(self, run, graph, output):
        self.run, self.output = run, output
        self.slots = []
        self.saved_controls = []
        self.ready = False
        self.size = os.environ.get("A081_PASSTHROUGH_SIZE", "1928x1088")
        if self.size not in ("1928x1088", "1728x1728"):
            raise RuntimeError("Unsupported IMX crop")
        sensors = {}
        for block in re.split(r'^- entity \d+: ', graph, flags=re.M)[1:]:
            name = block.split(' (', 1)[0]
            if not re.fullmatch(r'imx471 \d+-001a', name):
                continue
            nodes = re.findall(r'^\s*device node name (/dev/v4l-subdev\d+)\s*$', block, re.M)
            links = re.findall(r'^\s*-> "msm_csiphy(\d+)":0 \[([^\]]*)\]', block, re.M)
            if len(nodes) != 1 or len(links) != 1 or not {'ENABLED', 'IMMUTABLE'} <= set(links[0][1].split(',')):
                raise RuntimeError('Ambiguous IMX sensor link: ' + name)
            phy = int(links[0][0])
            if phy in sensors:
                raise RuntimeError('Duplicate IMX PHY')
            sensors[phy] = dict(sensor_entity=name, sensor_subdev=nodes[0])
        if set(sensors) != {0, 1}:
            raise RuntimeError('Expected the IMX pair on PHY1/0')
        for slot, phy, pipeline in ((4, 1, 6), (5, 0, 5)):
            # The four tracking routes and QCFT identities remain 0..3.
            for entity in (f'msm_csid{pipeline}', f'msm_vfe{pipeline}_rdi0'):
                run(['media-ctl', '-e', entity])
            video = run(['media-ctl', '-e', f'msm_vfe{pipeline}_video0']).strip()
            if not re.fullmatch(r'/dev/video\d+', video):
                raise RuntimeError('Invalid IMX capture node')
            self.slots.append(dict(id=slot, phy=phy, csid=pipeline, vfe=pipeline, video=video, **sensors[phy]))
        (output / 'passthrough-mapping.json').write_text(json.dumps(self.slots, indent=2) + '\n')

    def configure(self):
        for slot in self.slots:
            i, phy, sensor, sd = slot['csid'], slot['phy'], slot['sensor_entity'], slot['sensor_subdev']
            # The optional indoor diagnostic values reuse the previously
            # device-verified independent IMX recipe. This is not an AE/ISP.
            if os.environ.get('A081_PASSTHROUGH_GAIN_PROFILE') == 'diagnostic-indoor':
                text = self.run(['v4l2-ctl', '-d', sd, '-C', 'analogue_gain,digital_gain'])
                controls = dict((name, int(value)) for name, value in re.findall(r'^(\w+): (\d+)', text, re.M))
                if set(controls) != {'analogue_gain', 'digital_gain'}:
                    raise RuntimeError('Could not save IMX gain controls')
                self.saved_controls.append((sd, controls))
                self.run(['v4l2-ctl', '-d', sd, '-c', 'analogue_gain=800,digital_gain=2048'])
            self.run(['media-ctl', '-l', f'"msm_csiphy{phy}":1 -> "msm_csid{i}":0 [1], '
                      f'"msm_csid{i}":1 -> "msm_vfe{i}_rdi0":0 [1]'])
            for entity, pad in ((sensor, 0), (f'msm_csiphy{phy}', 0),
                                (f'msm_csid{i}', 0), (f'msm_csid{i}', 1), (f'msm_vfe{i}_rdi0', 0)):
                self.run(['media-ctl', '-V', f'"{entity}":{pad} [fmt:SRGGB10_1X10/{self.size} field:none]'])
            width, height = self.size.split('x')
            self.run(['v4l2-ctl', '-d', slot['video'], f'--set-fmt-video=width={width},height={height},pixelformat=pRAA'])
        self.ready = True

    def restore(self):
        for node, controls in reversed(self.saved_controls):
            self.run(['v4l2-ctl', '-d', node, '-c', ','.join(f'{name}={value}' for name, value in controls.items())])
        self.saved_controls.clear()

