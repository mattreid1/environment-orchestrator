"""Guest credential_process client. Never contains host source AWS keys."""
import json
from pathlib import Path
import re
import sys
import urllib.request


def main():
    value = json.loads((Path.home() / ".aws/aime-bridge.json").read_text())
    if set(value) != {"url", "workspace", "token"} or not re.fullmatch(r"http://172\.30\.78\.[0-9]{1,3}:6092/credentials", value["url"]):
        raise ValueError("Invalid bridge configuration.")
    request = urllib.request.Request(value["url"],
        data=json.dumps({"workspace": value["workspace"], "token": value["token"]}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    # Ignore proxy environment variables: credentials must stay on the VM link.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=60) as response:
        result = json.loads(response.read(16384))
    if set(result) != {"Version", "AccessKeyId", "SecretAccessKey", "SessionToken", "Expiration"} or result["Version"] != 1:
        raise ValueError("Invalid credential response.")
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("AIME AWS credentials unavailable. Ask the host operator to check authentication and workspace access.", file=sys.stderr)
        raise SystemExit(1)
