"""Entry point used by PyInstaller (it needs a script, not `-m videomerger`)."""

import sys

from videomerger.gui import main

if __name__ == "__main__":
    sys.exit(main())
