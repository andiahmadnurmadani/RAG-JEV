"""Trusted tenant context — the only source of ``organization_id`` (PRD 6, 19, 21).

Lives in ``core`` (not in the API layer) so the retrieval pipeline can depend on it
without importing FastAPI: a circular import here would mean the tenant context is
optional, which is exactly what we do not want.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.core.errors import AppError


@dataclass
class TrustedContext:
    user_id: str
    organization_id: str
    application_id: str
    permissions: List[str] = field(default_factory=list)
    source: str = "api_key"
    # Diisi hanya bila kredensialnya kunci dari registry (bukan API_KEYS_JSON / token KMS).
    # Dipakai untuk menolak permintaan yang mencabut kunci yang sedang dipakainya sendiri.
    key_id: Optional[str] = None
    # Diisi hanya bila kredensialnya sesi konsol: dipakai untuk "keluar" dan untuk menampilkan
    # sesi mana yang sedang aktif, tanpa perlu meminta tokennya lagi.
    session_id: Optional[str] = None
    session_expires_at: Optional[str] = None
    # Knowledge base yang boleh disentuh kredensial ini. Kosong = semua KB organisasinya (perilaku
    # lama). Diisi = kunci proyek: hanya KB miliknya, walau organisasinya dipakai bersama.
    knowledge_base_ids: List[str] = field(default_factory=list)

    def allows_knowledge_base(self, knowledge_base_id: Optional[str]) -> bool:
        if not self.knowledge_base_ids:
            return True
        return bool(knowledge_base_id) and str(knowledge_base_id) in self.knowledge_base_ids

    def require_knowledge_base(self, knowledge_base_id: Optional[str]) -> None:
        if not self.allows_knowledge_base(knowledge_base_id):
            raise AppError(
                "AUTH_FORBIDDEN",
                "Kunci ini tidak diizinkan mengakses knowledge base tersebut",
                details={"knowledge_base_id": knowledge_base_id, "allowed": list(self.knowledge_base_ids)},
            )

    def has_permission(self, permission: str) -> bool:
        if "*" in self.permissions:
            return True
        if permission in self.permissions:
            return True
        if permission == "read" and "write" in self.permissions:
            return True
        return False

    def require(self, permission: str) -> None:
        if not self.has_permission(permission):
            raise AppError(
                "AUTH_FORBIDDEN",
                f"Application is not permitted to perform '{permission}'",
                details={"permission": permission, "application_id": self.application_id},
            )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "organization_id": self.organization_id,
            "application_id": self.application_id,
            "permissions": list(self.permissions),
            "knowledge_base_ids": list(self.knowledge_base_ids),
        }
