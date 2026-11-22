"""
Lightroom Workflow Manager
==========================
Check a Lightroom Classic catalog out to a portable SSD for editing on a
travel laptop, and check it back in to the main PC afterwards.

Usage:
  python lr_workflow.py                 Suggest the next step
  python lr_workflow.py --leave         Check the catalog out to the SSD
  python lr_workflow.py --arrive        Bring the catalog and new photos back
  python lr_workflow.py --status        Show where the catalog is
  python lr_workflow.py --help          All options (add --dry-run to preview)

Settings live in config.json next to this file (see config.example.json).

DISCLAIMER: This script is provided "as-is" without warranty.
The author takes no responsibility for any damage caused by its usage.
"""

import sys

if sys.version_info < (3, 8):
    sys.exit("Python 3.8 or newer is required.")

from lrwm.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
