from pypresence import Presence, InvalidPipe, InvalidID
import time
import sys

client_id = sys.argv[1]
message = " ".join(sys.argv[2:])

print("usage:\n$ python rich-presence-cli.py CLIENT_ID [my message]")

RPC = Presence(client_id)

try:
    RPC.connect()
except InvalidPipe:
    print ("ERROR: could not connect, is Discord even running?")
    sys.exit(1)
except InvalidID:
    print ("ERROR: invalid client ID")
    sys.exit(1)
else:
    print("Discord is running, successfully connected")

try:
    while True:
        RPC.update(details=message)
        print ("status updated")
        time.sleep(60)
except KeyboardInterrupt:
    print ("interrupted, closing rpc")
    RPC.clear()
    RPC.close()
except InvalidPipe:
    print ("ERROR: Discord connection lost")
    sys.exit(1)
