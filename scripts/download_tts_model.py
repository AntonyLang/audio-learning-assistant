"""Fetch pinned official Qwen weights into ignored local deployment data."""

import argparse
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "deploy" / ".private" / "models"
NAME = "qwen3-tts-0.6b-customvoice"
REPO = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
REVISION = "85e237c12c027371202489a0ec509ded67b5e4b5"
WEIGHT_HASHES = {
    "model.safetensors": "bc3c7e785eb961179c25450d1acff03f839e0002f2f3a5aeb67b5735c0fa2adb",
    "speech_tokenizer/model.safetensors": "836b7b357f5ea43e889936a3709af68dfe3751881acefe4ecf0dbd30ba571258",
}
WEIGHT_SIZES = {"model.safetensors": 1811626576, "speech_tokenizer/model.safetensors": 682293092}
FILES = [
    "config.json", "generation_config.json", "merges.txt", "model.safetensors",
    "preprocessor_config.json", "speech_tokenizer/config.json",
    "speech_tokenizer/configuration.json", "speech_tokenizer/model.safetensors",
    "speech_tokenizer/preprocessor_config.json", "tokenizer_config.json", "vocab.json",
]


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def download_mirror(client, url, partial, size):
    """Use bounded ranges because long model downloads stall on the current network."""
    chunk_size = 4 * 1024 * 1024

    def fetch(window):
        start, end = window
        for attempt in range(3):
            try:
                response = client.get(url, headers={"Range": f"bytes={start}-{end}"})
                break
            except httpx.TransportError:
                if attempt == 2:
                    raise
        response.raise_for_status()
        if (response.status_code != 206 or
                response.headers.get("Content-Range") != f"bytes {start}-{end}/{size}" or
                len(response.content) != end - start + 1):
            raise RuntimeError("Incomplete model range")
        return response.content

    offset = partial.stat().st_size if partial.exists() else 0
    with ThreadPoolExecutor(max_workers=4) as pool, partial.open("ab") as target:
        while offset < size:
            windows = [(start, min(start + chunk_size, size) - 1)
                       for start in range(offset, min(offset + 4 * chunk_size, size), chunk_size)]
            for data in pool.map(fetch, windows):
                target.write(data)
                offset += len(data)
            target.flush()
            print(f"Downloaded {offset}/{size} bytes", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--weights-source", choices=["huggingface", "modelscope"], default="huggingface")
    args = parser.parse_args()
    destination = DESTINATION / NAME
    records = []
    with httpx.Client(timeout=120, follow_redirects=True) as client, \
            httpx.Client(timeout=45, follow_redirects=True, trust_env=False,
                         transport=httpx.HTTPTransport(local_address="0.0.0.0")) as direct:
        for filename in FILES:
            path = destination / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            expected = WEIGHT_HASHES.get(filename)
            if not path.exists() or (expected and digest(path) != expected):
                print(f"Downloading {filename}", flush=True)
                partial = path.with_suffix(path.suffix + ".partial")
                url = f"https://huggingface.co/{REPO}/resolve/{REVISION}/{filename}"
                if expected and args.weights_source == "modelscope":
                    # The official mirror is accepted only when it matches the pinned HF digest.
                    url = f"https://modelscope.cn/models/{REPO}/resolve/master/{filename}"
                    download_mirror(direct, url, partial, WEIGHT_SIZES[filename])
                else:
                    offset = partial.stat().st_size if partial.exists() else 0
                    headers = {"Range": f"bytes={offset}-"} if offset else {}
                    with client.stream("GET", url, headers=headers) as response:
                        response.raise_for_status()
                        if offset and (response.status_code != 206 or not response.headers.get(
                                "Content-Range", "").startswith(f"bytes {offset}-")):
                            raise RuntimeError(f"Server did not resume {filename} at {offset}")
                        with partial.open("ab" if offset else "wb") as target:
                            for chunk in response.iter_bytes(1024 * 1024):
                                target.write(chunk)
                actual = digest(partial)
                if expected and actual != expected:
                    raise RuntimeError(f"SHA-256 mismatch: {filename}")
                partial.replace(path)
            records.append({"path": filename, "bytes": path.stat().st_size, "sha256": digest(path)})
            print(f"Verified {filename}: {path.stat().st_size} bytes", flush=True)
    (destination / "download-manifest.json").write_text(
        json.dumps({"repo": REPO, "revision": REVISION,
                    "weights_source": args.weights_source, "files": records}, indent=2), "utf-8"
    )
    print(f"Model ready: {destination}")


if __name__ == "__main__":
    main()
