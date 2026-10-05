"""Local entry point, including environments that mark editable .pth files hidden."""

import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
sys.path.insert(0, str(root / "src"))
os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(root / "work/tokenizer-cache"))
from goalkeeper.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
