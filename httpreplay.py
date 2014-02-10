#!/usr/bin/python

import asynchat
import asyncore
import socket
import ssl
import time
from datetime import datetime

HEADER = object()
CHUNKED = object()
CHUNK = object()
BODY = object()


def ensure_string(data):
    if isinstance(data, bytes):
        return data.decode()
    return data


def ensure_bytes(data):
    if isinstance(data, str):
        return data.encode()
    return data


class NoDataException(Exception):
    pass


class AsyncHTTPRequest(asynchat.async_chat):
    state = HEADER
    response = None
    established = False
    want_read = True
    want_write = True

    def __init__(self, manager, seq):
        self.last_read = time.time()
        self.manager = manager
        self.consumer = manager.consumer_type()
        self.seq = seq
        self.set_terminator(b'\r\n\r\n')

        request = self.manager.feeder.get_request()
        if request is None:
            raise NoDataException()
        request = ensure_bytes(request)
        self.established = not self.manager.https
        self.consumer.set_request(request)

        asynchat.async_chat.__init__(self)
        self.push(request)
        # connect to the host asynchronously
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.connect((self.manager.host, self.manager.port))

    def collect_incoming_data(self, data):
        self.last_read = time.time()
        if self.state is HEADER or self.state is CHUNKED:
            self.incoming.append(data)
        elif (self.state is BODY or self.state is CHUNK):
            self.consumer.feed(data)

    def _find_header(self, name, default, headers):
        p = headers.find(name)
        if p < 0:
            return default
        s = p + len(name) + 2
        e = s + headers[s:].find("\n")
        return headers[s:e].rstrip("\r\n").lower()

    def found_terminator(self):
        self.last_read = time.time()
        if self.state is HEADER:
            self.state = BODY
            headers = self._get_data().rstrip() + b'\r\n\r\n'
            self.consumer.feed(headers)
            # chunked transfer encoding: useful for twitter, google, etc.
            headers = ensure_string(headers).lower()
            encoding = self._find_header("transfer-encoding", '', headers)
            length = int(self._find_header("content-length", '0', headers))

            if encoding == 'chunked':
                self.set_terminator(b'\r\n')
                self.state = CHUNKED
            else:
                self.set_terminator(length)

        elif self.state is CHUNKED:
            ch = ensure_string(self._get_data()).rstrip().partition(';')[0]
            if not ch:
                # it's probably the spare \r\n between chunks...
                return
            self.set_terminator(int(ch, 16))
            if self.terminator == 0:
                # no more chunks...
                self.state = BODY
                return self.found_terminator()
            self.state = CHUNK

        elif self.state is CHUNK:
            # prepare for the next chunk
            self.set_terminator(b'\r\n')
            self.state = CHUNKED

        else:
            # body is done being received, close the socket
            self.terminator = None
            self.consumer.close()
            self.handle_close()

    def handle_close(self):
        if self.manager.https:
            self.socket = self._socket
        asynchat.async_chat.handle_close(self)
        try:
            self.manager.add_channel(self.seq)
        except NoDataException:
            pass

    # https support
    def readable(self):
        return self.want_read and asynchat.async_chat.readable(self)

    def writable(self):
        if time.time() - self.last_read > self.manager.timeout:
            # should signal timeout here
            self.state = BODY
            self.found_terminator()
            return False
        return self.want_write and asynchat.async_chat.writable(self)

    def _handshake(self):
        try:
            self.socket.do_handshake()
        except ssl.SSLError as err:
            self.want_read = self.want_write = False
            if err.args[0] == ssl.SSL_ERROR_WANT_READ:
                self.want_read = True
            elif err.args[0] == ssl.SSL_ERROR_WANT_WRITE:
                self.want_write = True
            else:
                raise
        else:
            self.want_read = True
            self.want_write = True
            self.established = True

    def handle_write(self):
        if self.established:
            return self.initiate_send()

        self._handshake()

    def handle_read(self):
        if self.established:
            return asynchat.async_chat.handle_read(self)

        self._handshake()

    def handle_connect(self):
        if self.manager.https:
            self._socket = self.socket
            self.socket = ssl.wrap_socket(
                self._socket,
                do_handshake_on_connect=False)


class BaseFeeder(object):
    def get_request(self):
        raise NotImplementedError()


class SimpleFeeder(BaseFeeder):
    def __init__(self, request, count):
        self.counter = count
        self.request = request

    def get_request(self):
        if self.counter > 0:
            self.counter -= 1
            return self.request
        raise NoDataException()


class FileFeeder(BaseFeeder):
    def __init__(self, filename="/dev/stdin"):
        self.f = open(filename, 'r')

    def _find_start(self):
        while True:
            line = self.f.readline()
            if line == "":
                return None
            for cmd in ("GET", "POST", "HEAD", "PUT"):
                if line.startswith(cmd):
                    return line

    def _read_headers(self):
        headers = self._find_start()
        if headers is None:
            return None, None
        length = 0
        while True:
            line = self.f.readline().rstrip("\r\n")
            if line is None:
                return None, None
            headers += line + "\r\n"
            if line.lower().startswith("content-length:"):
                length = int(line[16:])
            if line == "":
                return headers, length
        return headers, length

    def get_request(self):
        req, length = self._read_headers()
        if req is None:
            return None
        req += self.f.read(length)
        return req


class BaseConsumer(object):
    def set_request(self, data):
        pass

    def feed(self, data):
        raise NotImplementedError()

    def close(self):
        pass


class PrintConsumer(BaseConsumer):
    def __init__(self):
        self.data = []

    def set_request(self, data):
        self.data.append(b'== REQUEST %s ==\n' % datetime.now())
        self.data.append(b'%s== RESPONSE ==\n' % data)

    def feed(self, data):
        self.data.append(data)

    def close(self):
        self.data.append(b'== FINISHED %s ==' % datetime.now())
        print(''.join(self.data))


def FileConsumer(filename):
    class _FileConsumer(PrintConsumer):
        def close(self):
            with open(filename, 'ab') as f:
                [f.write(d) for d in self.data]
    f = open(filename, 'w')
    f.close()
    return _FileConsumer


class DummyConsumer(BaseConsumer):
    def feed(self, data):
        pass


class RequestManager(object):
    def __init__(self, feeder, consumer_type=DummyConsumer, host='localhost',
                 port=80, parallel=1, https=False, timeout=30):
        self.feeder = feeder
        self.consumer_type = consumer_type
        self.host = host
        self.port = port
        self.https = https
        self.timeout = timeout
        self._prepare_queue(parallel)

    def _prepare_queue(self, parallel):
        self.queue = [None] * parallel
        try:
            for i in range(parallel):
                self.add_channel(i)
        except NoDataException:
            pass

    def add_channel(self, seq):
        self.queue[seq] = AsyncHTTPRequest(self, seq)


def main():
    print("== START ==")
    RequestManager(
        FileFeeder("/tmp/test"),
        FileConsumer('/tmp/szn'),
        host="77.75.72.3",
        parallel=10)
    RequestManager(
        SimpleFeeder("GET / HTTP/1.1\r\nHost: google.com\r\n\r\n", 100),
        PrintConsumer,
        host="google.cz",
        parallel=10)
    asyncore.loop(timeout=.25)
    print("== DONE ==")

if __name__ == '__main__':
    main()
