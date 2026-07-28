"""Per-language kind tables driving the shared walker.

Each module here is data plus small single-node callables. None of them
traverses a subtree -- that lives in walker.py alone, which is what makes the
no-recursion guarantee checkable by reading one file.
"""

from __future__ import annotations
