import sys
import socket
import select
import StringIO
import datetime

import config

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
        self.request_data = {}
        self.response_data = {}

    def serve_forever(self):
        try:
            while True:
                events = self.epoll.poll(1)
                for fileno, event in events:
                    if fileno == self.serversocket.fileno():
                        connection, address = self.serversocket.accept()
                        connection.setblocking(0)
                        self.epoll.register(connection.fileno(), select.EPOLLIN)
                        self.connections[connection.fileno()] = connection
                        self.request_data[connection.fileno()] = b''
                        self.response_data[connection.fileno()] = b''
                    elif event & select.EPOLLIN:
                        data = self.connections[fileno].recv(1024)
                        self.request_data[fileno] += data
                        if len(data) != 1024:
                            self.handle_request(fileno)
                            del self.request_data[fileno]
                            self.epoll.modify(fileno, select.EPOLLOUT)
                    elif event & select.EPOLLOUT:
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

    def handle_request(self, fileno):
        self.request_lines = self.request_data[fileno].splitlines()
        try:
            self.get_url_parameter()
            env = self.get_environ()
            app_data = self.application(env, self.start_response)
            self.response_data[fileno] = self.gen_response_data(app_data)
            print '[{0}] "{1}" {2}'.format(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                           self.request_lines[0], self.status)
        except Exception, e:
            pass

    def get_url_parameter(self):
        self.request_dict = {'Path': self.request_lines[0]}
        for index, item in enumerate(self.request_lines[1:]):
            if ':' in item:
                self.request_dict[item.split(':')[0]] = item.split(':')[1]
            else:
                break
        self.post_data = "\r\n".join(self.request_lines[index+1:])
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
            'SERVER_NAME': self.host,
            'SERVER_PORT': self.port,
            'USER_AGENT': self.request_dict.get('User-Agent'),
            'QUERY_STRING': self.query_string
        }
        return env

    def start_response(self, status, response_headers):
        headers = [
            ('Date', datetime.datetime.now().strftime('%a, %d %b %Y %H:%M:%S GMT')),
            ('Server', 'EWSGI0.1'),
        ]
        self.headers = response_headers + headers
        self.status = status

    def gen_response_data(self, app_data):
        response = 'HTTP/1.1 {status}\r\n'.format(status=self.status)
        for header in self.headers:
            response += '{0}: {1}\r\n'.format(*header)
        response += '\r\n'
        for data in app_data:
            response += data
        return response

def application(environ, start_response):
    status = '200 OK'
    response_headers = [('Content-Type', 'text/plain')]
    start_response(status, response_headers)
    return ['Hello world']

if __name__ == '__main__':
    server = WSGIServer(("0.0.0.0", 8888), application)
    server.serve_forever()