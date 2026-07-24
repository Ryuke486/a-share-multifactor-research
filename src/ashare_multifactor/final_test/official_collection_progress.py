"""Narrow observer seam for official identity and announcement collection."""

from __future__ import annotations

from typing import Protocol

from ashare_multifactor.final_test.official_query_coverage import OfficialQueryScope


class OfficialCollectionProgressObserver(Protocol):
    """Receive progress without controlling collection semantics."""

    def identity_request(
        self,
        *,
        symbol: str,
        completed: int,
        total: int,
    ) -> None: ...

    def identity_completed(
        self,
        *,
        symbol: str,
        completed: int,
        total: int,
    ) -> None: ...

    def query_request(
        self,
        *,
        scope: OfficialQueryScope,
        page: int,
    ) -> None: ...

    def query_page_verified(
        self,
        *,
        scope: OfficialQueryScope,
        page: int,
        total_pages: int,
    ) -> None: ...

    def split_recorded(self, *, scope: OfficialQueryScope) -> None: ...

    def security_completed(
        self,
        *,
        scope: OfficialQueryScope,
        completed: int,
        total: int,
    ) -> None: ...
