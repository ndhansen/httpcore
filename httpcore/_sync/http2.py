from http import HTTPStatus
from ssl import SSLContext
from typing import (
    Iterator,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
    Union,
)

import h2.connection
import h2.events
from h2.config import H2Configuration
from h2.exceptions import NoAvailableStreamIDError
from h2.settings import SettingCodes, Settings

from .._backends.auto import SyncLock, SyncSemaphore, SyncSocketStream, SyncBackend
from .._exceptions import PoolTimeout, ProtocolError
from .base import (
    SyncByteStream,
    SyncHTTPTransport,
    ConnectionState,
    NewConnectionRequired,
)


def get_reason_phrase(status_code: int) -> bytes:
    try:
        return HTTPStatus(status_code).phrase.encode("ascii")
    except ValueError:
        return b""


class SyncHTTP2Connection(SyncHTTPTransport):
    READ_NUM_BYTES = 4096
    CONFIG = H2Configuration(validate_inbound_headers=False)

    def __init__(
        self,
        socket: SyncSocketStream,
        backend: SyncBackend,
        ssl_context: SSLContext = None,
    ):
        self.socket = socket
        self.ssl_context = SSLContext() if ssl_context is None else ssl_context

        self.backend = backend
        self.h2_state = h2.connection.H2Connection(config=self.CONFIG)

        self.sent_connection_init = False
        self.streams = {}  # type: Dict[int, SyncHTTP2Stream]
        self.events = {}  # type: Dict[int, List[h2.events.Event]]

        self.state = ConnectionState.ACTIVE

    @property
    def init_lock(self) -> SyncLock:
        # We do this lazily, to make sure backend autodetection always
        # runs within an async context.
        if not hasattr(self, "_initialization_lock"):
            self._initialization_lock = self.backend.create_lock()
        return self._initialization_lock

    @property
    def read_lock(self) -> SyncLock:
        # We do this lazily, to make sure backend autodetection always
        # runs within an async context.
        if not hasattr(self, "_read_lock"):
            self._read_lock = self.backend.create_lock()
        return self._read_lock

    @property
    def streams_semaphore(self) -> SyncSemaphore:
        # We do this lazily, to make sure backend autodetection always
        # runs within an async context.
        if not hasattr(self, "_streams_semaphore"):
            semaphore_count = self.h2_state.remote_settings.max_concurrent_streams
            self._streams_semaphore = self.backend.create_semaphore(
                semaphore_count, PoolTimeout
            )
        return self._streams_semaphore

    def start_tls(
        self, hostname: bytes, timeout: Dict[str, Optional[float]] = None
    ):
        pass

    def mark_as_ready(self):
        if self.state == ConnectionState.IDLE:
            self.state = ConnectionState.READY

    def request(
        self,
        method: bytes,
        url: Tuple[bytes, bytes, int, bytes],
        headers: List[Tuple[bytes, bytes]] = None,
        stream: SyncByteStream = None,
        timeout: Dict[str, Optional[float]] = None,
    ) -> Tuple[bytes, int, bytes, List[Tuple[bytes, bytes]], SyncByteStream]:
        timeout = {} if timeout is None else timeout

        self.streams_semaphore.acquire()
        try:
            with self.init_lock:
                if not self.sent_connection_init:
                    # The very first stream is responsible for initiating the connection.
                    self.state = ConnectionState.ACTIVE
                    self.send_connection_init(timeout)
                    self.sent_connection_init = True

                try:
                    stream_id = self.h2_state.get_next_available_stream_id()
                except NoAvailableStreamIDError:
                    self.state = ConnectionState.FULL
                    raise NewConnectionRequired()
                else:
                    self.state = ConnectionState.ACTIVE
        finally:
            self.streams_semaphore.release()

        h2_stream = SyncHTTP2Stream(stream_id=stream_id, connection=self)
        self.streams[stream_id] = h2_stream
        self.events[stream_id] = []
        return h2_stream.request(method, url, headers, stream, timeout)

    def send_connection_init(self, timeout: Dict[str, Optional[float]]) -> None:
        """
        The HTTP/2 connection requires some initial setup before we can start
        using individual request/response streams on it.
        """
        # Need to set these manually here instead of manipulating via
        # __setitem__() otherwise the H2Connection will emit SettingsUpdate
        # frames in addition to sending the undesired defaults.
        self.h2_state.local_settings = Settings(
            client=True,
            initial_values={
                # Disable PUSH_PROMISE frames from the server since we don't do anything
                # with them for now.  Maybe when we support caching?
                SettingCodes.ENABLE_PUSH: 0,
                # These two are taken from h2 for safe defaults
                SettingCodes.MAX_CONCURRENT_STREAMS: 100,
                SettingCodes.MAX_HEADER_LIST_SIZE: 65536,
            },
        )

        # Some websites (*cough* Yahoo *cough*) balk at this setting being
        # present in the initial handshake since it's not defined in the original
        # RFC despite the RFC mandating ignoring settings you don't know about.
        del self.h2_state.local_settings[
            h2.settings.SettingCodes.ENABLE_CONNECT_PROTOCOL
        ]

        self.h2_state.initiate_connection()
        self.h2_state.increment_flow_control_window(2 ** 24)
        data_to_send = self.h2_state.data_to_send()
        self.socket.write(data_to_send, timeout)

    @property
    def is_closed(self) -> bool:
        return False

    def is_connection_dropped(self) -> bool:
        return self.socket.is_connection_dropped()

    def close(self) -> None:
        if self.state != ConnectionState.CLOSED:
            self.state = ConnectionState.CLOSED

            self.socket.close()

    def wait_for_outgoing_flow(
        self, stream_id: int, timeout: Dict[str, Optional[float]]
    ) -> int:
        """
        Returns the maximum allowable outgoing flow for a given stream.
        If the allowable flow is zero, then waits on the network until
        WindowUpdated frames have increased the flow rate.
        https://tools.ietf.org/html/rfc7540#section-6.9
        """
        local_flow = self.h2_state.local_flow_control_window(stream_id)
        connection_flow = self.h2_state.max_outbound_frame_size
        flow = min(local_flow, connection_flow)
        while flow == 0:
            self.receive_events(timeout)
            local_flow = self.h2_state.local_flow_control_window(stream_id)
            connection_flow = self.h2_state.max_outbound_frame_size
            flow = min(local_flow, connection_flow)
        return flow

    def wait_for_event(
        self, stream_id: int, timeout: Dict[str, Optional[float]]
    ) -> h2.events.Event:
        """
        Returns the next event for a given stream.
        If no events are available yet, then waits on the network until
        an event is available.
        """
        with self.read_lock:
            while not self.events[stream_id]:
                self.receive_events(timeout)
        return self.events[stream_id].pop(0)

    def receive_events(self, timeout: Dict[str, Optional[float]]) -> None:
        """
        Read some data from the network, and update the H2 state.
        """
        data = self.socket.read(self.READ_NUM_BYTES, timeout)
        events = self.h2_state.receive_data(data)
        for event in events:
            event_stream_id = getattr(event, "stream_id", 0)

            if hasattr(event, "error_code"):
                raise ProtocolError(event)

            if event_stream_id in self.events:
                self.events[event_stream_id].append(event)

        data_to_send = self.h2_state.data_to_send()
        self.socket.write(data_to_send, timeout)

    def send_headers(
        self,
        stream_id: int,
        headers: List[Tuple[bytes, bytes]],
        end_stream: bool,
        timeout: Dict[str, Optional[float]],
    ) -> None:
        self.h2_state.send_headers(stream_id, headers, end_stream=end_stream)
        self.h2_state.increment_flow_control_window(2 ** 24, stream_id=stream_id)
        data_to_send = self.h2_state.data_to_send()
        self.socket.write(data_to_send, timeout)

    def send_data(
        self, stream_id: int, chunk: bytes, timeout: Dict[str, Optional[float]]
    ) -> None:
        self.h2_state.send_data(stream_id, chunk)
        data_to_send = self.h2_state.data_to_send()
        self.socket.write(data_to_send, timeout)

    def end_stream(
        self, stream_id: int, timeout: Dict[str, Optional[float]]
    ) -> None:
        self.h2_state.end_stream(stream_id)
        data_to_send = self.h2_state.data_to_send()
        self.socket.write(data_to_send, timeout)

    def acknowledge_received_data(
        self, stream_id: int, amount: int, timeout: Dict[str, Optional[float]]
    ) -> None:
        self.h2_state.acknowledge_received_data(amount, stream_id)
        data_to_send = self.h2_state.data_to_send()
        self.socket.write(data_to_send, timeout)

    def close_stream(self, stream_id: int) -> None:
        del self.streams[stream_id]
        del self.events[stream_id]

        if not self.streams:
            if self.state == ConnectionState.ACTIVE:
                self.state = ConnectionState.IDLE
            elif self.state == ConnectionState.FULL:
                self.close()


class SyncHTTP2Stream:
    def __init__(self, stream_id: int, connection: SyncHTTP2Connection) -> None:
        self.stream_id = stream_id
        self.connection = connection

    def request(
        self,
        method: bytes,
        url: Tuple[bytes, bytes, int, bytes],
        headers: List[Tuple[bytes, bytes]] = None,
        stream: SyncByteStream = None,
        timeout: Dict[str, Optional[float]] = None,
    ) -> Tuple[bytes, int, bytes, List[Tuple[bytes, bytes]], SyncByteStream]:
        headers = [] if headers is None else [(k.lower(), v) for (k, v) in headers]
        stream = SyncByteStream() if stream is None else stream
        timeout = {} if timeout is None else timeout

        # Send the request.
        seen_headers = set(key for key, value in headers)
        has_body = (
            b"content-length" in seen_headers or b"transfer-encoding" in seen_headers
        )

        self.send_headers(method, url, headers, has_body, timeout)
        if has_body:
            self.send_body(stream, timeout)

        # Receive the response.
        status_code, headers = self.receive_response(timeout)
        reason_phrase = get_reason_phrase(status_code)
        stream = SyncByteStream(
            iterator=self.body_iter(timeout), close_func=self._response_closed
        )

        return (b"HTTP/2", status_code, reason_phrase, headers, stream)

    def send_headers(
        self,
        method: bytes,
        url: Tuple[bytes, bytes, int, bytes],
        headers: List[Tuple[bytes, bytes]],
        has_body: bool,
        timeout: Dict[str, Optional[float]],
    ) -> None:
        scheme, hostname, port, path = url
        default_port = {b"http": 80, b"https": 443}.get(scheme)
        authority = b"%s:%d" % (hostname, port) if port != default_port else hostname

        headers = [
            (b":method", method),
            (b":authority", authority),
            (b":scheme", scheme),
            (b":path", path),
        ] + [(k, v) for k, v in headers if k not in (b"host", b"transfer-encoding")]
        end_stream = not has_body

        self.connection.send_headers(self.stream_id, headers, end_stream, timeout)

    def send_body(
        self, stream: SyncByteStream, timeout: Dict[str, Optional[float]]
    ) -> None:
        for data in stream:
            while data:
                max_flow = self.connection.wait_for_outgoing_flow(
                    self.stream_id, timeout
                )
                chunk_size = min(len(data), max_flow)
                chunk, data = data[:chunk_size], data[chunk_size:]
                self.connection.send_data(self.stream_id, chunk, timeout)

        self.connection.end_stream(self.stream_id, timeout)

    def receive_response(
        self, timeout: Dict[str, Optional[float]]
    ) -> Tuple[int, List[Tuple[bytes, bytes]]]:
        """
        Read the response status and headers from the network.
        """
        while True:
            event = self.connection.wait_for_event(self.stream_id, timeout)
            if isinstance(event, h2.events.ResponseReceived):
                break

        status_code = 200
        headers = []
        for k, v in event.headers:
            if k == b":status":
                status_code = int(v.decode("ascii", errors="ignore"))
            elif not k.startswith(b":"):
                headers.append((k, v))

        return (status_code, headers)

    def body_iter(
        self, timeout: Dict[str, Optional[float]]
    ) -> Iterator[bytes]:
        while True:
            event = self.connection.wait_for_event(self.stream_id, timeout)
            if isinstance(event, h2.events.DataReceived):
                amount = event.flow_controlled_length
                self.connection.acknowledge_received_data(
                    self.stream_id, amount, timeout
                )
                yield event.data
            elif isinstance(event, (h2.events.StreamEnded, h2.events.StreamReset)):
                break

    def _response_closed(self) -> None:
        self.connection.close_stream(self.stream_id)
