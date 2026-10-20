# -*- coding: utf-8 -*-
"""PyInstaller entry point.

PyInstaller runs its entry file as a top-level script, so pointing it at
zkbridge/__main__.py leaves that file's relative imports with no parent
package and the frozen program dies on its first line. Importing the package
from here keeps `python -m zkbridge` and the frozen build on the same code.
"""
import sys

from zkbridge.__main__ import main

if __name__ == '__main__':
    sys.exit(main())
