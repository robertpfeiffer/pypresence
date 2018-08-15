import asyncio
import inspect
import json
import os
import struct
import sys
import time

from .exceptions import *
from .utils import *


class Presence:
    def __init__(self, client_id, pipe=0, loop=None, handler=None):
        client_id = str(client_id)
        if sys.platform == 'linux' or sys.platform == 'darwin':
            self.ipc_path = (
                os.environ.get(
                    'XDG_RUNTIME_DIR',
                    None) or os.environ.get(
                    'TMPDIR',
                    None) or os.environ.get(
                    'TMP',
                    None) or os.environ.get(
                    'TEMP',
                    None) or '/tmp') + '/discord-ipc-'+str(pipe)
            self.loop = asyncio.get_event_loop()
        elif sys.platform == 'win32':
            self.ipc_path = r'\\?\pipe\discord-ipc-'+str(pipe)
            self.loop = asyncio.ProactorEventLoop()
        
        if loop is not None:
            self.loop = loop
        
        self.sock_reader: asyncio.StreamReader = None
        self.sock_writer: asyncio.StreamWriter = None
        self.client_id = client_id

        self.connected=False

        if handler is not None:
            if not inspect.isfunction(handler):
                raise PyPresenceException('Error handler must be a function.')
            args = inspect.getfullargspec(handler).args
            if args[0] == 'self': args = args[1:]
            if len(args) != 2:
                raise PyPresenceException('Error handler should only accept two arguments.')

            loop.set_exception_handler(self._err_handle)
            self.handler = handler

    def _find_rpc_pipe(self,pipe):
        if pipe is None:
             file_pattern='discord-ipc-[0-9]'
        elif type(pipe)== int:
            file_pattern='discord-ipc-{}'.format(pipe)
        elif type(pipe)==str and os.path.exists(pipe):
            self.ipc_path=pipe
            return
        else:
            raise PyPresenceException("pipe should be path, int, or None, but got {} ({})".format(type(pipe), pipe))
        
        if sys.platform == 'linux' or sys.platform == 'darwin':
            # not os.name == 'posix', no Discord on Cygwin/Solaris/BSD
            import glob
            # can only glob IPC paths on UNIX
            vars = ['XDG_RUNTIME_DIR','TMPDIR','TMPDIR','TMP','TEMP']
            dirs=[os.environ[var] for var in vars
                                          if var in os.environ] + ["/tmp"]
            for d in dirs:
                paths=glob.glob(os.path.join(d,file_pattern))
                if paths:
                    self.ipc_path=paths[0]
                    break
            else:
                raise InvalidPipe("No IPC pipe found. Is Discord running?")

        elif sys.platform == 'win32':
            # TODO find a way to quit early if Discord isn't running
            # Can't use os.path here
            self.ipc_path=r'\\?\pipe\discord-ipc-{}'.format(pipe or 0)
        else:
            raise PyPresenceException('unsupported platform: {} ({})'
                            .format(sys.platform, os.name))


    def _err_handle(self, loop, context):
        if inspect.iscoroutinefunction(self.handler):
            loop.run_until_complete(self.handler(context['exception'], context['future']))
        else:
            self.handler(context['exception'], context['future'])

    @asyncio.coroutine
    def read_output(self):
        # see https://github.com/discordapp/discord-rpc/blob/master/documentation/hard-mode.md
        
        try:
            message_header = yield from self.sock_reader.read(8)
            code, length = struct.unpack('<II', message_header)
            payload = yield from self.sock_reader.read(length)
        except BrokenPipeError:
            self.connected=False
            raise InvalidPipe
        assert length==len(payload)
        parsed=json.loads(payload.decode('utf-8'))
        if parsed["evt"]=="ERROR":
            raise ServerError(parsed["data"]["message"])
        return parsed

    def send_data(self, op: int, payload: dict):
        payload = json.dumps(payload).encode('utf-8')
        length=len(payload)
        # endode first, then take length, because of multibyte code points
        self.sock_writer.write(
            struct.pack(
                '<II',
                op,
                length)
            + payload)

    @asyncio.coroutine
    def handshake(self):
        if sys.platform == 'linux' or sys.platform == 'darwin':
            try:
                self.sock_reader, self.sock_writer = yield from asyncio.open_unix_connection(self.ipc_path, loop=self.loop)
            except ConnectionRefusedError as err:
                raise InvalidPipe
        elif sys.platform == 'win32':
            self.sock_reader = asyncio.StreamReader(loop=self.loop)
            reader_protocol = asyncio.StreamReaderProtocol(
                self.sock_reader, loop=self.loop)
            try:
                self.sock_writer, _ = yield from self.loop.create_pipe_connection(lambda: reader_protocol, self.ipc_path)
            except FileNotFoundError as err:
                raise InvalidPipe
        
        self.send_data(0, {'v': 1, 'client_id': self.client_id})
        
        payload = yield from self.read_output()
        
        if "code" in payload:
            # see https://discordapp.com/developers/docs/topics/opcodes-and-status-codes#rpc-rpc-close-event-codes
            if payload["code"] == 4000:
                raise InvalidID
            raise ServerError(payload["message"])
                
        # First response should be READY
        # see https://discordapp.com/developers/docs/topics/rpc#ready
        if "cmd" in payload and payload["cmd"]=="DISPATCH" and "evt" in payload and payload["evt"]=="READY":
            assert payload["data"]["v"]==1

            self.config_data=payload["data"]["config"]
            self.user_data=payload["data"]["user"]
            self.connected=True
        return payload


    def update(self,pid=os.getpid(),state=None,details=None,start=None,end=None,large_image=None,large_text=None,small_image=None,small_text=None,party_id=None,party_size=None,join=None,spectate=None,match=None,instance=True):
        current_time = time.time()
        payload = {
            "cmd": "SET_ACTIVITY",
            "args": {
                "pid": pid,
                "activity": {
                    "state": state,
                    "details": details,
                    "timestamps": {
                        "start": start,
                        "end": end
                    },
                    "assets": {
                        "large_image": large_image,
                        "large_text": large_text,
                        "small_image": small_image,
                        "small_text": small_text
                    },
                    "party": {
                        "id": party_id,
                        "size": party_size
                    },
                    "secrets": {
                        "join": join,
                        "spectate": spectate,
                        "match": match
                    },
                    "instance": instance,
                },
            },
            "nonce": '{:.20f}'.format(current_time)
        }
        payload = remove_none(payload)

        self.send_data(1, payload)
        return self.loop.run_until_complete(self.read_output())

    def clear(self,pid=os.getpid()):
        current_time = time.time()
        payload = {
            "cmd": "SET_ACTIVITY",
            "args": {
                "pid": pid,
                "activity": None
            },
            "nonce": '{:.20f}'.format(current_time)
        }
        self.send_data(1, payload)
        return self.loop.run_until_complete(self.read_output())
    
    def connect(self):
        return self.loop.run_until_complete(self.handshake())

    def close(self):
        self.send_data(2, {'v': 1, 'client_id': self.client_id})
        self.sock_writer.close()
        self.loop.close()
