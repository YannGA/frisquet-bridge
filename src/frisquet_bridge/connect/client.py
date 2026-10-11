'High-level protocol client: request/response loop, reads, writes, pairing.'

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass

import structlog

from frisquet_bridge.connect.state import ProtocolState
from frisquet_bridge.frame import ADDR_BOILER, ADDR_CONNECT, MSG_ADDRESS_NOT_FOUND, MSG_ASSOCIATION, MSG_READ, Frame, association_network_id
from frisquet_bridge.transport.base import Transport, TransportError

log = structlog.get_logger(__name__)

# Frisquet Connect firmware version advertised during pairing.
CONNECT_VERSION = bytes((0x01, 0x21, 0x01, 0x02))

_DEFAULT_TIMEOUT = 5.0
_DEFAULT_RETRIES = 3


@dataclass(frozen=True, slots=True)
class Association:
    network_id: bytes
    association_id: int
    request_id: int


class AddressNotFound(TransportError):
    'The boiler rejected a matching operation because its address is unknown.'

    def __init__(
        self,
        *,
        request_msg_type: int,
        request_payload: bytes,
        response: Frame,
    ) -> None:
        self.request_msg_type = request_msg_type
        self.request_payload = request_payload
        self.response = response
        super().__init__(f"address not found for message 0x{request_msg_type:02x} payload {request_payload.hex()}")


class RequestTimeout(TransportError):
    'No matching RF response arrived before all request attempts expired.'


class FrisquetClient:
    'Drives request/response exchanges with the boiler over a Transport.'

    def __init__(
        self,
        transport: Transport,
        state: ProtocolState,
        *,
        self_addr: int = ADDR_CONNECT,
        boiler_addr: int = ADDR_BOILER,
        lock: asyncio.Lock | None = None, # serialise exchanges across clients sharing one modem
    ) -> None:
        self._transport = transport
        self._state = state
        self._self_addr = self_addr
        self._boiler_addr = boiler_addr
        self._last_response_rssi = None
        self._attempt_outcome_callback = ContextVar(f'rf_attempt_outcome_{id(self)}', default=None)
        # Serialises request/response exchanges so multiple clients sharing one
        # modem (connect + sonde + satellites) don't transmit over each other.
        self._lock = lock or asyncio.Lock()

    @property
    def state(self) -> ProtocolState: return self._state

    @property
    def last_response_rssi(self) -> int | None: return self._last_response_rssi

    def set_attempt_outcome_callback(self, callback: Callable[[str, int | None], None]) -> Token:
        'Install a task-local request-attempt observer and return its reset token.'
        return self._attempt_outcome_callback.set(callback)

    def reset_attempt_outcome_callback(self, token: Token) -> None: self._attempt_outcome_callback.reset(token)

    def _record_attempt_outcome(self, outcome: str, rssi: int | None = None) -> None:
        callback = self._attempt_outcome_callback.get()
        if callback is not None: callback(outcome, rssi)

    async def request(
        self,
        *,
        control: int,
        msg_type: int,
        payload: bytes,
        from_addr: int | None = None,
        to_addr: int | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        retries: int = _DEFAULT_RETRIES,
        burst: int = 1,
        burst_interval: float = 0.0,
    ) -> Frame:
        """Send a frame and await the boiler's matching response.

        The response is identified by swapped addresses and the same
        association id / request id (as in frisquet-connect).

        ``burst`` transmits the *identical* frame (same request id and payload)
        that many times, ``burst_interval`` seconds apart, before waiting for a
        reply. This mirrors the reference implementation on exchanges where the
        boiler ignores most individual frames: a reply to any frame of the burst
        matches, because the request id does not change inside a burst.
        """
        from_addr = self._self_addr if from_addr is None else from_addr
        to_addr = self._boiler_addr if to_addr is None else to_addr

        async with self._lock:
            return await self._request_locked(control=control, msg_type=msg_type, payload=payload, from_addr=from_addr, to_addr=to_addr,
                timeout=timeout, retries=retries, burst=burst, burst_interval=burst_interval)

    async def _request_locked(
        self,
        *,
        control: int,
        msg_type: int,
        payload: bytes,
        from_addr: int,
        to_addr: int,
        timeout: float,
        retries: int,
        burst: int = 1,
        burst_interval: float = 0.0,
    ) -> Frame:
        await self._transport.set_network_id(self._state.network_id)

        async with self._transport.subscribe() as queue:
            last_error = None
            for attempt in range(1, retries + 1):
                req_id = self._state.next_request_id()
                frame = Frame(to_addr=to_addr, from_addr=from_addr, association_id=self._state.association_id, request_id=req_id,
                    control=control, msg_type=msg_type, payload=payload)
                _drain(queue)
                try:
                    for index in range(max(1, burst)):
                        await self._transport.send(frame)
                        if index + 1 < burst and burst_interval > 0: await asyncio.sleep(burst_interval)
                except TransportError as exc:
                    self._record_attempt_outcome("transport_failure")
                    last_error = exc
                    log.warning('request_send_failed', attempt=attempt, retries=retries, to_addr=f'0x{to_addr:02x}',
                        from_addr=f'0x{from_addr:02x}', association_id=f'0x{self._state.association_id:02x}', request_id=f'0x{req_id:02x}',
                        msg_type=f'0x{msg_type:02x}', error=str(exc))
                    continue

                try:
                    response = await self._await_match(queue, from_addr=to_addr, to_addr=from_addr, association_id=self._state.association_id,
                        request_id=req_id, control=control, msg_type=msg_type, request_payload=payload, timeout=timeout)
                except AddressNotFound:
                    self._record_attempt_outcome("nack")
                    raise
                if response is not None:
                    self._record_attempt_outcome("success", self._last_response_rssi)
                    log.debug('request_succeeded', attempt=attempt, to_addr=f'0x{to_addr:02x}', from_addr=f'0x{from_addr:02x}',
                        association_id=f'0x{self._state.association_id:02x}', request_id=f'0x{req_id:02x}', msg_type=f'0x{msg_type:02x}')
                    return response
                last_error = RequestTimeout("no matching response")
                self._record_attempt_outcome("timeout")
                log.warning('request_no_response', attempt=attempt, retries=retries, to_addr=f'0x{to_addr:02x}',
                    from_addr=f'0x{from_addr:02x}', association_id=f'0x{self._state.association_id:02x}', request_id=f'0x{req_id:02x}',
                    msg_type=f'0x{msg_type:02x}')

            if isinstance(last_error, RequestTimeout): raise RequestTimeout(f"request failed after {retries} attempts: {last_error}")
            raise TransportError(f"request failed after {retries} attempts: {last_error}")

    async def send_oneway(
        self,
        *,
        control: int,
        msg_type: int,
        payload: bytes,
        from_addr: int | None = None,
        to_addr: int | None = None,
        repeats: int = 1,
        interval: float = 0.0,
        on_attempt_outcome: Callable[[str, int | None], None] | None = None, # per-send outcome observer
    ) -> None:
        """Transmit a frame without awaiting a response (fire-and-forget).

        Relayed zone writes (``control`` = a satellite/zone id) are forwarded by
        the boiler to the physical satellite and are *never* acknowledged
        directly to Connect, so there is no matching response to await. The
        official app simply (re)sends the frame a few times; we mirror that with
        ``repeats``/``interval`` for reliability over the lossy RF link.
        """
        from_addr = self._self_addr if from_addr is None else from_addr
        to_addr = self._boiler_addr if to_addr is None else to_addr

        for index in range(repeats):
            # Take the lock per send (not across the whole burst) so the gaps
            # between retransmits stay free for the sonde / polling reads that
            # share this modem.
            async with self._lock:
                try:
                    await self._transport.set_network_id(self._state.network_id)
                    req_id = self._state.next_request_id()
                    frame = Frame(to_addr=to_addr, from_addr=from_addr, association_id=self._state.association_id, request_id=req_id,
                        control=control, msg_type=msg_type, payload=payload)
                    await self._transport.send(frame)
                except TransportError:
                    if on_attempt_outcome is not None: on_attempt_outcome("transport_failure", None)
                    raise
                if on_attempt_outcome is not None: on_attempt_outcome("success", None)
            if interval > 0 and index + 1 < repeats: await asyncio.sleep(interval)

    async def read_memory(
        self,
        address: int,
        size: int,
        **kwargs: object # extra FrisquetClient.request options
    ) -> bytes:
        'READ ``size`` bytes at ``address``; returns the response payload.'
        payload = address.to_bytes(2, "big") + size.to_bytes(2, "big")
        response = await self.request(control=0x01, msg_type=MSG_READ, payload=payload, **kwargs)  # type: ignore[arg-type]
        return response.payload

    async def _await_match(
        self,
        queue: asyncio.Queue,
        *,
        from_addr: int,
        to_addr: int,
        association_id: int,
        request_id: int,
        control: int,
        msg_type: int,
        request_payload: bytes,
        timeout: float,
    ) -> Frame | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0: return None
            try: received = await asyncio.wait_for(queue.get(), remaining)
            except TimeoutError: return None
            frame = received.frame
            if not frame.matches(from_addr=from_addr, to_addr=to_addr, association_id=association_id, request_id=request_id) or not frame.is_ack: continue
            if frame.msg_type == MSG_ADDRESS_NOT_FOUND:
                if frame.control != ((control & 0x7F) | 0x80): continue
                raise AddressNotFound(
                    request_msg_type=msg_type,
                    request_payload=request_payload,
                    response=frame)
            if frame.msg_type == msg_type:
                self._last_response_rssi = received.rssi
                return frame

    async def recover_network_id(self, *, timeout: float = 30.0) -> bytes:
        """Sniff the boiler's association broadcast and return its network id.

        Unlike :meth:`pair`, this never transmits a reply, so the boiler's
        association stays open. Trigger an association (or "replace satellite")
        on the boiler, then cancel it once the id has been captured.
        """
        await self._transport.set_network_id(b"\xff\xff\xff\xff")
        log.info("network_id_recovery_waiting", timeout=timeout)
        async with self._transport.subscribe() as queue:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0: raise TransportError("network id recovery timed out (no broadcast)")
                try: received = await asyncio.wait_for(queue.get(), remaining)
                except TimeoutError: raise TransportError("network id recovery timed out (no broadcast)") from None
                frame = received.frame
                network_id = association_network_id(frame)
                if network_id is None: continue
                log.info("network_id_recovered", network_id=network_id.hex())
                return network_id

    async def pair(
        self,
        from_addr: int,
        *, listen_window: float = 5.0, # seconds of silence that end the listen
    ) -> Association:
        """Listen for the boiler's association broadcast and reply.

        Set the boiler into association mode first. Replies to every broadcast
        until the boiler stops sending (one quiet ``listen_window``).
        """
        await self._transport.set_network_id(b"\xff\xff\xff\xff")
        log.info("pairing_waiting", from_addr=f"0x{from_addr:02x}", listen_window=listen_window)

        result = None
        async with self._transport.subscribe() as queue:
            while True:
                try: received = await asyncio.wait_for(queue.get(), listen_window)
                except TimeoutError:
                    if result is not None: return result
                    raise TransportError("pairing timed out (no broadcast)") from None

                frame = received.frame
                network_id = association_network_id(frame)
                if network_id is None: continue

                result = Association(network_id=network_id, association_id=frame.association_id, request_id=frame.request_id)
                log.info('pairing_broadcast_received', from_addr=f'0x{from_addr:02x}', network_id=network_id.hex(),
                    association_id=f'0x{frame.association_id:02x}', request_id=f'0x{frame.request_id:02x}')
                reply = Frame(to_addr=ADDR_BOILER, from_addr=from_addr, association_id=frame.association_id, request_id=frame.request_id,
                    control=frame.control + 128, msg_type=MSG_ASSOCIATION, payload=CONNECT_VERSION)
                await self._transport.send(reply)


def _drain(queue: asyncio.Queue) -> None:
    while not queue.empty(): queue.get_nowait()
