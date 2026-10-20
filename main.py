"""LaTeX compile microservice for the InLaTeXbot Lovable port.

Faithful port of InLaTeXbot's src/LatexConverter.py rendering pipeline:
  gs bbox -> padded bbox -> aspect-corrected PNG box (negated translate)
  -> pngalpha -g render -> separate RAW-bbox CropBox for the PDF.

POST /compile  {document: str, dpi: int, return_pdf: bool}
  -> 200 {"png": "<base64>", "pdf": "<base64 or null>", "width_px": int, "height_px": int}
  -> 400 {"detail": "<LaTeX error message>"}
GET  /health -> {"status": "ok"}   (pinger target)
"""
import base64
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from warmup import start_warmup_thread

app = FastAPI()


@app.on_event('startup')
def on_startup():
    start_warmup_thread()

TECTONIC_TIMEOUT = 90
GS_TIMEOUT = 60


class CompileRequest(BaseModel):
    document: str = Field(min_length=1, max_length=50000)
    dpi: int = Field(default=300, ge=100, le=1000)
    return_pdf: bool = False


def _run(cmd, cwd: str, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def _latex_error(output: str) -> str | None:
    """Extract the first '! ...' LaTeX error block from compiler output."""
    lines = output.splitlines()
    for idx, line in enumerate(lines):
        if line.startswith("! "):
            chunk = "\n".join(lines[idx: idx + 3])
            return chunk[:500]
    for line in lines:
        if line.startswith("error:"):
            return line[:500]
    return None


def _tectonic(workdir: str, tex_name: str) -> None:
    # Original bot runs pdflatex -interaction=nonstopmode; Tectonic is the
    # Docker-friendly stand-in, everything downstream is identical.
    proc = _run(["tectonic", "-o", "build", f"src/{tex_name}"], workdir, TECTONIC_TIMEOUT)
    pdf = Path(workdir) / "build" / tex_name.replace(".tex", ".pdf")
    if not pdf.exists():
        msg = _latex_error((proc.stdout or "") + (proc.stderr or "")) or \
            "LaTeX compilation failed (no output produced)."
        raise ValueError(msg)


def _gs_bbox(workdir: str, pdf_name: str) -> list[int]:
    """Run Ghostscript's bbox device, exactly like the original bot."""
    proc = _run(["/bin/sh", "-c",
                 "gs -q -dBATCH -dNOPAUSE -sDEVICE=bbox build/" + pdf_name],
                workdir, GS_TIMEOUT)
    out = (proc.stdout or "") + (proc.stderr or "")
    try:
        line = next(l for l in out.splitlines() if l.startswith("%%BoundingBox:"))
        bounds = [int(v) for v in line.split(":")[1].split()]
    except (StopIteration, ValueError):
        raise ValueError("Could not extract bounding box! Empty expression?")
    if bounds[0] == bounds[2] or bounds[1] == bounds[3]:
        raise ValueError("Empty expression!")
    return bounds


def _bounding_box(workdir: str, pdf_name: str, dpi: int):
    """Padded bounding box. Mirrors LatexConverter.extractBoundingBox:
    width/height are padded page sizes in pixels, and the translation is
    returned NEGATED (original returns -translation_x, -translation_y)."""
    bounds = _gs_bbox(workdir, pdf_name)

    hpad = 0.25 * 72  # 72 postscript points = 1 inch
    vpad = 0.1 * 72
    llc = bounds[:2]
    llc[0] -= hpad
    llc[1] -= vpad
    ruc = bounds[2:]
    ruc[0] += hpad
    ruc[1] += vpad

    size_factor = dpi / 72
    width = (ruc[0] - llc[0]) * size_factor
    height = (ruc[1] - llc[1]) * size_factor
    translation_x = llc[0]
    translation_y = llc[1]
    return width, height, -translation_x, -translation_y


def _correct_aspect_ratio(dpi: int, bbox, max_width_to_height: int = 3,
                          max_height_to_width: int = 1):
    """Mirrors LatexConverter.correctBoundingBoxAspectRaito (sic)."""
    width, height, translation_x, translation_y = bbox
    size_factor = dpi / 72
    if width > max_width_to_height * height:
        translation_y += (width / max_width_to_height - height) / 2 / size_factor
        height = width / max_width_to_height
    elif height > max_height_to_width * width:
        translation_x += (height / max_height_to_width - width) / 2 / size_factor
        width = height / max_height_to_width
    return width, height, translation_x, translation_y


def _crop_pdf(workdir: str, session: str) -> str:
    """Mirrors LatexConverter.cropPdf: the CropBox comes from a FRESH,
    RAW bbox run on the pdf (no padding, no aspect correction)."""
    bounds = _gs_bbox(workdir, f"expression_file_{session}.pdf")
    cropped = f"expression_file_cropped_{session}.pdf"
    cmd = (
        "gs -o build/" + cropped + " -sDEVICE=pdfwrite "
        f'-c "[/CropBox [{bounds[0]} {bounds[1]} {bounds[2]} {bounds[3]}]" '
        '-c " /PAGES pdfmark" -f build/' + f"expression_file_{session}.pdf"
    )
    _run(["/bin/sh", "-c", cmd], workdir, GS_TIMEOUT)
    return cropped


def _render_png(workdir: str, session: str, dpi: int, bbox) -> str:
    """Mirrors LatexConverter.convertPdfToPng: -g takes the (float-truncated)
    padded page size, and the Install translate receives the negated values."""
    w, h, tx, ty = bbox
    png = f"expression_{session}.png"
    cmd = (
        "gs -o build/" + png + f" -r{dpi} -sDEVICE=pngalpha -g{int(w)}x{int(h)} "
        f"-dLastPage=1 -c \"<</Install {{{int(tx)} {int(ty)} translate}}>> "
        f"setpagedevice\" -f build/expression_file_{session}.pdf"
    )
    _run(["/bin/sh", "-c", cmd], workdir, GS_TIMEOUT)
    return png


@app.post("/compile")
async def compile(req: CompileRequest, x_compile_token: str = Header(default="")):
    expected = os.environ.get("COMPILE_TOKEN", "")
    if expected and x_compile_token != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")
    session = uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix=f"latex_{session}_") as tmp:
        Path(tmp, "build").mkdir()
        src = Path(tmp, "src")
        src.mkdir()
        (src / f"expression_file_{session}.tex").write_text(req.document, encoding="utf-8")
        try:
            await run_in_threadpool(_tectonic, tmp, f"expression_file_{session}.tex")

            # Original order: bbox, then aspect-ratio correction, then PNG.
            bbox = await run_in_threadpool(
                _bounding_box, tmp, f"expression_file_{session}.pdf", req.dpi)
            bbox = await run_in_threadpool(_correct_aspect_ratio, req.dpi, bbox)
            png = await run_in_threadpool(_render_png, tmp, session, req.dpi, bbox)
            png_b64 = base64.b64encode((Path(tmp, "build") / png).read_bytes()).decode()

            pdf_b64 = None
            if req.return_pdf:
                cropped = await run_in_threadpool(_crop_pdf, tmp, session)
                pdf_b64 = base64.b64encode((Path(tmp, "build") / cropped).read_bytes()).decode()

            return {
                "png": png_b64,
                "pdf": pdf_b64,
                "width_px": int(bbox[0]),
                "height_px": int(bbox[1]),
            }
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=400, detail="Compilation timed out.")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"Compile service error: {e}")


@app.get("/health")
async def health():
    return {"status": "ok"}
