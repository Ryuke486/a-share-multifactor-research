from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from enum import Enum


class OrderStatus(str, Enum):
    CREATED = "created"
    PENDING = "pending"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REJECTED = "rejected"


@dataclass(frozen=True)
class Order:
    order_id: str
    signal_date: date
    symbol: str
    side: str
    quantity: int
    remaining_quantity: int
    status: OrderStatus = OrderStatus.CREATED


_ALLOWED = {
    OrderStatus.CREATED: {OrderStatus.PENDING, OrderStatus.REJECTED, OrderStatus.CANCELLED},
    OrderStatus.PENDING: {
        OrderStatus.PENDING,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
    },
    OrderStatus.PARTIALLY_FILLED: {
        OrderStatus.PENDING,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
    },
}


def transition(
    order: Order, status: OrderStatus, *, remaining_quantity: int | None = None
) -> Order:
    if status not in _ALLOWED.get(order.status, set()):
        raise ValueError(f"illegal order transition: {order.status} -> {status}")
    remaining = (
        0
        if status is OrderStatus.FILLED and remaining_quantity is None
        else order.remaining_quantity
        if remaining_quantity is None
        else remaining_quantity
    )
    if remaining < 0 or (status is OrderStatus.FILLED and remaining != 0):
        raise ValueError("invalid remaining quantity")
    return replace(order, status=status, remaining_quantity=remaining)
