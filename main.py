"""LaTeX compile microservice for the InLaTeXbot Lovable port."""
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
    llx = bounds[0] - hpad
    lly = bounds[1] - vpad
    urx = bounds[2] + hpad
    ury = bounds[3] + vpad

    width_pt = urx - llx
    height_pt = ury - lly

    # Check if it's a full document page (e.g., A4 is ~595x842 points).
    # If the content is larger than 500 points, bypass the aggressive snippet cropping.
    is_full_page = height_pt > 500 or width_pt > 500

    if not is_full_page:
        # Aspect-ratio correction (Ported from InLaTeXbot for small equations)
        # We safely adjust the bounding box coordinates directly instead of messing with pixel translations.
        if width_pt > 3 * height_pt:
            new_height = width_pt / 3
            diff = new_height - height_pt
            lly -= diff / 2
            ury += diff / 2
        elif height_pt > width_pt:
            new_width = height_pt
            diff = new_width - width_pt
            llx -= diff / 2
            urx += diff / 2

    size_factor = dpi / 72
    width_px = int(round((urx - llx) * size_factor))
    height_px = int(round((ury - lly) * size_factor))
    
    # CropBox expects [lower_left_x, lower_left_y, upper_right_x, upper_right_y]
    crop_box = [int(round(llx)), int(round(lly)), int(round(urx)), int(round(ury))]

    return width_px, height_px, crop_box

def _crop_pdf(workdir: str, session: str, crop_box) -> str:
    """Creates a PDF with a specific CropBox applied to it."""
    cropped = f"expression_file_cropped_{session}.pdf"
    cmd = (
        "gs -o build/" + cropped + " -sDEVICE=pdfwrite "
        f'-c "[/CropBox [{crop_box[0]} {crop_box[1]} {crop_box[2]} {crop_box[3]}]" '
        '-c " /PAGES pdfmark" -f build/' + f"expression_file_{session}.pdf"
    )
    _run(["/bin/sh", "-c", cmd], workdir, GS_TIMEOUT)
    return cropped

def _render_png(workdir: str, session: str, dpi: int, cropped_pdf_name: str) -> str:
    """Renders the cropped PDF directly to PNG using Ghostscript's built-in CropBox logic."""
    png = f"expression_{session}.png"
    
    # -dUseCropBox tells Ghostscript to strictly respect the CropBox we set in the PDF.
    # This eliminates all the manual -g (canvas) and translate math which caused the clipping.
    cmd = (
        "gs -o build/" + png + f" -r{dpi} -sDEVICE=pngalpha -dUseCropBox -dLastPage=1 -f build/" + cropped_pdf_name
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
            # 1. Compile LaTeX to PDF
            await run_in_threadpool(_tectonic, tmp, f"expression_file_{session}.tex")
            
            # 2. Calculate Bounding Box and Crop coordinates
            width_px, height_px, crop_box = await run_in_threadpool(
                _bounding_box, tmp, f"expression_file_{session}.pdf", req.dpi
            )
            
            # 3. Physically crop the PDF (we do this regardless of return_pdf because the PNG needs it)
            cropped_pdf = await run_in_threadpool(_crop_pdf, tmp, session, crop_box)
            
            # 4. Render the cropped PDF to PNG
            png = await run_in_threadpool(_render_png, tmp, session, req.dpi, cropped_pdf)
            png_b64 = base64.b64encode((Path(tmp, "build") / png).read_bytes()).decode()
            
            # 5. Return PDF if requested
            pdf_b64 = None
            if req.return_pdf:
                pdf_b64 = base64.b64encode((Path(tmp, "build") / cropped_pdf).read_bytes()).decode()
                
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
