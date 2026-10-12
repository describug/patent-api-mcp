"""공보 원본 PDF(pdf_download): 저장 폴더 검사·파일 이름·쪽 합치기·안전한 저장. 네트워크를 모르는 계층이다."""

from __future__ import annotations

import io
import os
import re
import secrets
import unicodedata
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from . import errors as E
from .errors import PatentApiError

PDF_MAGIC = b"%PDF"
PARTS = ("full", "drawings")
# 종류코드 첫 글자가 이것이면 등록(특허·실용) 공보로 본다. B·C(등록), E(미국 재발행), Y(등록실용).
REGISTRATION_KIND_LETTERS = frozenset("BCEY")
_LABEL_RE = re.compile(r"[A-Za-z0-9-]{1,20}")


def nfc(text: str) -> str:
    """맥 한글 경로는 NFD로 오기도 한다. 비교·저장은 NFC로 맞춘다."""
    return unicodedata.normalize("NFC", text)


def kind_of(kind_code: str | None) -> str:
    """종류코드 → 'publication'(공개) 또는 'registration'(등록)."""
    return "registration" if (kind_code or "A")[:1].upper() in REGISTRATION_KIND_LETTERS else "publication"


def clean_label(label: str | None) -> str | None:
    if label is None or not label.strip():
        return None
    label = nfc(label.strip())
    if not _LABEL_RE.fullmatch(label):
        raise PatentApiError(
            E.INVALID_INPUT,
            f"label은 영문·숫자·하이픈 20자 이내로 넣어 주세요(예: D1). 파일 이름은 ASCII로만 만듭니다(입력: {label}).",
        )
    return label


def conventional_number(number: str) -> str:
    """OPS(docdb) 표기를 흔히 쓰는 공보번호 표기로. 미국 공개번호만 다르다: US2022181889A1 → US20220181889A1."""
    m = re.fullmatch(r"US((?:19|20)\d{2})(\d{6})(A\d?)", number)
    return f"US{m.group(1)}0{m.group(2)}{m.group(3)}" if m else number


def file_name(label: str | None, number: str, part: str) -> str:
    """'{label}_{국가}{번호}{종류코드}[_drawings].pdf'. 예: D1_WO2022119250A1.pdf, D2_US20220181889A1.pdf"""
    safe = re.sub(r"[^0-9A-Za-z]", "", conventional_number(number))
    if not safe:
        raise PatentApiError(E.INVALID_INPUT, "파일 이름을 만들 번호가 없습니다.")
    name = f"{label}_{safe}" if label else safe
    if part == "drawings":
        name += "_drawings"
    return name + ".pdf"


def _within(path: Path, root: Path) -> bool:
    p, r = nfc(str(path)), nfc(str(root))
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def allowed_root(allowed: Path) -> Path:
    """허용 폴더의 실제 경로(심볼릭 링크를 푼 것). 없으면 오류."""
    root = Path(nfc(str(allowed))).expanduser()
    if not root.is_dir():
        raise PatentApiError(
            E.FILE_ERROR,
            f"허용 폴더가 없습니다: {root}. 확장 설정의 'PDF 허용 폴더'(또는 .env의 PATENT_API_PDF_ALLOWED_DIR)를 있는 폴더로 바꿔 주세요.",
        )
    return Path(nfc(os.path.realpath(root)))


def resolve_save_dir(save_dir: str | None, allowed: Path) -> Path:
    """save_dir이 허용 폴더 아래인지 확인하고 실제 경로를 돌려준다(폴더는 아직 만들지 않는다).

    비우면 허용 폴더 자체. 절대경로만 받고, '..'가 들어 있거나 심볼릭 링크로 허용 폴더 밖을 가리키면 오류.
    """
    root = allowed_root(allowed)
    if save_dir is None or not save_dir.strip():
        return root
    raw = nfc(save_dir.strip())
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise PatentApiError(E.INVALID_INPUT, f"save_dir은 절대경로로 넣어 주세요(입력: {raw}). 허용 폴더: {root}")
    if ".." in path.parts:
        raise PatentApiError(E.INVALID_INPUT, f"save_dir에 '..'를 쓸 수 없습니다(입력: {raw}).")
    real = Path(nfc(os.path.realpath(path)))
    if not _within(real, root):
        raise PatentApiError(
            E.INVALID_INPUT,
            f"save_dir이 허용 폴더 밖입니다(입력: {raw}). 허용 폴더({root}) 아래에만 저장합니다. "
            "다른 곳에 저장하려면 확장 설정의 'PDF 허용 폴더'(또는 PATENT_API_PDF_ALLOWED_DIR)를 바꿔 주세요.",
        )
    return real


def is_pdf(data: bytes) -> bool:
    return data[:1024].lstrip().startswith(PDF_MAGIC)


def describe(data: bytes) -> dict:
    """{'pages', 'bytes', 'hasTextLayer'}. 텍스트층은 앞쪽 몇 쪽에서 실제로 글자를 뽑아 본다."""
    out: dict = {"bytes": len(data)}
    try:
        reader = PdfReader(io.BytesIO(data))
        out["pages"] = len(reader.pages)
        chars = 0
        for page in reader.pages[:5]:
            chars += len((page.extract_text() or "").strip())
            if chars >= 50:
                break
        out["hasTextLayer"] = chars >= 50
    except Exception:
        out["pages"] = None
        out["hasTextLayer"] = None
    return out


def merge(pages: list[bytes]) -> bytes:
    """쪽별 PDF를 순서대로 하나로 합친다."""
    writer = PdfWriter()
    try:
        for data in pages:
            for page in PdfReader(io.BytesIO(data)).pages:
                writer.add_page(page)
        out = io.BytesIO()
        writer.write(out)
    except Exception as e:
        raise PatentApiError(E.PARSE_ERROR, f"받은 쪽 PDF를 합치지 못했습니다({type(e).__name__}).") from None
    return out.getvalue()


def save_new(directory: Path, name: str, data: bytes, root: Path) -> Path:
    """새 파일로만 저장한다(같은 이름이 있으면 FileExistsError). 임시 파일에 다 쓴 뒤 이름을 붙여 반쪽 파일을 남기지 않는다."""
    final = directory / name
    tmp = directory / f".{name}.{secrets.token_hex(4)}.part"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # 만드는 사이에 링크로 바뀌지 않았는지 한 번 더 본다.
        if not _within(Path(nfc(os.path.realpath(directory))), root):
            raise PatentApiError(E.INVALID_INPUT, "저장 폴더가 허용 폴더 밖을 가리킵니다.")
        tmp.write_bytes(data)
        try:
            os.link(tmp, final)  # 이미 있으면 FileExistsError — 덮어쓰지 않는다
        except FileExistsError:
            raise
        except OSError:
            # 하드 링크를 못 쓰는 파일 시스템
            if final.exists():
                raise FileExistsError(str(final)) from None
            os.replace(tmp, final)
    except (PatentApiError, FileExistsError):
        raise
    except OSError as e:
        raise PatentApiError(
            E.FILE_ERROR, f"PDF를 저장하지 못했습니다({type(e).__name__}: {directory}). 폴더 권한을 확인해 주세요."
        ) from None
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return final
