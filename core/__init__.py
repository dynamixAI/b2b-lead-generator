# This network's IPv6 is broken: connections hang until they time out.
# Browsers quietly fall back to IPv4, Python doesn't, so make every request use IPv4.
import socket

import urllib3.util.connection as _connection

_connection.allowed_gai_family = lambda: socket.AF_INET
