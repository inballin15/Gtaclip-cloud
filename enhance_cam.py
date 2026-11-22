"""AI-upscale one webcam source (≈270 px wide) to the 1080-wide top strip with Real-ESRGAN (general-x4v3, light denoise + real texture).
Runs on its own GitHub server per clip (CPU, ~1 s per frame). usage: python enhance_cam.py in.mp4 out.mp4 models_dir"""
import subprocess
import sys
import time

import cv2
import imageio_ffmpeg
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

torch.set_num_threads(4)


class SRVGG(nn.Module):   # Real-ESRGAN's compact network (realesr-general-x4v3)
    def __init__(self, nf=64, nc=32, up=4):
        super().__init__()
        self.up = up
        b = [nn.Conv2d(3, nf, 3, 1, 1), nn.PReLU(nf)]
        for _ in range(nc):
            b += [nn.Conv2d(nf, nf, 3, 1, 1), nn.PReLU(nf)]
        b += [nn.Conv2d(nf, 3 * up * up, 3, 1, 1)]
        self.body = nn.ModuleList(b)
        self.ups = nn.PixelShuffle(up)

    def forward(self, x):
        o = x
        for layer in self.body:
            o = layer(o)
        return self.ups(o) + F.interpolate(x, scale_factor=self.up, mode="nearest")


def load(models: str, denoise: float = 0.5) -> nn.Module:
    sa = torch.load(f"{models}/x4v3.pth", map_location="cpu")
    sb = torch.load(f"{models}/wdn.pth", map_location="cpu")
    sa, sb = sa.get("params", sa), sb.get("params", sb)
    m = SRVGG()
    m.load_state_dict({k: (1 - denoise) * sa[k] + denoise * sb[k] for k in sa})
    return m.eval()


DENOISE = 0.82   # weight of the detail-keeping model: 0.5 made faces waxy/smeared, this keeps skin and hair texture
BLEND = 0.22     # mix a little of the plain sharp scale back in so it looks like a real camera, not a painting


def main(src: str, dst: str, models: str) -> None:
    model = load(models, DENOISE)
    cap = cv2.VideoCapture(src)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    W, H = w * 4, h * 4
    ff = subprocess.Popen([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "rawvideo",
                           "-pix_fmt", "bgr24", "-s", f"{W}x{H}", "-r", str(fps), "-i", "-", "-c:v", "libx264",
                           "-preset", "fast", "-crf", "15", "-pix_fmt", "yuv420p", dst], stdin=subprocess.PIPE)
    t0, n, batch = time.time(), 0, []

    def flush():
        if not batch:
            return
        t = torch.stack([torch.from_numpy(cv2.cvtColor(f, cv2.COLOR_BGR2RGB)).permute(2, 0, 1).float().div(255)
                         for f in batch])
        with torch.inference_mode():
            o = model(t).clamp(0, 1).mul(255).round().byte().permute(0, 2, 3, 1).numpy()
        for f, src_f in zip(o, batch):
            out = cv2.cvtColor(f, cv2.COLOR_RGB2BGR)
            plain = cv2.resize(src_f, (out.shape[1], out.shape[0]), interpolation=cv2.INTER_LANCZOS4)
            ff.stdin.write(cv2.addWeighted(out, 1 - BLEND, plain, BLEND, 0).tobytes())
        batch.clear()

    while True:
        ok, f = cap.read()
        if not ok:
            break
        batch.append(f)
        n += 1
        if len(batch) == 4:
            flush()
    flush()
    ff.stdin.close()
    ff.wait()
    print(f"upscaled {n} frames {w}x{h} -> {W}x{H} in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main(*sys.argv[1:4])
