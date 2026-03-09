#!/usr/bin/env python3
"""
Generate an app icon for Claude Bot using Dreamer's ComfyUI cloud pipeline.

Uses Z-Image Turbo on Comfy Cloud to generate a clean, modern app icon,
then resizes it into Android mipmap density variants.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

# Reuse Dreamer's cloud client
DREAMER_ROOT = Path("/home/kafar/dreamer")
sys.path.insert(0, str(DREAMER_ROOT / "tools"))

from comfyui.cloud_client import (
    download_image,
    fetch_images,
    queue_prompt,
    wait_for_completion,
)

WORKFLOW_PATH = DREAMER_ROOT / "tools" / "comfyui" / "workflows" / "workflow_character_base_zimage.json"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "android" / "app" / "src" / "main" / "res"


def load_workflow(path: Path) -> dict:
    with open(path, "r") as f:
        data = json.load(f)
    data.pop("_meta", None)
    for node in data.values():
        if isinstance(node, dict):
            node.pop("_comment", None)
    return data


def substitute(workflow: dict, replacements: dict) -> dict:
    raw = json.dumps(workflow)
    for key, value in replacements.items():
        if key == "seed":
            raw = raw.replace(f'"{{{{{key}}}}}"', value)
        else:
            escaped = json.dumps(value)[1:-1]
            raw = raw.replace(f"{{{{{key}}}}}", escaped)
    return json.loads(raw)


def generate_icon():
    print("Loading Z-Image Turbo workflow...")
    wf = load_workflow(WORKFLOW_PATH)

    prompt = (
        "app icon design, a friendly cute robot assistant face, "
        "round happy eyes with warm orange glow, small smile, "
        "soft rounded features, modern flat design, "
        "smooth dark teal gradient background, "
        "clean minimalist style, centered composition, "
        "professional app icon, rounded square shape, "
        "warm amber and teal color palette, "
        "digital art, high quality, sharp details, no text, no letters"
    )

    seed = str(random.randint(0, 2**32 - 1))

    wf = substitute(wf, {
        "positive_prompt": prompt,
        "seed": seed,
        "character_id": "claude_bot_icon",
    })

    print(f"Submitting to Comfy Cloud (seed={seed})...")
    prompt_id = queue_prompt(wf)
    print(f"Job queued: {prompt_id}")

    print("Waiting for completion...")
    wait_for_completion(prompt_id)
    print("Job completed!")

    images = fetch_images(prompt_id)
    if not images:
        print("ERROR: No images returned!")
        sys.exit(1)

    img = images[0]
    raw_output = OUTPUT_DIR / "raw_icon.png"
    raw_output.parent.mkdir(parents=True, exist_ok=True)

    print(f"Downloading image: {img['filename']}...")
    download_image(img["filename"], img.get("subfolder", ""), raw_output)
    print(f"Saved raw icon to: {raw_output}")

    # Generate Android mipmap variants
    try:
        from PIL import Image
        generate_mipmaps(raw_output)
    except ImportError:
        print("\nPillow not installed. Install with: pip install Pillow")
        print("Then run: python tools/generate-icon-mipmaps.py")
        print(f"Raw icon saved at: {raw_output}")


def generate_mipmaps(source: Path):
    from PIL import Image

    img = Image.open(source).convert("RGBA")

    # Crop to center square if not already square
    w, h = img.size
    if w != h:
        size = min(w, h)
        left = (w - size) // 2
        top = (h - size) // 2
        img = img.crop((left, top, left + size, top + size))

    # Android adaptive icon: foreground is 108dp, visible area is 72dp
    # We need the full 108dp versions for adaptive icons
    densities = {
        "mipmap-mdpi": 48,
        "mipmap-hdpi": 72,
        "mipmap-xhdpi": 96,
        "mipmap-xxhdpi": 144,
        "mipmap-xxxhdpi": 192,
    }

    for folder, size in densities.items():
        out_dir = OUTPUT_DIR / folder
        out_dir.mkdir(parents=True, exist_ok=True)

        resized = img.resize((size, size), Image.LANCZOS)
        resized.save(out_dir / "ic_launcher.png")
        print(f"  {folder}/ic_launcher.png ({size}x{size})")

    print("\nMipmap icons generated!")


if __name__ == "__main__":
    generate_icon()
