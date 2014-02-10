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

HTTP_METHODS = set(["GET", "POST", "HEAD", "PUT", "DELETE",
                    "OPTIONS", "TRACE", "CONNECT"])


def ensure_string(data):
    """ Utility function to convert bytes to string when neccessary """
    if isinstance(data, bytes):
        return data.decode()
    return data


def ensure_bytes(data):
    """ Utility function to convert string to bytes when neccessary """
    if isinstance(data, str):
        return data.encode()
    return data


class NoDataException(Exception):
    """ Exception signaling that no more requests are available """
    pass


class AsyncHTTPRequest(asynchat.async_chat):
    """
    Class for handling HTTP(S) request asynchronously.
    Both fixed content-length and chunked encoding are supported. Request data
    is read from consumer suplied by RequestManager and the response is
    passed to the feeder, also supplied by RequestManager.
    """

    state = HEADER
    response = None
    established = False
    want_read = True
    want_write = True

    def __init__(self, manager, slot=0):
        """
        Params:
            manager     should be instance of RequestManager
            slot        integer designating slot when using parallel execution,
                        this is only stored and passed back to RequestManager
                        after the response is received
        """
        asynchat.async_chat.__init__(self)
        self.last_read = time.time()
        self.manager = manager
        self.consumer = manager.consumer_type()
        self.slot = slot
        self.set_terminator(b'\r\n\r\n')

        # get request data from feeder
        request = ensure_bytes(self.manager.feeder.get_request())
        # pass request to consumer, for logging purposes
        self.consumer.set_request(request)

        # prepare the request for sending
        self.push(request)
        # connect to the host asynchronously
        self.established = not self.manager.https
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.connect((self.manager.host, self.manager.port))

    def collect_incoming_data(self, data):
        """ Called by asyncore when terminator is found """
        self.last_read = time.time()
        if self.state is HEADER or self.state is CHUNKED:
            self.incoming.append(data)
        elif (self.state is BODY or self.state is CHUNK):
            self.consumer.feed(data)

    def _find_header(self, name, default, headers):
        """ Finds header in string and return its value """
        p = headers.find(name)
        if p < 0:
            return default
        s = p + len(name) + 2
        e = s + headers[s:].find("\n")
        return headers[s:e].rstrip("\r\n").lower()

    def found_terminator(self):
        """ Called by asyncore when terminator is found """
        self.last_read = time.time()
        if self.state is HEADER:
            # reading headers
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
            # reading chunked response
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
        """ Called by asyncore when socket is closed """
        if self.manager.https:
            self.socket = self._socket
        asynchat.async_chat.handle_close(self)
        try:
            # this request is done, tell RequestManager new one can be spawned
            self.manager.add_channel(self.slot)
        except NoDataException:
            pass

    def readable(self):
        """ Called by asyncore to determine if we're ready to read """
        return self.want_read and asynchat.async_chat.readable(self)

    def writable(self):
        """ Called by asyncore to determine if we're ready to write """
        if time.time() - self.last_read > self.manager.timeout:
            # should signal timeout here
            self.state = BODY
            self.found_terminator()
            return False
        return self.want_write and asynchat.async_chat.writable(self)

    def _handshake(self):
        """ Performs ssl handshake when HTTPS protocol is used """
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
        """ Called by asyncore when socket is ready for writing """
        if self.established:
            return self.initiate_send()
        self._handshake()

    def handle_read(self):
        """ Called by asyncore when socket is ready for reading """
        if self.established:
            return asynchat.async_chat.handle_read(self)
        self._handshake()

    def handle_connect(self):
        """ Called by asyncore when socket is really opened """
        if self.manager.https:
            # we replace regular socket with ssl socket
            self._socket = self.socket
            self.socket = ssl.wrap_socket(
                self._socket,
                do_handshake_on_connect=False)


class BaseFeeder(object):
    """
    Base class for feeders. Only method that must be implemented by inheriting
    classes is get_request()
    """
    def get_request(self):
        """ Should return request data or raise NoDataException """
        raise NotImplementedError()


class SimpleFeeder(BaseFeeder):
    """
    Basic implementation of feeder, repeats the same request
    for specified amount of times.
    """
    def __init__(self, request, count):
        """
        Params:
            request     Request data, must end with '\r\n\r\n'
            count       How many times to repeat the request
        """
        self.counter = count
        self.request = request

    def get_request(self):
        """ Returns the request data as many times as requested in __init__ """
        if self.counter > 0:
            self.counter -= 1
            return self.request
        raise NoDataException()


class FileFeeder(BaseFeeder):
    """
    Feeder implementation that reads the request from specified file.
    The requests in file should with 'GET', 'POST', etc. and be terminated
    by '\n\n' or '\r\n\r\n' (line endings style doesn't matter). Additional
    data between requests are ignored.
    """
    def __init__(self, filename="/dev/stdin"):
        """
        Params:
            filename    File to read, defaults to standard input
        """
        self.f = open(filename, 'r')

    def _find_start(self):
        """ Searches file for beginning of next request """
        while True:
            line = self.f.readline()
            if line == "":
                raise NoDataException
            for cmd in HTTP_METHODS:
                if line.startswith(cmd):
                    return line

    def _read_headers(self):
        """
        Reads all headers, returns the data read and content-length if present
        """
        headers = self._find_start()
        length = 0
        while True:
            line = self.f.readline().rstrip("\r\n")
            if line is None:
                raise NoDataException
            headers += line + "\r\n"
            if line.lower().startswith("content-length:"):
                length = int(line[16:])
            if line == "":
                return headers, length
        return headers, length

    def get_request(self):
        """ Returns one request read from file """
        req, length = self._read_headers()
        req += self.f.read(length)
        return req


class BaseConsumer(object):
    """
    Base class for consumers. Only method that must be implemented
    by inheriting classes is feed().
    """
    def set_request(self, data):
        """ Called as soon as request is read from feeder. """
        pass

    def feed(self, data):
        """ Called to pass parts of response data as they arrive. """
        raise NotImplementedError()

    def close(self):
        """ Called after entire response is received. """
        pass


class PrintConsumer(BaseConsumer):
    """
    Simple implementation of consumer that caches the request and response data
    and prints them to standard output when they are complete.
    """
    def __init__(self):
        self.data = []

    def set_request(self, data):
        """ Stores request data and adds captions. """
        self.data.extend([
            b'== REQUEST ',
            ensure_bytes(str(datetime.now())),
            b' ==\n',
            data,
            b'== RESPONSE ==\n'])

    def feed(self, data):
        """ Stores the received data. """
        self.data.append(data)

    def _finished(self):
        self.data.extend([
            b'== FINISHED ',
            ensure_bytes(str(datetime.now())),
            b' =='])

    def close(self):
        """ Prints all the data collected so far and end caption. """
        self._finished()
        print(ensure_string(b''.join(self.data)))


def FileConsumer(filename):
    """
    Factory function returning consumer class with preset filename to
    write into. Also, the file is truncated when called, to assure it can be
    accessed and to scrape results from previous runs.
    """
    class _FileConsumer(PrintConsumer):
        """
        Slight modification of PrintConsumer that appends the data into
        a file instead of standard output.
        """
        def close(self):
            """ Prints collected data to file """
            self._finished()
            with open(filename, 'ab') as f:
                [f.write(d) for d in self.data]
    f = open(filename, 'w')
    f.close()
    return _FileConsumer


class DummyConsumer(BaseConsumer):
    """ Consumer implementation that simply discards all data it recieves. """
    def feed(self, data):
        pass


class RequestManager(object):
    """
    Class managing creation of requests. Contains all neccessary information,
    including feeder and consumer.
    """
    def __init__(self, feeder, consumer_type=DummyConsumer, host='localhost',
                 port=80, parallel=1, https=False, timeout=30):
        """
        Params:
            feeder          Instance of BaseFeeder inherited class
            consumer_type   Type of consumer (new one is constructed for
                            each request)
            host            Hostname or IP address of remote end
            port            Port to talk to
            parallel        How many request should be used at any given moment
            https           Whether to use https or http
            timeout         How long to wait for reply
        """
        self.feeder = feeder
        self.consumer_type = consumer_type
        self.host = host
        self.port = port
        self.https = https
        self.timeout = timeout
        self._prepare_requests(parallel)

    def _prepare_requests(self, count):
        """ Creates given number of requests """
        self.slots = [None] * count
        try:
            for i in range(count):
                self.add_channel(i)
        except NoDataException:
            pass

    def add_channel(self, slot):
        """ Replaces finished request in given slot by a new one """
        self.slots[slot] = AsyncHTTPRequest(self, slot)


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
