"""불변 객체 ID = 접두어 + uuid4 hex (부록 A.10). 내용이 같은지는 hash 컬럼으로 본다."""

import uuid


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"
