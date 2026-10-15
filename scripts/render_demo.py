#!/usr/bin/env python3
"""Render a deck built by make_demo.py into PNG frames, an animated GIF and a PDF (headless Chromium + Pillow).

    nix develop .#demo -c python3 scripts/render_demo.py demo/HumanEval_42
"""
import json
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

# seconds each phase stays on screen in the GIF
DURATIONS = {"title": [7.0], "arm": [4.5], "note": [4.0], "verdict": [6.5],
             "llm": [1.8, 7.0, 3.8], "verifier": [2.8, 6.0]}
W, H = 1280, 720


def chromium() -> str:
    for name in ("chromium", "chromium-browser", "google-chrome"):
        if shutil.which(name):
            return name
    sys.exit("chromium not found: run inside `nix develop .#demo`")


def shoot(browser: str, url: str, png: Path, profile: str):
    subprocess.run([browser, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                    f"--user-data-dir={profile}", "--force-device-scale-factor=1", f"--window-size={W},{H}",
                    "--virtual-time-budget=2000", f"--screenshot={png}", url],
                   check=True, capture_output=True, timeout=120)


def main():
    d = Path(sys.argv[1]).resolve()
    deck = d / "deck.html"
    manifest = json.loads((d / "slides.json").read_text())
    frames_dir = d / "frames"
    frames_dir.mkdir(exist_ok=True)
    for f in frames_dir.glob("*.png"):
        f.unlink()
    browser = chromium()
    jobs = []
    for m in manifest:
        for p in range(m["phases"]):
            png = frames_dir / f"s{m['slide']:02d}_p{p}.png"
            # the last phase of a slide shows everything (p=9)
            phase = 9 if p == m["phases"] - 1 else p
            jobs.append((f"file://{deck}#s={m['slide']}&p={phase}", png,
                         DURATIONS.get(m["kind"], [5.0])[min(p, len(DURATIONS.get(m['kind'], [5.0])) - 1)]))

    def run(job):
        url, png, _ = job
        with tempfile.TemporaryDirectory() as prof:
            shoot(browser, url, png, prof)
        return png

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(run, jobs))
    print(f"rendered {len(jobs)} frames")

    imgs = [Image.open(p).convert("RGB").crop((0, 0, W, H)) for _, p, _ in jobs]
    # one shared adaptive palette keeps the GIF small and the colours stable between frames
    pal_src = Image.new("RGB", (W, H * min(len(imgs), 8)))
    step = max(1, len(imgs) // 8)
    for i, im in enumerate(imgs[::step][:8]):
        pal_src.paste(im, (0, H * i))
    palette = pal_src.quantize(colors=128, method=Image.Quantize.MEDIANCUT)
    q = [im.quantize(palette=palette, dither=Image.Dither.NONE) for im in imgs]
    gif = d / "demo.gif"
    q[0].save(gif, save_all=True, append_images=q[1:], duration=[int(t * 1000) for *_, t in jobs], loop=0,
              optimize=True, disposal=1)
    print(f"wrote {gif} ({gif.stat().st_size / 1e6:.1f} MB, {sum(t for *_, t in jobs):.0f}s)")

    pdf = d / "demo.pdf"
    with tempfile.TemporaryDirectory() as prof:
        subprocess.run([browser, "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run", f"--user-data-dir={prof}",
                        "--no-pdf-header-footer", f"--print-to-pdf={pdf}", "--virtual-time-budget=3000",
                        f"file://{deck}?print"], check=True, capture_output=True, timeout=180)
    print(f"wrote {pdf} ({pdf.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
