"""家庭成员隐私保护。

事件中只允许出现化名 member_ref；真实姓名、电话等敏感信息写入独立密钥表，
API 投影与客服时间线重放一律经过本模块脱敏，不随份额数据流出。
"""

from __future__ import annotations

from typing import Any, Mapping

SENSITIVE_MEMBER_FIELDS = frozenset(
    {
        "member_name",
        "member_phone",
        "member_id_card",
        "member_address",
        "member_real_name",
        "family_member_name",
    }
)


def redact_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """删除载荷中任何混入的家庭成员敏感字段（保留化名 member_ref）。"""
    return {key: value for key, value in payload.items() if key not in SENSITIVE_MEMBER_FIELDS}


def mask_name(name: str) -> str:
    if not name:
        return "**"
    if len(name) == 1:
        return name + "*"
    return name[0] + "*" * (len(name) - 1)


def mask_phone(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 7:
        return "***"
    return digits[:3] + "****" + digits[-4:]


def public_member_view(secret: Mapping[str, Any]) -> dict[str, str]:
    """敏感库 -> 对外投影：只给化名与打码展示名。"""
    return {
        "member_ref": str(secret.get("member_ref", "")),
        "display_name": mask_name(str(secret.get("name", ""))),
        "contact": mask_phone(str(secret.get("phone", ""))) if secret.get("phone") else "",
    }
