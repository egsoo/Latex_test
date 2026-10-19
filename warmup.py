"""Compile the default preamble once at startup so Tectonic caches all packages."""
import threading
import tempfile
import subprocess
from pathlib import Path

DEFAULT_PREAMBLE = """\\documentclass{article}
\\usepackage[a6paper]{geometry}
\\usepackage[T1]{fontenc}
\\usepackage[utf8]{inputenc}
\\usepackage{lmodern}
\\usepackage{textcomp}
\\usepackage{lastpage}
\\usepackage{amsmath}
\\usepackage{amsfonts}
\\usepackage{amssymb}
\\usepackage{physics}
\\usepackage{lipsum}
\\pagenumbering{gobble}
\\begin{document}
$x^2 + \\int_0^1 y\\,dy$
\\end{document}
"""


def warmup():
    try:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "warm.tex").write_text(DEFAULT_PREAMBLE, encoding="utf-8")
            subprocess.run(["tectonic", "-o", "build", "warm.tex"], cwd=tmp, capture_output=True, timeout=300)
            print("Tectonic cache warmed up.")
    except Exception as e:  # noqa: BLE001
        print(f"Warmup skipped: {e}")


def start_warmup_thread():
    threading.Thread(target=warmup, daemon=True).start()
