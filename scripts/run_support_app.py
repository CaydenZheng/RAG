"""Start the normal HTTP application with an explicit support profile."""

import argparse
import os
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    profile = args.profile.resolve(strict=True)
    os.environ['AGENT_SUPPORT_PROFILE'] = str(profile)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import uvicorn

    uvicorn.run('app:app', host=args.host, port=args.port)


if __name__ == '__main__':
    main()
