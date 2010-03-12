"""Crawler worker.

Gets URLs to crawl from queue server, crawls them, store and send crawl info back to queue server."""

from datetime import datetime
import eventlet
from eventlet import GreenPool, greenthread, sleep, spawn
from eventlet.queue import Empty, Queue
import httplib2
import random, socket, sys, time, urlparse
import robotparser

from heroshi.data import Link, Page, PoolMap
from heroshi.conf import settings
from heroshi.error import ApiError, CrawlError, FetchError
from heroshi import TIME_FORMAT
from heroshi import api, dns, error, misc
log = misc.get_logger("worker.Crawler")

eventlet.monkey_patch(all=False, socket=True, select=True)


class Crawler(object):
    def __init__(self, queue_size, max_connections):
        self.max_queue_size = queue_size
        self.max_connections = max_connections
        self.max_connections_per_host = 5

        self.queue = Queue(self.max_queue_size)
        self.closed = False
        self._handler_pool = GreenPool(self.max_connections)
        self._connections = PoolMap(httplib2.Http,
                                    pool_max_size=self.max_connections_per_host,
                                    timeout=120)
        self._robots_cache = PoolMap(self.get_robots_checker,
                                     pool_max_size=1,
                                     timeout=600)
        self.resolver = dns.CachingResolver()

        log.debug(u"Crawler started. Max queue size: %d, connections: %d.",
                  self.max_queue_size, self.max_connections)

    def crawl(self, forever=True):
        # TODO: do something special about signals?

        crawler_thread = greenthread.getcurrent()
        def _exc_link(gt):
            try:
                gt.wait()
            except Exception: # pylint: disable-msg=W0703
                crawler_thread.throw(*sys.exc_info())

        def qputter():
            while True:
                if self.queue.qsize() < self.max_queue_size:
                    self.do_queue_get()
                    sleep()
                else:
                    sleep(settings.full_queue_pause)

        if forever:
            spawn(qputter).link(_exc_link)

        while not self.closed:
            # `get_nowait` will only work together with sleep(0) here
            # because we need greenlet switch to reraise exception from `do_process`.
            sleep()
            try:
                item = self.queue.get_nowait()
            except Empty:
                if not forever:
                    self.graceful_stop()
                sleep(0.01)
                continue
            self._handler_pool.spawn(self.do_process, item).link(_exc_link)

    def stop(self):
        self.closed = True

    def graceful_stop(self, timeout=None):
        """Stops crawler and waits for all already started crawling requests to finish.

        If `timeout` is supplied, it waits for at most `timeout` time to finish
            and returns True if allocated time was enough.
            Returns False if `timeout` was not enough.
        """
        self.closed = True
        if timeout is not None:
            with eventlet.Timeout(timeout, False):
                self._handler_pool.waitall()
                return True
            return False
        else:
            self._handler_pool.waitall()

    def get_active_connections_count(self, key):
        pool = self._connections._pools.get(key)
        if pool is None:
            return 0
        return pool.max_size - pool.free()

    def do_queue_get(self):
        log.debug(u"It's queue update time!")
        num = self.max_queue_size - self.queue.qsize()
        log.debug(u"  getting %d items from URL server.", num)
        try:
            new_queue = api.get_crawl_queue(num)
            log.debug(u"  got %d items", len(new_queue))

            if len(new_queue) == 0:
                log.debug(u"  waiting some time before another request to URL server.")
                sleep(10.0)

            # extend worker queue
            # 1. skip duplicate URLs
            for new_item in new_queue:
                for queue_item in self.queue.queue:
                    if queue_item['url'] == new_item['url']: # compare URLs
                        break
                else:
                    # 2. extend queue with new items
                    self.queue.put(new_item)

            # shuffle the queue so there are no long sequences of URIs on same domain
            random.shuffle(self.queue.queue)
        except ApiError:
            log.exception(u"do_queue_get")
            self.stop()

    def report_item(self, item):
        import cPickle
        pickled = cPickle.dumps(item)
        log.debug(u"Reporting %s results back to URL server. Size ~= %d KB. Connections cache: %r.",
                  unicode(item['url']),
                  len(pickled) / 1024,
                  self._connections)
        try:
            api.report_result(item)
        except ApiError:
            log.exception(u"report_item")

    def fetch(self, uri, _redirect_counter=0):
        # FIXME: magic number
        if _redirect_counter >= 7:
            return {'result': u"Too many redirects."}
        log.debug(u"  fetching: %s.", uri)

        result = {}

        parsed = urlparse.urlsplit(uri)
        addr = self.resolver.gethostbyname(parsed.hostname)
        conn_key = addr
        request_uri = uri.replace(parsed.hostname, addr)
        request_headers = {'user-agent': settings.identity['user_agent'],
                           'host': parsed.hostname}
        with self._connections.getc(conn_key, timeout=settings.socket_timeout) as conn:
            conn.follow_redirects = False
            try:
                response, content = conn.request(request_uri, headers=request_headers)
            except (AssertionError, KeyboardInterrupt, error.ConfigurationError):
                raise
            except socket.timeout:
                log.info(u"Socket timeout at %s", uri)
                result['result'] = u"Socket timeout"
            except Exception, e:
                log.warning(u"HTTP error at %s: %s", uri, str(e))
                result['result'] = u"HTTP Error: " + unicode(e)
            else:
                result['result'] = u"OK"
                result['status_code'] = response.status
                # TODO: update tests for this to work
                result['headers'] = dict(response)
                result['content'] = content

        if result.get('result') == u"OK" and response.status in (301, 302):
            new_location = response.get('location')
            if new_location is not None:
                result = self.fetch(new_location, _redirect_counter=_redirect_counter+1)
                result.setdefault('redirects', []).append( (response.status, new_location) )
                return result
            else:
                result['result'] = u"HTTP Error: redirect w/o location"

        return result

    def get_robots_checker(self, scheme, authority):
        """PoolMap func :: scheme, authority -> (agent, uri -> bool)."""
        robots_uri = "%s://%s/robots.txt" % (scheme, authority)

        fetch_result = self.fetch(robots_uri)
        if fetch_result['result'] == u"OK":
            # TODO: set expiration time from headers
            # but this must be done after `self._robots_cache.put` or somehow else...
            if 200 <= fetch_result['status_code'] < 300:
                parser = robotparser.RobotFileParser()
                parser.parse(fetch_result['content'].splitlines())
                return parser.can_fetch
            # Authorization required and Forbidden are considered Disallow all.
            elif fetch_result['status_code'] in (401, 403):
                return lambda _agent, _uri: False
            # /robots.txt Not Found is considered Allow all.
            elif fetch_result['status_code'] == 404:
                return lambda _agent, _uri: True
            # FIXME: this is an optimistic rule and probably should be detailed with more specific checks
            elif fetch_result['status_code'] >= 400:
                return lambda _agent, _uri: True
            # What other cases left? 100 and redirects. Consider it Disallow all.
            else:
                return lambda _agent, _uri: False
        else:
            raise FetchError(u"/robots.txt fetch problem: %s" % (fetch_result['result']))

    def ask_robots(self, uri, scheme, authority):
        key = scheme+":"+authority
        with self._robots_cache.getc(key, scheme, authority) as checker:
            return checker(settings.identity['name'], uri)

    def do_process(self, item):
        report = self._process(item)
        self.report_item(report)

    def _process(self, item):
        url = item['url']
        log.debug(u"Crawling: %s", url)
        uri = httplib2.iri2uri(url)
        report = {
            'url': url,
            'result': None,
            'status_code': None,
            'visited': None,
        }

        (scheme, authority, _path, _query, _fragment) = httplib2.parse_uri(uri)
        if scheme is None or authority is None:
            report['result'] = u"Invalid URI"
        else:
            try:
                robot_check_result = self.ask_robots(uri, scheme, authority)
            except FetchError, e:
                report['result'] = unicode(e)
                return report
            if robot_check_result == True:
                pass
            elif robot_check_result == False:
                report['result'] = u"Deny by robots.txt"
                return report
            else:
                assert False, u"This branch should not be executed."

            fetch_start_time = time.time()
            fetch_result = self.fetch(uri)
            fetch_end_time = time.time()
            report['fetch_time'] = fetch_end_time - fetch_start_time
            report.update(fetch_result)

        if report['status_code'] == 200:
            page = Page(Link(uri), report['content'])
            try:
                page.parse()
            except (AssertionError, KeyboardInterrupt, error.ConfigurationError):
                raise
            except Exception, e:
                report['result'] = u"Parse Error: " + unicode(e)
            else:
                report['links'] = [ link.full for link in page.links ]

        timestamp = datetime.now().strftime(TIME_FORMAT)
        report['visited'] = timestamp
        return report