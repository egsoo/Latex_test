"""LaTeX compile microservice for the InLaTeXbot Lovable port.

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


def _run(cmd: list, cwd: str, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def _latex_error(output: str) -> str | None:
    """Extract the first '! ...' LaTeX error block from compiler output."""
    lines = output.splitlines()
    for idx, line in enumerate(lines):
        if line.startswith("! "):
            chunk = "\n".join(lines[idx : idx + 3])
            return chunk[:500]
    for line in lines:
        if line.startswith("error:"):
            return line[:500]
    return None


def _tectonic(workdir: str, tex_name: str) -> None:
    proc = _run(["tectonic", "-o", "build", f"src/{tex_name}"], workdir, TECTONIC_TIMEOUT)
    pdf = Path(workdir) / "build" / tex_name.replace(".tex", ".pdf")
    if not pdf.exists():
        msg = _latex_error((proc.stdout or "") + (proc.stderr or "")) or "LaTeX compilation failed (no output produced)."
        raise ValueError(msg)


def _bounding_box(workdir: str, pdf_name: str, dpi: int):
    proc = _run(
        ["gs", "-q", "-dBATCH", "-dNOPAUSE", "-sDEVICE=bbox", f"build/{pdf_name}"],
        workdir, GS_TIMEOUT,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    try:
        line = next(l for l in out.splitlines() if l.startswith("%%BoundingBox:"))
        bounds = [int(v) for v in line.split(":")[1].split()]
    except (StopIteration, ValueError):
        raise ValueError("Could not extract bounding box! Empty expression?")

    if bounds[0] == bounds[2] or bounds[1] == bounds[3]:
        raise ValueError("Empty expression!")

    hpad, vpad = 0.25 * 72, 0.1 * 72  # 72 postscript points = 1 inch
    llc = [bounds[0] - hpad, bounds[1] - vpad]
    ruc = [bounds[2] + hpad, bounds[3] + vpad]

    size_factor = dpi / 72
    width = (ruc[0] - llc[0]) * size_factor
    height = (ruc[1] - llc[1]) * size_factor

    # Fix for Blank PNG: Bypass aspect-ratio correction for full documents.
    # 500 points is roughly 7 inches. If the content is taller than this, 
    # it's a full document, not a small equation snippet.
    is_full_page = (ruc[1] - llc[1]) > 500

    translation_x, translation_y = llc[0], llc[1]
    
    if not is_full_page:
        # Aspect-ratio correction (ported from InLaTeXbot LatexConverter)
        if width > 3 * height:
            translation_y += (width / 3 - height) / 2 / size_factor
            height = width / 3
        elif height > 1 * width:
            translation_x += (height - width) / 2 / size_factor
            width = height

    # Fix for Blank PDF: CropBox expects [llx, lly, urx, ury] in PostScript points.
    # The previous code was incorrectly passing [width, height, tx, ty].
    crop_box = [int(round(llc[0])), int(round(llc[1])), int(round(ruc[0])), int(round(ruc[1]))]

    return int(round(width)), int(round(height)), crop_box, int(round(translation_x)), int(round(translation_y))


def _crop_pdf(workdir: str, session: str, crop_box) -> str:
    cropped = f"expression_file_cropped_{session}.pdf"
    cmd = (
        "gs -o build/" + cropped + " -sDEVICE=pdfwrite "
        f'-c "[/CropBox [{crop_box[0]} {crop_box[1]} {crop_box[2]} {crop_box[3]}]" '
        '-c " /PAGES pdfmark" -f build/' + f"expression_file_{session}.pdf"
    )
    _run(["/bin/sh", "-c", cmd], workdir, GS_TIMEOUT)
    return cropped


def _render_png(workdir: str, session: str, dpi: int, width: int, height: int, tx: int, ty: int) -> str:
    png = f"expression_{session}.png"
    cmd = (
        "gs -o build/" + png + f" -r{dpi} -sDEVICE=pngalpha -g{width}x{height} -dLastPage=1 "
        f'-c "<</Install {{{tx} {ty} translate}}>> setpagedevice" -f build/'
        + f"expression_file_{session}.pdf"
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
            
            # Unpack the 5 returned values from _bounding_box
            width_px, height_px, crop_box, tx, ty = await run_in_threadpool(
                _bounding_box, tmp, f"expression_file_{session}.pdf", req.dpi
            )
            
            png = await run_in_threadpool(_render_png, tmp, session, req.dpi, width_px, height_px, tx, ty)
            png_b64 = base64.b64encode((Path(tmp, "build") / png).read_bytes()).decode()
            
            pdf_b64 = None
            if req.return_pdf:
                cropped = await run_in_threadpool(_crop_pdf, tmp, session, crop_box)
                pdf_b64 = base64.b64encode((Path(tmp, "build") / cropped).read_bytes()).decode()
                
            return {
                "png": png_b64,
                "pdf": pdf_b64,
                "width_px": width_px,
                "height_px": height_px,
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
