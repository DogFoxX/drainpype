"""Allow ``python -m uploader`` and frozen PyInstaller builds."""
import sys

from uploader.cli import main

if __name__ == "__main__":
    sys.exit(main())