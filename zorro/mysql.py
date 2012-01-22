import socket
import errno
import struct
import os.path
import hashlib
import warnings
from collections import namedtuple
from decimal import Decimal

from .core import gethub, Lock, Future
from . import channel


OK_PACKET = (bytearray(b'\x00\x00\x00\x02\x00\x00\x00'),)
FIELD_STR = struct.Struct('<HLBHB')
FIELD_MAPPING = {
    0x00: Decimal,
    0x01: int,
    0x02: int,
    0x03: int,
    0x04: float,
    0x05: float,
    0x06: lambda a: None,
    0x09: int,
    0x0d: int,
    0x0f: lambda a: str(a, 'utf-8'),
    0xf6: Decimal,
    0xf7: lambda a: str(a, 'utf-8'),
    0xf8: lambda a: set(s.decode('utf-8').split(',')),
    0xf9: bytes,
    0xfa: bytes,
    0xfb: bytes,
    0xfc: bytes,
    0xfd: lambda a: str(a, 'utf-8'),
    0xfe: lambda a: str(a, 'utf-8'),
    }


def _read_lcb(buf, pos=0):
    num = buf[pos]
    if num < 251:
        return num, pos+1
    elif num == 251:
        return None, pos+1
    elif num == 252:
        return struct.unpack_from('<H', buf, pos+1), pos+3
    elif num == 253:
        return buf[pos+1] + (buf[pos+2] << 8) + (buf[pos+3] << 16), pos+4
    elif num == 254:
        return struct.unpack_from('<Q', buf, pos+1), pos+9

def _read_lcbytes(buf, pos=0):
    num = buf[pos]
    pos += 1
    if num < 251:
        return buf[pos:pos+num], pos+num
    elif num == 251:
        return None, pos
    elif num == 252:
        num = struct.unpack_from('<H', buf, pos)
        pos += 2
    elif num == 253:
        num = buf[pos] + (buf[pos+1] << 8) + (buf[pos+2] << 16)
        pos += 3
    elif num == 254:
        num = struct.unpack_from('<Q', buf, pos)
        pos += 8
    return buf[pos:pos+num], pos+num

def _read_lcstr(buf, pos=0):
    num = buf[pos]
    pos += 1
    if num < 251:
        pass
    elif num == 251:
        return None, pos
    elif num == 252:
        num = struct.unpack_from('<H', buf, pos)
        pos += 2
    elif num == 253:
        num = buf[pos] + (buf[pos+1] << 8) + (buf[pos+2] << 16)
        pos += 3
    elif num == 254:
        num = struct.unpack_from('<Q', buf, pos)
        pos += 8
    return buf[pos:pos+num].decode('utf-8'), pos+num


class MysqlError(Exception):

    def __init__(self, errno, sqlstate, message):
        self.errno = errno
        self.sqlstate = sqlstate
        self.message = message

    def __str__(self):
        return '({}:{}) {}'.format(self.errno, self.sqlstate, self.message)


_Field = namedtuple('_Field', 'catalog db table org_table name org_name'
        ' charsetnr length type flags decimals default')
class Field(_Field):
    __slots__ = ()

    @classmethod
    def parse_packet(cls, packet, pos=0):
        catalog, pos = _read_lcstr(packet, pos)
        db, pos = _read_lcstr(packet, pos)
        table, pos = _read_lcstr(packet, pos)
        org_table, pos = _read_lcstr(packet, pos)
        name, pos = _read_lcstr(packet, pos)
        org_name, pos = _read_lcstr(packet, pos)
        pos += 1
        charset, length, type, flags, decimals \
            = FIELD_STR.unpack_from(packet, pos)
        pos += FIELD_STR.size + 2
        if len(packet) > pos:
            default = _read_lcstr(packet, pos)
        else:
            default = None
        return cls(catalog, db, table, org_table, name, org_name,
            charset, length, type, flags, decimals, default)


class Resultset(object):

    def __init__(self, reply, nfields, extra):
        self.nfields = nfields
        self.extra = extra
        self.fields = [Field.parse_packet(fp) for fp in reply[1:nfields+1]]
        self.reply = reply

    def __iter__(self):
        for rpacket in self.reply[self.nfields+2:-1]:
            row = {}
            pos = 0
            for f in self.fields:
                col, pos = _read_lcbytes(rpacket, pos)
                cvt = FIELD_MAPPING.get(f.type)
                if cvt is None:
                    raise RuntimeError('{} is not supported'.format(f.type))
                row[f.name] = cvt(col)
            yield row

    def tuples(self):
        buf = []
        for rpacket in self.reply[self.nfields+2:-1]:
            pos = 0
            for f in self.fields:
                col, pos = _read_lcbytes(rpacket, pos)
                cvt = FIELD_MAPPING.get(f.type)
                if cvt is None:
                    raise RuntimeError('{} is not supported'.format(f.type))
                buf.append(cvt(col))
            yield tuple(buf)
            del buf[:]


execute_result = namedtuple('ExecuteResult', 'insert_id affected_rows')


class Capabilities(object):

    def __init__(self, num):
        self.long_password = bool(num & 1)
        self.found_rows = bool(num & 2)
        self.long_flag = bool(num & 4)
        self.connect_with_db = bool(num & 8)
        self.no_schema = bool(num & 16)
        self.compress = bool(num & 32)
        self.odbc = bool(num & 64)
        self.local_files = bool(num & 128)
        self.ignore_space = bool(num & 256)
        self.protocol_41 = bool(num & 512)
        self.interactive = bool(num & 1024)
        self.ssl = bool(num & 2048)
        self.ignore_sigpipe = bool(num & 4096)
        self.transactions = bool(num & 8192)
        self.secure_connection = bool(num & 32768)
        self.multi_statements = bool(num & 65536)
        self.multi_results = bool(num & 131072)

    def to_int(self):
        num = 0
        if self.long_password: num |= 1
        if self.found_rows: num |= 2
        if self.long_flag: num |= 4
        if self.connect_with_db: num |= 8
        if self.no_schema: num |= 16
        if self.compress: num |= 32
        if self.odbc: num |= 64
        if self.local_files: num |= 128
        if self.ignore_space: num |= 256
        if self.protocol_41: num |= 512
        if self.interactive: num |= 1024
        if self.ssl: num |= 2048
        if self.ignore_sigpipe: num |= 4096
        if self.transactions: num |= 8192
        if self.secure_connection: num |= 32768
        if self.multi_statements: num |= 65536
        if self.multi_results: num |= 131072
        return num



class Channel(channel.PipelinedReqChannel):
    BUFSIZE = 16384

    def __init__(self, host, port, unixsock):
        super().__init__()
        if host == 'localhost':
            if os.path.exists(unixsock):
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                addr = unixsock
        if sock is None:
            sock = socket.socket(socket.AF_INET,
                socket.SOCK_STREAM, socket.IPPROTO_TCP)
            addr = (host, port)
        self._sock = sock
        self._sock.setblocking(0)
        self.__addr = addr

    def connect(self, user, password, database):
        try:
            return self._connect(user, password, database)
        except Exception:
            self._alive = False
            raise

    def _connect(self, user, password, database):
        fut = Future()
        self._producing.append((None, fut))
        try:
            self._sock.connect(self.__addr)
        except socket.error as e:
            if e.errno == errno.EINPROGRESS:
                gethub().do_write(self._sock)
            else:
                raise
        handshake, = fut.get()

        assert handshake[0] == 10, "Wrong protocol version"
        prefix, suffix = handshake[0:].split(b'\0', 1)
        self.thread_id, scramble, caplow, self.language, \
        self.status, caphigh, scrlen = struct.unpack_from('<L8sxHBHHB', suffix)
        self.capabilities = Capabilities((caphigh << 16) + caplow)
        assert self.capabilities.protocol_41, "Old protocol is not supported"
        assert self.capabilities.connect_with_db
        self.capabilities.odbc = False
        self.capabilities.compress = False
        self.capabilities.multi_statement = False
        self.capabilities.multi_results = False
        self.capabilities.ssl = False
        self.capabilities.transactions = False
        buf = bytearray(b'\x00\x00\x00\x01')
        buf += struct.pack('<L4sB23s',
            self.capabilities.to_int()&0xFFFF,
            b'\x8f\xff\xff\xff',
            33, # utf-8 character set with general collation
            b'\x00'*23)
        buf += user.encode('ascii')
        buf += b'\x00'
        if password:
            buf += '\x14'
            hash1 = hashlib.sha1(password.encode('ascii')).digest()
            hash2 = hashlib.sha1(scramble
                + hashlib.sha1(hash1).digest()).digest()
            buf += bytes(a^b for a, b in zip(hash1, hash2))
        else:
            buf += b'\x00'
        buf += database.encode('ascii')
        buf += b'\x00'
        ln = len(buf)-4
        buf[0] = ln & 0xFF
        buf[1] = (ln >> 8) & 0xFF
        buf[2] = (ln >> 16) & 0xFF
        value = self.request(buf).get()
        assert value == OK_PACKET, value

    def sender(self):
        buf = bytearray()

        add_chunk = buf.extend
        wait_write = gethub().do_write
        sock = self._sock

        while True:
            if not buf:
                self.wait_requests()
            wait_write(sock)
            for chunk in self.get_pending_requests():
                add_chunk(chunk)
            try:
                bytes = sock.send(buf)
            except socket.error as e:
                if e.errno in (errno.EAGAIN, errno.EINTR):
                    continue
                else:
                    raise
            if not bytes:
                raise EOFError()
            del buf[:bytes]

    def receiver(self):
        buf = bytearray()

        sock = self._sock
        wait_read = gethub().do_read
        add_chunk = buf.extend
        pos = 0
        current = []
        fields = False
        handshake = 0

        while True:
            if pos*2 > len(buf):
                del buf[:pos]
                pos = 0
            wait_read(sock)
            try:
                bytes = sock.recv(self.BUFSIZE)
                if not bytes:
                    raise EOFError()
                add_chunk(bytes)
            except socket.error as e:
                if e.errno in (errno.EAGAIN, errno.EINTR):
                    continue
                else:
                    raise
            while len(buf)-pos >= 4:
                length = buf[pos] + (buf[pos+1] << 8) + (buf[pos+2] << 16)
                if len(buf)-pos < length+4:
                    continue
                num = buf[pos+3]
                ptype = buf[pos+4]
                current.append(buf[pos+4:pos+length+4])
                if handshake < 2:
                    ptype = 0
                    handshake += 1
                else:
                    assert num == len(current), (current, num)
                if ptype in (0, 0xff):
                    self.produce(tuple(current))
                    del current[:]
                    fields = False
                elif ptype == 0xfe:
                    if fields:
                        self.produce(tuple(current))
                        del current[:]
                        fields = False
                    else:
                        fields = True
                pos += length+4


class Mysql(object):

    def __init__(self, host='localhost', port=3306,
                       unixsock='/var/run/mysqld/mysqld.sock',
                       user='root', password='', database='test'):
        self._channel = None
        self._channel_lock = Lock()
        self.host = host
        self.port = port
        self.unixsock = unixsock
        self.user = user
        self.password = password
        self.database = database

    def channel(self):
        if not self._channel:
            with self._channel_lock:
                if not self._channel:
                    self._channel = Channel(
                        self.host, self.port,
                        unixsock=self.unixsock)
                    self._channel.connect(self.user, self.password,
                        database=self.database)
        return self._channel

    def execute(self, query):
        chan = self.channel()
        buf = bytearray(b'\x00\x00\x00\x00')
        buf += b'\x03'
        buf += query.encode('utf-8')
        ln = len(buf)-4
        buf[0] = ln & 0xFF
        buf[1] = (ln >> 8) & 0xFF
        buf[2] = (ln >> 16) & 0xFF
        reply = chan.request(buf).get()
        assert len(reply) == 1, "Use query for queries that return result set"
        reply = reply[0]
        assert reply[0] == 0, reply
        pos = 1
        affected_rows, pos = _read_lcb(reply, pos)
        insert_id, pos = _read_lcb(reply, pos)
        server_status, warning_count = struct.unpack_from('<HH', reply, pos)
        pos += 4
        if warning_count:
            warnings.warn("Query {0!r} caused warnings".format(query))
        return execute_result(insert_id, affected_rows)

    def query(self, query):
        chan = self.channel()
        buf = bytearray(b'\x00\x00\x00\x00')
        buf += b'\x03'
        buf += query.encode('utf-8')
        ln = len(buf)-4
        buf[0] = ln & 0xFF
        buf[1] = (ln >> 8) & 0xFF
        buf[2] = (ln >> 16) & 0xFF
        reply = chan.request(buf).get()
        assert reply[0][0] not in (0, 0xFF, 0xFE), \
            "Use execute for statements that does not return a result set"
        nfields, pos = _read_lcb(reply[0], 0)
        if pos < len(reply[0]):
            extra, pos = _read_lcb(reply[0], pos)
        else:
            extra = 0
        return Resultset(reply, nfields, extra)
