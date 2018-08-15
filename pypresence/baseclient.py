import inspect
import json
import os
import struct
import sys
import time

from .exceptions import *
from .utils import *


class BaseClient:

    def __init__(self, client_id, pipe=0, loop=None, handler=None):
        self.client_id = str(client_id)
        
        self.connected=False
        self.listening=False
        self._events={}
        self.oauth_token = None
        
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

        if handler is not None:
            if not inspect.isfunction(handler):
                raise PyPresenceException('Error handler must be a function.')
            args = inspect.getfullargspec(handler).args
            if args[0] == 'self': args = args[1:]
            if len(args) != 2:
                raise PyPresenceException('Error handler should only accept two arguments.')

            loop.set_exception_handler(self._err_handle)
            self.handler = handler

    def _err_handle(self, loop, context):
        if inspect.iscoroutinefunction(self.handler):
            loop.run_until_complete(self.handler(context['exception'], context['future']))
        else:
            self.handler(context['exception'], context['future'])

    async def read_output(self):
        # see https://github.com/discordapp/discord-rpc/blob/master/documentation/hard-mode.md

        try:
            message_header = await self.sock_reader.readexactly(8)
            code, length = struct.unpack('<II', message_header)
            payload = await self.sock_reader.readexactly(length)
        except BrokenPipeError:
            self.connected=False
            raise InvalidPipe
        assert length==len(payload)
        parsed=json.loads(payload.decode('utf-8'))
        if parsed.get("evt", None)=="ERROR":
            raise ServerError(parsed["data"]["message"])
        return parsed

    def callback(self, event="NOTIFICATION_CREATE", **args):
        def register_inner(func):
            self.register_event(event, func, args)
        return register_inner
    
    def register_event(self, event: str, func, args={}):
        event=event.upper()
        if inspect.iscoroutinefunction(func):
            raise NotImplementedError
        elif len(inspect.signature(func).parameters) != 1:
            raise ArgumentError
        self.subscribe(event, args)
        self._events[event] = func

    def unregister_event(self, event: str, args={}):
        event=event.upper()
        if event not in self._events:
            raise EventNotFound
        self.unsubscribe(event, args)
        del self._events[event]

    def subscribe(self, event, args={}):
        current_time = time.time()
        payload = {
            "cmd": "SUBSCRIBE",
            "args": args,
            "evt": event.upper(),
            "nonce": '{:.20f}'.format(current_time)
        }
        sent = self.send_data(1, payload)
        return self.loop.run_until_complete(self.read_output())
    
    def unsubscribe(self, event, args={}):
        current_time = time.time()
        payload = {
            "cmd": "UNSUBSCRIBE",
            "args": args,
            "evt": event.upper(),
            "nonce": '{:.20f}'.format(current_time)
        }
        sent = self.send_data(1, payload)
        return self.loop.run_until_complete(self.read_output())
    
    async def respond_to_events(self):
        self.listening=True
        try:
            while self.connected and self.listening:
                event_json = await self.read_output()
                if event_json.get("cmd", None) == "DISPATCH":
                    evt=event_json.get("evt", None)
                    handler = self._events.get(evt, None)
                    if handler:
                        handler(event_json["data"])
        except InvalidPipe:    
            if self.sock_reader._eof:
                return
        finally:
            self.listening=False

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

            return response
        
    def close(self):
        self.send_data(2, {'v': 1, 'client_id': self.client_id})
        self.sock_writer.close()
        self.connected = False
        self.loop.close()
