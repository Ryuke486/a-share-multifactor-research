"""Bounded BaoStock dividend transport with EOF-aware reconnects."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import time
from typing import Any, Protocol


_SUCCESS_CODE = "0"
_RECEIVE_ERROR_CODE = "10002007"


class _BaoStockResult(Protocol):
    error_code: str
    error_msg: str
    fields: list[str]

    def next(self) -> bool: ...

    def get_row_data(self) -> list[str]: ...


class _BaoStockApi(Protocol):
    def login(self) -> _BaoStockResult: ...

    def logout(self) -> _BaoStockResult: ...

    def query_dividend_data(
        self,
        *,
        code: str,
        year: str,
        yearType: str,
    ) -> _BaoStockResult: ...


@dataclass(frozen=True)
class BaoStockTransportPolicy:
    """Fixed timeout and retry policy for the sealed provider boundary."""

    attempts: int = 3
    timeout_seconds: float = 20.0
    retry_delay_seconds: float = 0.25

    def __post_init__(self) -> None:
        if (
            not isinstance(self.attempts, int)
            or isinstance(self.attempts, bool)
            or self.attempts <= 0
            or not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or self.timeout_seconds <= 0
            or not isinstance(self.retry_delay_seconds, (int, float))
            or isinstance(self.retry_delay_seconds, bool)
            or self.retry_delay_seconds < 0
        ):
            raise ValueError("BaoStock transport policy is invalid")


class _EofAwareSocket:
    """Turn a peer EOF into a bounded transport failure."""

    def __init__(self, raw_socket: Any) -> None:
        self._raw_socket = raw_socket
        self.failure: OSError | None = None

    def send(self, payload: bytes) -> int:
        try:
            return int(self._raw_socket.send(payload))
        except OSError as error:
            self.failure = error
            raise

    def recv(self, size: int) -> bytes:
        try:
            payload = self._raw_socket.recv(size)
        except OSError as error:
            self.failure = error
            raise
        if payload == b"":
            error = ConnectionResetError("BaoStock peer closed the connection")
            self.failure = error
            raise error
        return bytes(payload)

    def close(self) -> None:
        self._raw_socket.close()


class _TransientBaoStockError(RuntimeError):
    """A provider transport failure eligible for bounded retry."""


class BaoStockDividendClient:
    """Own one guarded BaoStock session and retry only transport failures."""

    def __init__(
        self,
        api: _BaoStockApi,
        *,
        policy: BaoStockTransportPolicy = BaoStockTransportPolicy(),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not callable(sleep):
            raise ValueError("BaoStock retry sleep is invalid")
        self._api = api
        self._policy = policy
        self._sleep = sleep
        self._socketutil: Any = None
        self._context: Any = None
        self._original_connect: Callable[[object], None] | None = None

    def __enter__(self) -> BaoStockDividendClient:
        import socket
        import baostock.common.contants as constants
        import baostock.common.context as context
        import baostock.util.socketutil as socketutil

        self._context = context
        self._socketutil = socketutil
        self._original_connect = socketutil.SocketUtil.connect

        def guarded_connect(instance: object) -> None:
            del instance
            raw_socket = socket.create_connection(
                (
                    constants.BAOSTOCK_SERVER_IP,
                    constants.BAOSTOCK_SERVER_PORT,
                ),
                timeout=self._policy.timeout_seconds,
            )
            try:
                raw_socket.settimeout(self._policy.timeout_seconds)
            except Exception:
                raw_socket.close()
                raise
            setattr(self._context, "default_socket", _EofAwareSocket(raw_socket))

        socketutil.SocketUtil.connect = guarded_connect
        try:
            self._login_with_retry()
        except BaseException:
            self._close_socket()
            self._restore_connect()
            raise
        return self

    def __exit__(
        self,
        _error_type: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: object,
    ) -> None:
        try:
            current = self._current_socket()
            if current is not None and current.failure is None:
                try:
                    self._api.logout()
                except Exception:
                    pass
        finally:
            self._close_socket()
            self._restore_connect()

    def query(
        self,
        *,
        code: str,
        year: int,
        year_type: str,
    ) -> tuple[list[str], list[list[str]]]:
        """Return one complete response or fail after bounded reconnects."""
        last_error: _TransientBaoStockError | None = None
        for attempt in range(self._policy.attempts):
            try:
                return self._query_once(
                    code=code,
                    year=year,
                    year_type=year_type,
                )
            except _TransientBaoStockError as error:
                last_error = error
                if attempt + 1 == self._policy.attempts:
                    break
                self._close_socket()
                self._sleep(self._policy.retry_delay_seconds * (2**attempt))
                self._login_with_retry()
        raise RuntimeError(
            f"BaoStock dividend transport failed after "
            f"{self._policy.attempts} attempts: {code} {year}"
        ) from last_error

    def _query_once(
        self,
        *,
        code: str,
        year: int,
        year_type: str,
    ) -> tuple[list[str], list[list[str]]]:
        try:
            result = self._api.query_dividend_data(
                code=code,
                year=str(year),
                yearType=year_type,
            )
            rows: list[list[str]] = []
            if result.error_code == _SUCCESS_CODE:
                while result.next():
                    rows.append(result.get_row_data())
        except OSError as error:
            raise _TransientBaoStockError(
                f"BaoStock dividend transport failed: {code} {year}"
            ) from error
        if self._transport_failed() or result.error_code == _RECEIVE_ERROR_CODE:
            raise _TransientBaoStockError(
                f"BaoStock dividend transport failed: {code} {year}"
            )
        if result.error_code != _SUCCESS_CODE:
            raise RuntimeError(
                f"BaoStock dividend query failed: {code} {year} {result.error_msg}"
            )
        return list(result.fields), rows

    def _login_with_retry(self) -> None:
        last_error: BaseException | None = None
        for attempt in range(self._policy.attempts):
            try:
                result = self._api.login()
            except OSError as error:
                last_error = error
            else:
                if (
                    result.error_code == _SUCCESS_CODE
                    and not self._transport_failed()
                ):
                    return
                if (
                    result.error_code != _RECEIVE_ERROR_CODE
                    and not self._transport_failed()
                ):
                    raise RuntimeError(
                        f"BaoStock login failed: {result.error_msg}"
                    )
                last_error = RuntimeError(result.error_msg)
            self._close_socket()
            if attempt + 1 < self._policy.attempts:
                self._sleep(self._policy.retry_delay_seconds * (2**attempt))
        raise RuntimeError(
            f"BaoStock login failed after {self._policy.attempts} attempts"
        ) from last_error

    def _current_socket(self) -> _EofAwareSocket | None:
        current = getattr(self._context, "default_socket", None)
        return current if isinstance(current, _EofAwareSocket) else None

    def _transport_failed(self) -> bool:
        current = self._current_socket()
        return current is None or current.failure is not None

    def _close_socket(self) -> None:
        current = self._current_socket()
        if current is not None:
            current.close()
        if self._context is not None:
            setattr(self._context, "default_socket", None)

    def _restore_connect(self) -> None:
        if self._socketutil is not None and self._original_connect is not None:
            self._socketutil.SocketUtil.connect = self._original_connect
        self._original_connect = None
