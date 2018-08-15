import inspect
import json
import os
import struct
import sys

from .exceptions import *
from .utils import *


class BaseClient:

    def __init__(self, client_id, pipe=0, loop=None, handler=None):
        client_id = str(client_id)
        self.connected=False
        if sys.platform == 'linux' or sys.platform == 'darwin':
            # not os.name == 'posix'
            self.ipc_path = (
                                    os.environ.get(
                                        'XDG_RUNTIME_DIR',
                                        None) or os.environ.get(
                                'TMPDIR',
                                None) or os.environ.get(
                                'TMP',
                                None) or os.environ.get(
                                'TEMP',
                                None) or '/tmp') + '/discord-ipc-' + str(pipe)
            if not os.path.exists(self.ipc_path):
                raise InvalidPipe("No IPC pipe found. Is Discord running?")

        elif sys.platform == 'win32':
            self.ipc_path = r'\\?\pipe\discord-ipc-' + str(pipe)
        else:
            # no Discord on Cygwin/Solaris/BSD
            raise PyPresenceException('unsupported platform: {} ({})'
                            .format(sys.platform, os.name))

        if loop is not None:
            self.loop = loop
        elif sys.platform == 'linux' or sys.platform == 'darwin':
            self.loop = asyncio.get_event_loop()
        elif sys.platform == 'win32':
            self.loop = asyncio.ProactorEventLoop()

        self.sock_reader: asyncio.StreamReader = None
        self.sock_writer: asyncio.StreamWriter = None
        self.client_id = client_id

        if handler is not None:
            if not inspect.isfunction(handler):
                raise PyPresenceException('Error handler must be a function.')
            args = inspect.getfullargspec(handler).args
            if args[0] == 'self': args = args[1:]
            if len(args) != 2:
                raise PyPresenceException('Error handler should only accept two arguments.')

            loop.set_exception_handler(self._err_handle)
            self.handler = handler

        if getattr(self, "on_event", None):  # Tasty bad code ;^)
            self._events_on = True
        else:
            self._events_on = False

    def _err_handle(self, loop, context):
        if inspect.iscoroutinefunction(self.handler):
            loop.run_until_complete(self.handler(context['exception'], context['future']))
        else:
            self.handler(context['exception'], context['future'])

    async def read_output(self):
        # see https://github.com/discordapp/discord-rpc/blob/master/documentation/hard-mode.md

        try:
            message_header = await self.sock_reader.read(8)
            code, length = struct.unpack('<II', message_header)
            payload = await self.sock_reader.read(length)
        except BrokenPipeError:
            self.connected=False
            raise InvalidPipe
        assert length==len(payload)
        parsed=json.loads(payload.decode('utf-8'))
        if parsed.get("evt", None)=="ERROR":
            raise ServerError(parsed["data"]["message"])
        return parsed

    def send_data(self, op: int, payload: dict):
        payload = json.dumps(payload).encode('utf-8')
        length=len(payload)
        # encode first, then take length, because of multibyte code points
        self.sock_writer.write(
            struct.pack(
                '<II',
                op,
                length)
            + payload)

    async def handshake(self):
        if sys.platform == 'linux' or sys.platform == 'darwin':
            try:
                self.sock_reader, self.sock_writer = await asyncio.open_unix_connection(self.ipc_path, loop=self.loop)
            except ConnectionRefusedError as err:
                raise InvalidPipe
        elif sys.platform == 'win32':
            self.sock_reader = asyncio.StreamReader(loop=self.loop)
            reader_protocol = asyncio.StreamReaderProtocol(
                self.sock_reader, loop=self.loop)
            try:
                self.sock_writer, _ = await self.loop.create_pipe_connection(lambda: reader_protocol, self.ipc_path)
            except FileNotFoundError as err:
                raise InvalidPipe
        self.send_data(0, {'v': 1, 'client_id': self.client_id})

        response = await self.read_output()
        if "code" in response:
            # see https://discordapp.com/developers/docs/topics/opcodes-and-status-codes#rpc-rpc-close-event-codes
            if response["code"] == 4000:
                raise InvalidID
            raise ServerError(response["message"])

        if response.get("cmd", None)=="DISPATCH" and response.get("evt", None)=="READY":
            assert response["data"]["v"]==1

            self.config_data=response["data"]["config"]
            self.user_data=response["data"]["user"]
            self.connected=True

            if self._events_on:
                self.sock_reader.feed_data = self.on_event
            return response
