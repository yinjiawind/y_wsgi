import sys
import socket
import select
import StringIO
import datetime
import threading
import time
import Queue
import errno

import config

_SHUTDOWNREQUEST = None


class WorkerThread(threading.Thread):
    """Thread which continuously polls a Queue for Connection objects.

    Due to the timing issues of polling a Queue, a WorkerThread does not
    check its own 'ready' flag after it has started. To stop the thread,
    it is necessary to stick a _SHUTDOWNREQUEST object onto the Queue
    (one for each running WorkerThread).
    """

    server = None
    """The HTTP Server which spawned this thread, and which owns the
    Queue and is placing active connections into it."""

    ready = False
    """A simple flag for the calling server to know when this thread
    has begun polling the Queue."""

    def __init__(self, server):
        self.ready = False
        self.server = server
        threading.Thread.__init__(self)

    def run(self):
        try:
            self.ready = True
            while True:
                conn = self.server.requests.get()
                if conn is _SHUTDOWNREQUEST:
                    return

                conn.handle_request()
        except (KeyboardInterrupt, SystemExit), exc:
            self.server.interrupt = exc


class ThreadPool(object):
    """A Request Queue for the CherryPyWSGIServer which pools threads.

    ThreadPool objects must provide min, get(), put(obj), start()
    and stop(timeout) attributes.
    """

    def __init__(self, server, min=10, max=-1):
        self.server = server
        self.min = min
        self.max = max
        self._threads = []
        self._queue = Queue.Queue()
        self.get = self._queue.get

    def start(self):
        """Start the pool of threads."""
        for i in range(self.min):
            self._threads.append(WorkerThread(self.server))
        for worker in self._threads:
            worker.setName("CP Server " + worker.getName())
            worker.start()
        for worker in self._threads:
            while not worker.ready:
                time.sleep(.1)

    def _get_idle(self):
        """Number of worker threads which are idle. Read-only."""
        return len([t for t in self._threads if t.conn is None])

    idle = property(_get_idle, doc=_get_idle.__doc__)

    def put(self, obj):
        self._queue.put(obj)
        if obj is _SHUTDOWNREQUEST:
            return

    def grow(self, amount):
        """Spawn new worker threads (not above self.max)."""
        for i in range(amount):
            if self.max > 0 and len(self._threads) >= self.max:
                break
            worker = WorkerThread(self.server)
            worker.setName("CP Server " + worker.getName())
            self._threads.append(worker)
            worker.start()

    def shrink(self, amount):
        """Kill off worker threads (not below self.min)."""
        # Grow/shrink the pool if necessary.
        # Remove any dead threads from our list
        for t in self._threads:
            if not t.isAlive():
                self._threads.remove(t)
                amount -= 1

        if amount > 0:
            for i in range(min(amount, len(self._threads) - self.min)):
                # Put a number of shutdown requests on the queue equal
                # to 'amount'. Once each of those is processed by a worker,
                # that worker will terminate and be culled from our list
                # in self.put.
                self._queue.put(_SHUTDOWNREQUEST)

    def stop(self, timeout=5):
        # Must shut down threads here so the code that calls
        # this method can know when all threads are stopped.
        for worker in self._threads:
            self._queue.put(_SHUTDOWNREQUEST)

        # Don't join currentThread (when stop is called inside a request).
        current = threading.currentThread()
        if timeout and timeout >= 0:
            endtime = time.time() + timeout
        while self._threads:
            worker = self._threads.pop()
            if worker is not current and worker.isAlive():
                try:
                    if timeout is None or timeout < 0:
                        worker.join()
                    else:
                        remaining_time = endtime - time.time()
                        if remaining_time > 0:
                            worker.join(remaining_time)
                        if worker.isAlive():
                            # We exhausted the timeout.
                            # Forcibly shut down the socket.
                            c = worker.conn
                            if c and not c.rfile.closed:
                                try:
                                    c.socket.shutdown(socket.SHUT_RD)
                                except TypeError:
                                    # pyOpenSSL sockets don't take an arg
                                    c.socket.shutdown()
                            worker.join()
                except (AssertionError,
                        # Ignore repeated Ctrl-C.
                        # See http://www.cherrypy.org/ticket/691.
                        KeyboardInterrupt), exc1:
                    pass

class Connection(object):
    def __init__(self, server, fileno):
        self.server = server
        self._fileno = fileno
        self._request_data = b''

    def handle_request(self):
        self.request_lines = self._request_data.splitlines()
        try:
            self.get_url_parameter()
            env = self.get_environ()
            app_data = self.server.application(env, self.start_response)
        except Exception, e:
            self.status = 500
            app_data = None
        finally:
            self.server.response_data[self._fileno] = self.gen_response_data(self.status, app_data)
            self.server.epoll.modify(self._fileno, select.EPOLLOUT)
            print '[{0}] "{1}" {2}'.format(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                           self.request_lines[0], self.status)

    def get_url_parameter(self):
        self.request_dict = {'Path': self.request_lines[0]}
        for index, item in enumerate(self.request_lines[1:]):
            if ':' in item:
                self.request_dict[item.split(':')[0]] = item.split(':')[1]
            else:
                break
        self.post_data = "\r\n".join(self.request_lines[index + 1:])
        self.request_method, self.path, self.request_version = self.request_dict.get('Path').split()
        self.query_string = ""
        self.path_parse()

    def path_parse(self):
        if '?' in self.path:
            parse = self.path.split("?")
            self.path = parse[0]
            self.query_string = "?".join(parse[1:])

    def get_environ(self):
        env = {
            'wsgi.version': (1, 0),
            'wsgi.url_scheme': 'http',
            'wsgi.input': StringIO.StringIO(self.post_data),
            'wsgi.errors': sys.stderr,
            'wsgi.multithread': False,
            'wsgi.multiprocess': False,
            'wsgi.run_once': False,
            'REQUEST_METHOD': self.request_method,
            'PATH_INFO': self.path,
            'SERVER_NAME': self.server.host,
            'SERVER_PORT': self.server.port,
            'USER_AGENT': self.request_dict.get('User-Agent'),
            'QUERY_STRING': self.query_string
        }
        return env

    def gen_response_data(self, status, app_data=None):
        response = 'HTTP/1.1 {status}\r\n'.format(status=status)
        for header in self.headers:
            response += '{0}: {1}\r\n'.format(*header)
        response += '\r\n'
        if app_data:
            response += app_data
        return response

    def start_response(self, status, response_headers):
        headers = [
            ('Date', datetime.datetime.now().strftime('%a, %d %b %Y %H:%M:%S GMT')),
            ('Server', 'EWSGI0.1'),
        ]
        self.headers = response_headers + headers
        self.status = status

    def read_request_data(self):
        try:
            data = self.server.connections[self._fileno].recv(1024)
            self._request_data += data
            if len(data) < 1024:
                self.server.requests.put(self)
        except socket.error, msg:
            if msg.errno == errno.EAGAIN:
                self.server.requests.put(self)
            else:
                self.server.epoll.unregister(self._fileno)
                self.server.connections[self._fileno].close()

class WSGIServer(object):
    def __init__(self, server_address, application):
        self.serversocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.serversocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.serversocket.bind(server_address)
        self.serversocket.listen(config.LISTEN_SIZE)
        self.serversocket.setblocking(0)
        self.serversocket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        host, port = self.serversocket.getsockname()[:2]
        self.epoll = select.epoll()
        self.epoll.register(self.serversocket.fileno(), select.EPOLLIN)
        self.host = host
        self.port = port
        self.application = application
        self.connections = {}
        self.request_conns = {}
        self.response_data = {}
        self.requests = ThreadPool(self)

    def serve_forever(self):
        try:
            self.requests.start()
            while True:
                events = self.epoll.poll(1)
                for fileno, event in events:
                    if fileno == self.serversocket.fileno():
                        connection, address = self.serversocket.accept()
                        connection.setblocking(0)
                        self.epoll.register(connection.fileno(), select.EPOLLIN)
                        self.connections[connection.fileno()] = connection
                        self.request_conns[connection.fileno()] = Connection(self, connection.fileno())
                        self.response_data[connection.fileno()] = b''
                    elif event & select.EPOLLIN:
                        self.request_conns[fileno].read_request_data()
                    elif event & select.EPOLLOUT:
                        del self.request_conns[fileno]
                        byteswritten = self.connections[fileno].send(self.response_data[fileno])
                        self.response_data[fileno] = self.response_data[fileno][byteswritten:]
                        if len(self.response_data[fileno]) == 0:
                            self.epoll.modify(fileno, 0)
                            self.connections[fileno].shutdown(socket.SHUT_RDWR)
                    elif event & select.EPOLLHUP:
                        self.epoll.unregister(fileno)
                        self.connections[fileno].close()
                        del self.connections[fileno]
        finally:
            self.epoll.unregister(self.serversocket.fileno())
            self.epoll.close()
            self.serversocket.close()

def application(environ, start_response):
    status = '200 OK'
    response_headers = [('Content-Type', 'text/plain')]
    start_response(status, response_headers)
    return 'Hello world'

if __name__ == '__main__':
    server = WSGIServer(("0.0.0.0", 8888), application)
    server.serve_forever()