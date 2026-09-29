"""공용 직렬화 (부록 A.1). pack_hash와 candidate_hash(§5.2)가 같은 함수를 쓴다."""

import hashlib
import json
from typing import Any


def canonical_json(obj: Any) -> bytes:
    """key 사전순, 공백 없음, UTF-8. NaN·datetime 등 JSON 밖의 값은 오류."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_hash(obj: Any) -> str:
    return sha256_hex(canonical_json(obj))
