"""PDF decryption via pikepdf.

Docling never sees an encrypted file -- it always receives the decrypted
temp file this module produces. The temp file is always removed on exit,
success or failure. No exception raised here ever embeds the password.
"""
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pikepdf


class DecryptError(Exception):
    """Raised when a PDF can't be decrypted for a reason other than a wrong password."""


class WrongPasswordError(DecryptError):
    """Raised when the supplied password doesn't open the PDF."""


def is_encrypted(path: Path) -> bool:
    try:
        with pikepdf.open(path):
            return False
    except pikepdf.PasswordError:
        return True


@contextmanager
def decrypted_copy(path: Path, password: str | None):
    """Yield the path to a decrypted temp copy of `path`. Always deletes the
    temp file on exit, whether the caller succeeds or raises."""
    fd, tmp_name = tempfile.mkstemp(suffix=".pdf")
    import os
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        try:
            with pikepdf.open(path, password=password or "") as pdf:
                pdf.save(tmp_path)
        except pikepdf.PasswordError:
            raise WrongPasswordError("That password didn't open the statement.") from None
        yield tmp_path
    finally:
        tmp_path.unlink(missing_ok=True)
