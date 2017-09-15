# -*- coding: utf-8 -*-
# Created by restran on 2017/9/14
from __future__ import unicode_literals, absolute_import

import re
import socket
from optparse import OptionParser
import platform

import tornado.httpclient
import tornado.httpserver
import tornado.ioloop
import tornado.iostream
import tornado.web
from mountains import logging
from mountains.logging import StreamHandler
from mountains.encoding import utf8
from tornado import gen
from tornado import httpserver, ioloop
from tornado.httpclient import AsyncHTTPClient, HTTPRequest, HTTPError
from tornado.httputil import HTTPHeaders
from tornado.web import RequestHandler

parser = OptionParser()

parser.add_option('-l', '--host', help='proxy listen host', default='127.0.0.1')
parser.add_option('-p', '--port', help='proxy listen port', type="int", default=8899)
parser.add_option('-d', '--display_url', help='display url', default=False,
                  action="store_true")
parser.add_option('-v', '--verbose', help='verbose', default=False, action="store_true")

log_format = '[%(asctime)s] %(levelname)s %(message)s'
(options, args) = parser.parse_args()

logging.init_log(StreamHandler(
    logging.INFO, log_format, logging.DATE_FMT_SIMPLE),
    disable_existing_loggers=not options.display_url)
logger = logging.getLogger(__name__)

ASYNC_HTTP_CONNECT_TIMEOUT = 60
ASYNC_HTTP_REQUEST_TIMEOUT = 120

ASYNC_HTTP_CLIENT_MAX_CLIENTS = 100

if platform.system() != 'windows':
    try:
        # curl_httpclient is faster than simple_httpclient
        AsyncHTTPClient.configure(
            'tornado.curl_httpclient.CurlAsyncHTTPClient',
            max_clients=ASYNC_HTTP_CLIENT_MAX_CLIENTS)
    except ImportError:
        AsyncHTTPClient.configure(
            'tornado.simple_httpclient.AsyncHTTPClient')


class ProxyHandler(RequestHandler):
    @gen.coroutine
    def get(self):
        yield self._do_fetch('GET')

    @gen.coroutine
    def post(self):
        yield self._do_fetch('POST')

    @gen.coroutine
    def head(self):
        yield self._do_fetch('HEAD')

    @gen.coroutine
    def options(self):
        yield self._do_fetch('OPTIONS')

    @gen.coroutine
    def put(self):
        yield self._do_fetch('PUT')

    @gen.coroutine
    def delete(self):
        yield self._do_fetch('DELETE')

    def _clean_headers(self):
        """
        清理headers中不需要的部分
        :return:
        """
        headers = self.request.headers
        new_headers = HTTPHeaders()
        # 如果 header 有的是 str，有的是 unicode
        # 会出现 422 错误
        for name, value in headers.get_all():
            if name == 'Content-Length':
                pass
            else:
                new_headers.add(name, value)

        return new_headers

    @gen.coroutine
    def _do_fetch(self, method):
        # 清理和处理一下 header
        headers = self._clean_headers()
        try:
            if method == 'GET':
                body = None
            elif method == 'POST':
                body = self.request.body
            elif method in ['PUT']:
                body = self.request.body
            else:
                # method in ['GET', 'HEAD', 'OPTIONS', 'DELETE']
                # GET 方法 Body 必须为 None，否则会出现异常
                body = None

            # 设置超时时间
            async_http_connect_timeout = ASYNC_HTTP_CONNECT_TIMEOUT
            async_http_request_timeout = ASYNC_HTTP_REQUEST_TIMEOUT

            response = yield AsyncHTTPClient().fetch(
                HTTPRequest(url=self.request.uri,
                            method=method,
                            body=body,
                            headers=headers,
                            decompress_response=True,
                            connect_timeout=async_http_connect_timeout,
                            request_timeout=async_http_request_timeout,
                            follow_redirects=False))
            self._on_proxy(response)
        except HTTPError as x:
            if hasattr(x, 'response') and x.response:
                self._on_proxy(x.response)
            else:
                self.set_status(502)
                self.write('502 Bad Gateway')
        except Exception as e:
            if options.verbose:
                logger.exception(e)

            self.set_status(502)
            self.write('502 Bad Gateway')

    def _on_proxy(self, response):
        try:
            # 如果response.code是非w3c标准的，而是使用了自定义，就必须设置reason，
            # 否则会出现unknown status code的异常
            self.set_status(response.code, response.reason)
        except ValueError:
            self.set_status(response.code, 'Unknown Status Code')

        # 这里要用 get_all 因为要按顺序
        for (k, v) in response.headers.get_all():
            if k == 'Transfer-Encoding' and v.lower() == 'chunked':
                pass
            elif k == 'Content-Length':
                pass
            elif k == 'Content-Encoding':
                pass
            elif k == 'Set-Cookie':
                self.add_header(k, v)
            else:
                self.set_header(k, v)

        if response.code != 304:
            self.write(response.body)

        extract_flag(response)

    @tornado.web.asynchronous
    def connect(self):
        logger.debug('Start CONNECT to %s', self.request.uri)
        host, port = self.request.uri.split(':')
        client = self.request.connection.stream

        def read_from_client(data):
            upstream.write(data)

        def read_from_upstream(data):
            client.write(data)

        def client_close(data=None):
            if upstream.closed():
                return
            if data:
                upstream.write(data)
            upstream.close()

        def upstream_close(data=None):
            if client.closed():
                return
            if data:
                client.write(data)
            client.close()

        def start_tunnel():
            logger.debug('CONNECT tunnel established to %s', self.request.uri)
            client.read_until_close(client_close, read_from_client)
            upstream.read_until_close(upstream_close, read_from_upstream)
            client.write(b'HTTP/1.0 200 Connection established\r\n\r\n')

        def on_proxy_response(data=None):
            if data:
                first_line = data.splitlines()[0]
                http_v, status, text = first_line.split(None, 2)
                if int(status) == 200:
                    start_tunnel()
                    return

            self.set_status(500)
            self.finish()

        def start_proxy_tunnel():
            upstream.write('CONNECT %s HTTP/1.1\r\n' % self.request.uri)
            upstream.write('Host: %s\r\n' % self.request.uri)
            upstream.write('Proxy-Connection: Keep-Alive\r\n\r\n')
            upstream.read_until('\r\n\r\n', on_proxy_response)

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
        upstream = tornado.iostream.IOStream(s)
        upstream.connect((host, int(port)), start_tunnel)


def extract_flag(response):
    try:
        headers = []
        for (k, v) in response.headers.get_all():
            headers.append('%s: %s' % (k, v))

        do_extract_flag('\n'.join(headers))
        if response.body not in (b'', None):
            content_type = response.headers.get('Content-Type', '').lower()
            if 'application' in content_type \
                    or 'plain' in content_type \
                    or 'html' in content_type \
                    or 'text' in content_type \
                    or content_type == '':
                do_extract_flag(response.body)
    except Exception as e:
        logger.exception(e)


def do_extract_flag(data):
    data = utf8(data)
    data = data.replace(b'\n', b'')
    data = data.replace(b'\r', b'')
    data = data.replace(b'\t', b'')
    data = data.replace(b' ', b'')

    # 这里用 ?: 表示不对该分组编号，也不匹配捕获的文本
    # 这样使用 findall 得到的结果就不会只有()里面的东西
    # [\x20-\x7E] 是可见字符
    re_list = [
        # (r'(?:key|flag|ctf)\{[^\{\}]{3,35}\}', re.I),
        # (r'(?:key|KEY|flag|FLAG|ctf|CTF)+[\x20-\x7E]{3,50}', re.I),
        (r'(?:key|flag|ctf)[\x20-\x7E]{5,35}', re.I),
        (r'(?:key|flag|ctf)[\x20-\x7E]{0,3}(?:\:|=|\{|is)[\x20-\x7E]{,35}', re.I)
    ]

    for r, option in re_list:
        # print('Search RegExp: %s' % r)
        # re.I表示大小写无关
        if option is not None:
            pattern = re.compile(r, option)
        else:
            pattern = re.compile(r)
        ret = pattern.findall(data)
        if len(ret):
            ret = [t.strip() for t in ret]
            for t in ret:
                try:
                    logger.warning(t)
                except Exception as e:
                    logger.debug(e)


def main():
    if not options.port:
        parser.print_help()

    app = tornado.web.Application([
        (r'.*', ProxyHandler),
    ])

    logger.info('MagicProxy is running on %s:%s' % (options.host, options.port))
    server = httpserver.HTTPServer(app, xheaders=True)
    server.listen(options.port, options.host)
    ioloop.IOLoop.instance().start()


if __name__ == '__main__':
    main()
