"""Attach a browser-enabled V1 session without putting credentials in argv."""

import argparse
import os
from pathlib import Path
import re
import sys

from .source import api_credentials_are_safe, read_server_credentials, validate_api_url


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--server-env", required=True)
    args = parser.parse_args(argv)
    if (validate_api_url(args.url) or not re.fullmatch(r"ses_[A-Za-z0-9_-]+", args.session)
            or not Path(args.directory).is_absolute() or not Path(args.directory).is_dir()
            or not Path(args.opencode).is_absolute()):
        print("Invalid browser-session attachment target", file=sys.stderr)
        return 1
    username, password = read_server_credentials(Path(args.server_env))
    environment = dict(os.environ)
    username = environment.get("OPENCODE_SERVER_USERNAME") or username or "opencode"
    password = environment.get("OPENCODE_SERVER_PASSWORD") or password
    if password and not api_credentials_are_safe(args.url):
        print("Refusing credentials over non-loopback HTTP", file=sys.stderr)
        return 1
    environment["OPENCODE_SERVER_USERNAME"] = username
    environment["OPENCODE_SERVER_PASSWORD"] = password
    command = [args.opencode, "attach", args.url, "--dir", args.directory, "--session", args.session]
    try:
        os.execvpe(args.opencode, command, environment)
    except OSError:
        print("Could not attach the browser-enabled session", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
