"""Fetch the pinned CPU engine source and omit speech text from its diagnostics."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

REPO = "gabriele-mastrapasqua/qwen3-tts"
REVISION = "ef339be58a778b062e1c14382347964552eae007"
OUTPUT = Path(__file__).resolve().parents[1] / "deploy/.private/verification/native-build"


def git_blob(body):
    return hashlib.sha1(f"blob {len(body)}\0".encode() + body).hexdigest()


def main():
    with httpx.Client(timeout=45, follow_redirects=True) as client:
        response = client.get(f"https://api.github.com/repos/{REPO}/git/trees/{REVISION}?recursive=1")
        response.raise_for_status()
        tree = response.json()["tree"]
    files = [entry for entry in tree if entry["type"] == "blob" and (
        ("/" not in entry["path"] and (entry["path"].endswith((".c", ".h")) or entry["path"] in {"Makefile", "LICENSE"}))
        or entry["path"].startswith(("vendor/", "third_party/ingot/src/", "third_party/ingot/include/"))
        or entry["path"] in {"third_party/ingot/Makefile", "third_party/ingot/LICENSE", "tools/source_fingerprint.sh"}
    )]
    target = OUTPUT / "native-src"

    def fetch(entry):
        path = target / entry["path"]
        if path.exists() and git_blob(path.read_bytes()) == entry["sha"]:
            return
        with httpx.Client(timeout=45, follow_redirects=True) as client:
            response = client.get(f"https://raw.githubusercontent.com/{REPO}/{REVISION}/{entry['path']}")
            response.raise_for_status()
        if git_blob(response.content) != entry["sha"]:
            raise RuntimeError(f"Git blob mismatch: {entry['path']}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(response.content)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(fetch, files))
    server = target / "qwen_tts_server.c"
    original = 'fprintf(stderr, "[HTTP] TTS: \\"%s\\" (speaker=%d, lang=%d, seed=%u)\\n",\n            text, ctx->speaker_id, ctx->language_id, ctx->seed);'
    redacted = 'fprintf(stderr, "[HTTP] TTS: %zu bytes (speaker=%d, lang=%d, seed=%u)\\n",\n            strlen(text), ctx->speaker_id, ctx->language_id, ctx->seed);'
    source = server.read_text("utf-8")
    if source.count(original) != 1:
        raise RuntimeError("Pinned source no longer matches the speech-log patch")
    server.write_text(source.replace(original, redacted), "utf-8", newline="\n")
    (target / ".source_fingerprint").write_text(f"{REVISION}:cpu-source-subset-no-speech-log\n", "utf-8")
    (OUTPUT / "source-manifest.json").write_text(json.dumps({
        "repo": REPO, "revision": REVISION, "files": files,
        "local_patch": "replace speech content in HTTP log with byte count",
        "patched_server_sha256": hashlib.sha256(server.read_bytes()).hexdigest(),
    }, indent=2), "utf-8")
    print(f"Verified {len(files)} source files; speech-log patch applied: {OUTPUT}")


if __name__ == "__main__":
    main()
