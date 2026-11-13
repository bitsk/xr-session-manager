"""QVRServiceTuning OV7251 exposure index policy recovered from stock binaries.

Source: libqvrservice.so f8b3b7582fe696c302ce1ab2a026ab13089f390d4011730733ddf395a257bbc7,
ComputeExposureIdxAdjustment ELF 0xf93f0 and ComputeNewExposureIdx 0xf9544.
The accompanying table comes from LoadTuningData in the stock ov7251 tuning
library. This module computes requests only. ISO gain is not a V4L2 register;
the camera owner must translate and associate applied settings with frames.
"""
import json
import math
from pathlib import Path
import struct


def stereo_luma(left, right):
    """Stock splitImageLuma: central 75% of rows, brighter eye's integer mean.

    REA entry 0x10998c (ELF 0xf998c), 1280x480 side-by-side Y8 input.
    Separate 640x480 buffers are equivalent because the crop is row-aligned.
    """
    means = []
    for pixels in (left, right):
        if len(pixels) != 640 * 480:
            raise ValueError('Expected two complete 640x480 Y8 images')
        means.append(sum(pixels[60 * 640:420 * 640]) // (360 * 640))
    return max(means), means


def _float32(value):
    return struct.unpack('<f', struct.pack('<f', value))[0]


class StockOv7251Ae:
    def __init__(self, table_path=None):
        path = Path(table_path) if table_path else Path('/usr/share/xr/session-manager/integration/stock-ov7251-ae.json')
        self.table = json.loads(path.read_text())
        if self.table['source_sha256'] != '96b4370826b4b9351e1c02ae015fe6f1168214aeb8ea0be85091a7cf12058ef6':
            raise ValueError('Unexpected stock OV7251 tuning table')
        self.index = self.table['initial_index']

    def request(self):
        return dict(index=self.index, **self.table['entries'][self.index])

    def update(self, luma_sum, pixel_count):
        """Advance only with pixels from the last successfully applied request.

        Matches stock integer mean, tolerance, float32 rounding and index
        bounds. The caller owns startup/settling and stale frame rejection.
        """
        if pixel_count <= 0 or luma_sum < 0 or luma_sum > 255 * pixel_count:
            raise ValueError('Expected a real Y8 pixel sum and nonzero pixel count')
        luma = luma_sum // pixel_count
        target = self.table['luma_target']
        if abs(luma - target) > self.table['luma_tolerance']:
            adjustment = _float32(math.log10(target / max(luma, 1)) * _float32(77.9))
            adjustment = int(_float32(adjustment * self.table['convergence_speed']))
            self.index = max(0, min(len(self.table['entries']) - 1, self.index + adjustment))
        return dict(frame_luma=luma, **self.request())
