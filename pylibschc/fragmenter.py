# Copyright (C) 2023 Freie Universität Berlin
#
# SPDX-License-Identifier: GPL-3.0-only

import abc
import enum
import threading
import typing

import pylibschc.device

__author__ = "Martine S. Lenders"
__copyright__ = "Copyright 2023 Freie Universität Berlin"
__license__ = "GPLv3"
__email__ = "m.lenders@fu-berlin.de"


# pylint: disable=import-error
from .libschc import (
    BitArray,
    FragmentationConnection,
    FragmentationMode,
    FragmentationResult,
)


class ReassemblyStatus(enum.Enum):
    ONGOING = 0
    COMPLETED = 1
    STAY_ALIVE = 2
    ACK_HANDLED = 256


class BaseFragmenterReassembler(abc.ABC):
    # pylint: disable=too-many-instance-attributes,too-few-public-methods
    conn_cls = FragmentationConnection

    def __init__(  # pylint: disable=too-many-arguments
        self,
        device: pylibschc.device.Device,
        mtu: int,
        duty_cycle_ms: int,
        mode: FragmentationMode,
        end_rx: typing.Callable[[object], None] = None,
        end_tx: typing.Callable[[object], None] = None,
        post_timer_task: typing.Callable[
            [object, typing.Callable[[object], None], float, object], None
        ] = None,
        remove_timer_entry: typing.Callable[[object], None] = None,
    ):
        self.device = device
        self.mtu = mtu
        self.duty_cycle_ms = duty_cycle_ms
        self.mode = mode
        self._post_timer_task = post_timer_task
        self._end_rx = end_rx
        self._end_tx = end_tx
        self._remove_timer_entry = remove_timer_entry

    def input(self, data: typing.Union[bytes, BitArray]) -> ReassemblyStatus:
        pass  # pragma: no cover


class Fragmenter(BaseFragmenterReassembler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._tx_conn = None
        self._tx_conn_lock = threading.Lock()

    def _tx_conn_release(self):
        del self._tx_conn
        self._tx_conn = None
        self._tx_conn_lock.release()

    def _end_fragmentation_tx(self, conn: object):
        if self._end_tx:  # pragma: no cover
            self._end_tx(conn)
        self._tx_conn_release()

    def input(self, data: typing.Union[bytes, BitArray]) -> ReassemblyStatus:
        if isinstance(data, BitArray):
            bit_array = data
        else:
            bit_array = BitArray(data)
        self._tx_conn.bit_arr = bit_array
        new_conn = self._tx_conn.input(data)
        if new_conn is None:
            return ReassemblyStatus.ACK_HANDLED  # pragma: no cover
        if new_conn != self._tx_conn:  # pragma: no cover
            # is equal when acknowledgment was received
            if not new_conn.fragmented:
                new_conn.end_rx(new_conn)
                new_conn.reset()
            assert RuntimeError(
                b"Unexpected state, input {data.hex()} should be an ACK"
            )
        return ReassemblyStatus.ACK_HANDLED  # pragma: no cover

    def output(self, data: typing.Union[bytes, BitArray]) -> FragmentationResult:
        if isinstance(data, BitArray):
            bit_array = data
        else:
            bit_array = BitArray(data)
        self._tx_conn_lock.acquire()  # pylint: disable=consider-using-with
        assert self._tx_conn is None
        self._tx_conn = self.conn_cls(
            post_timer_task=self._post_timer_task,
            end_tx=self._end_tx,
            end_rx=self._end_fragmentation_tx,
            remove_timer_entry=self._remove_timer_entry,
        )
        self._tx_conn.init_tx(
            self.device.device_id,
            bit_array,
            self.mtu,
            self.duty_cycle_ms,
            self.mode.value,
        )
        try:
            res = self._tx_conn.fragment()
            if res == FragmentationResult.NO_FRAGMENTATION:
                self._end_fragmentation_tx(self._tx_conn)
            return res
        except Exception:  # pragma: no cover
            self._tx_conn_release()
            raise

    @classmethod
    def register_send(
        cls, device: pylibschc.device.Device, send: typing.Callable[[bytes], int]
    ):
        return cls.conn_cls.register_send(device.device_id, send)

    @classmethod
    def unregister_send(cls, device: pylibschc.device.Device):
        return cls.conn_cls.unregister_send(device.device_id)


class Reassembler(BaseFragmenterReassembler):  # pylint: disable=too-few-public-methods
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._rx_conn = None
        self._rx_conn_lock = threading.Lock()

    def _end_reassembly_rx(self, conn: object):
        if self._end_rx:  # pragma: no cover
            self._end_rx(conn)
        del self._rx_conn
        self._rx_conn = None

    def input(self, data: typing.Union[bytes, BitArray]) -> ReassemblyStatus:
        if isinstance(data, BitArray):
            bit_array = data
        else:
            bit_array = BitArray(data)
        with self._rx_conn_lock:
            if self._rx_conn is None:
                self._rx_conn = self.conn_cls(
                    post_timer_task=self._post_timer_task,
                    end_rx=self._end_reassembly_rx,
                    remove_timer_entry=self._remove_timer_entry,
                )
                self._rx_conn.init_rx(
                    self.device.device_id, bit_array, self.duty_cycle_ms
                )
            else:
                self._rx_conn.bit_arr = bit_array
            new_conn = self._rx_conn.input(data)
            if new_conn is None:
                return ReassemblyStatus.COMPLETED  # pragma: no cover
            if new_conn == self._rx_conn:  # is equal when acknowledgment was received
                assert RuntimeError(  # pragma: no cover
                    b"Unexpected state, input {data.hex()} should not be an ACK"
                )
            if not new_conn.fragmented:
                new_conn.end_rx(new_conn)
                new_conn.reset()
                return ReassemblyStatus.COMPLETED
            return ReassemblyStatus(new_conn.reassemble())