"""Allow ``python3 -m cachybench`` and, for convenience, ``python3 ./cachybench``.

The second form runs this file as a bare script with no parent package, so the
package directory's parent is put on ``sys.path`` and the CLI imported by its
absolute name.
"""

import sys

if __package__:
    from .cli import main
else:  # executed as ``python3 path/to/cachybench``
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from cachybench.cli import main

sys.exit(main())
