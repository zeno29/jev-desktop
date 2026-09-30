"""One-time setup for offline voice.

    python -m jev_assistant.voice.setup_local            Hinglish speech-to-text model
    python -m jev_assistant.voice.setup_local --kokoro   also the Kokoro offline voice (~350 MB)

Speech-to-text: converts Oriserve/Whisper-Hindi2Hinglish-Swift (Apache-2.0; Whisper-base
fine-tuned to write Hindi/Hinglish in Roman script) to CTranslate2 int8 for faster-whisper, in
data/models/whisper-hinglish-ct2. The converter needs PyTorch + transformers only for this step,
so they're installed into a temporary folder that is deleted afterwards, not into your venv.
If that fails, offline speech-to-text keeps using stock Whisper "base".

Needs: pip install -r requirements-local-voice.txt
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.request

from ..config import DATA_DIR
from .stt_local import HINGLISH_MODEL_DIR

HINGLISH_REPO = "Oriserve/Whisper-Hindi2Hinglish-Swift"
KOKORO_DIR = DATA_DIR / "models" / "kokoro"
KOKORO_FILES = {  # from the kokoro-onnx project's releases (model weights: Apache-2.0)
    # fp32: the int8 build was ~3x slower here (5 s for "Opening Spotify." on an i5-1240P)
    "kokoro-v1.0.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
    "voices-v1.0.bin": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
}

CONVERT = f"""
from ctranslate2.converters import TransformersConverter
TransformersConverter({HINGLISH_REPO!r}, copy_files=["tokenizer.json", "preprocessor_config.json"]).convert(
    {str(HINGLISH_MODEL_DIR)!r}, quantization="int8", force=True)
"""


def setup_hinglish() -> bool:
    if (HINGLISH_MODEL_DIR / "model.bin").exists():
        print(f"Hinglish model already set up: {HINGLISH_MODEL_DIR}")
        return True
    deps = DATA_DIR / ".convert-deps"
    env = dict(os.environ)
    try:
        try:
            import torch, transformers  # noqa: F401,E401  (already installed: use them)
        except ImportError:
            print("Installing PyTorch + transformers into a temporary folder for the conversion (~250 MB)...")
            subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--target", str(deps),
                            "torch", "transformers"], check=True)
            env["PYTHONPATH"] = str(deps) + os.pathsep + env.get("PYTHONPATH", "")
        print(f"Converting {HINGLISH_REPO} to CTranslate2 int8...")
        subprocess.run([sys.executable, "-c", CONVERT], check=True, env=env)
    except (subprocess.CalledProcessError, OSError) as e:
        print(f"\nConversion failed ({e}). Offline speech-to-text will use Whisper 'base' instead.")
        shutil.rmtree(HINGLISH_MODEL_DIR, ignore_errors=True)
        return False
    finally:
        shutil.rmtree(deps, ignore_errors=True)
    print(f"Done: {HINGLISH_MODEL_DIR}  (used automatically when local_stt_model = \"auto\")")
    return True


def setup_kokoro() -> None:
    try:
        import kokoro_onnx  # noqa: F401
    except ImportError:
        print("kokoro-onnx isn't installed: pip install -r requirements-local-voice.txt")
        return
    KOKORO_DIR.mkdir(parents=True, exist_ok=True)
    for name, url in KOKORO_FILES.items():
        target = KOKORO_DIR / name
        if target.exists():
            continue
        print(f"Downloading {name}...")
        tmp = target.with_suffix(".part")
        urllib.request.urlretrieve(url, tmp)
        tmp.replace(target)
    print(f"Kokoro voice ready: {KOKORO_DIR}  (set tts_provider = \"kokoro\" or leave \"auto\")")


def main(argv: list[str]) -> None:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        print("Install the offline voice packages first: pip install -r requirements-local-voice.txt")
        return
    setup_hinglish()
    if "--kokoro" in argv:
        setup_kokoro()


if __name__ == "__main__":
    main(sys.argv[1:])
