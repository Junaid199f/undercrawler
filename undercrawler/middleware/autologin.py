from copy import deepcopy
from functools import partial
import json
from http.cookies import SimpleCookie
import logging
from urllib.parse import urljoin

import scrapy
from scrapy.exceptions import IgnoreRequest, NotConfigured


logger = logging.getLogger(__name__)


class AutologinMiddleware:
    '''
    Autologin middleware uses autologin to make all requests while being
    logged in. It uses autologin to get cookies, detects logouts and tries
    to avoid them in the future. A single authorization domain for the spider
    is assumed. Middleware also puts "autologin_active" into request.meta,
    which is True only if we are logged in (and False if domain is skipped
    or login failed).

    Required settings:
    AUTOLOGIN_ENABLED = True
    AUTOLOGIN_URL: url of where the autologin service is running
    COOKIES_ENABLED = False (this could be relaxed perhaps)

    Optional settings:
    AUTH_COOKIES: pass auth cookies after manual login (format is_logout
    "name=value; name2=value2")
    LOGOUT_URL: pass url substring to avoid
    USERNAME, PASSWORD, LOGIN_URL are passed to autologin and
    override values from stored credentials.  LOGIN_URL is a relative url.
    It can be omitted if it is the same as the start url.
    '''
    def __init__(self, autologin_url, crawler):
        self.crawler = crawler
        s = crawler.settings
        self.autologin_url = autologin_url
        self.splash_url = s.get('SPLASH_URL')
        self.login_url = s.get('LOGIN_URL')
        self.username = s.get('USERNAME')
        self.password = s.get('PASSWORD')
        self.user_agent = s.get('USER_AGENT')
        self.autologin_download_delay = s.get('AUTOLOGIN_DOWNLOAD_DELAY')
        self.logout_url = s.get('LOGOUT_URL')
        self._force_skip = s.get('_AUTOLOGIN_FORCE_SKIP')
        self._queue = []
        self.waiting_for_login = False
        auth_cookies = s.get('AUTH_COOKIES')
        self.skipped = False
        if auth_cookies:
            cookies = SimpleCookie()
            cookies.load(auth_cookies)
            self.auth_cookies = [
                {'name': m.key, 'value': m.value} for m in cookies.values()]
            self.logged_in = True
        else:
            self.auth_cookies = None
            self.logged_in = False

    @classmethod
    def from_crawler(cls, crawler):
        if not crawler.settings.getbool('AUTOLOGIN_ENABLED'):
            raise NotConfigured
        return cls(crawler.settings['AUTOLOGIN_URL'], crawler)

    def process_request(self, request, spider):
        ''' Login if we are not logged in yet.
        '''
        if '_autologin' in request.meta or request.meta.get('skip_autologin'):
            return
        if self.skipped:
            request.meta['autologin_active'] = False
            return
        elif self.logged_in:
            request.meta['autologin_active'] = True
            if self.logout_url and self.logout_url in request.url:
                logger.debug('Ignoring logout request %s', request.url)
                raise IgnoreRequest
            # Save original request to be able to retry it in case of logout
            req_copy = request.replace(meta=deepcopy(request.meta))
            req_copy.callback = req_copy.errback = None
            request.meta['_autologin'] = autologin_meta = {'request': req_copy}
            # TODO - it should be possible to put auth cookies into the
            # cookiejar in process_response (but also check non-splash)
            if self.auth_cookies:
                request.cookies = self.auth_cookies
                autologin_meta['cookie_dict'] = {
                    c['name']: c['value'] for c in self.auth_cookies}
        else:
            self._enqueue(request)
            if self.waiting_for_login:
                raise IgnoreRequest
            else:
                return self._login_request(request.url, spider)

    def _on_login_response(self, url, response, spider):
        self.waiting_for_login = False
        response_data = json.loads(response.text)
        status = response_data['status']
        if self._force_skip:
            status = 'skipped'
        logger.debug('Got login response with status "%s"', status)
        if status == 'pending':
            self.crawler.engine.crawl(self._login_request(url, spider), spider)
            return
        elif status in {'skipped', 'error'}:
            self.auth_cookies = None
            self.skipped = True
            if status == 'error':
                logger.error("Can't login; crawl will continue without auth.")
        elif status == 'solved':
            cookies = response_data.get('cookies')
            if cookies:
                cookies = _cookies_to_har(cookies)
                logger.debug('Got cookies after login %s', cookies)
                self.auth_cookies = cookies
                self.logged_in = True
            else:
                logger.error('No cookies after login')
                self.auth_cookies = None
                self.skipped = True
        self._process_queue(spider)

    def _enqueue(self, request):
        self._queue.append(request)

    def _process_queue(self, spider):
        for request in self._queue:
            request.dont_filter = True
            self.crawler.engine.crawl(request, spider)
        self._queue[:] = []

    def _login_request(self, url, spider):
        self.waiting_for_login = True
        logger.debug('Attempting login at %s', url)
        autologin_endpoint = urljoin(self.autologin_url, '/login-cookies')
        params = {
            'url': urljoin(url, self.login_url) if self.login_url else url,
            'username': self.username,
            'password': self.password,
            'splash_url': self.splash_url,
            'settings': {
                'ROBOTSTXT_OBEY': False,
            }
        }
        if self.user_agent:
            params['settings']['USER_AGENT'] = self.user_agent
        if self.autologin_download_delay:
            params['settings']['DOWNLOAD_DELAY'] = \
                self.autologin_download_delay
        return scrapy.Request(
            autologin_endpoint, method='POST',
            body=json.dumps(params).encode(),
            headers={'content-type': 'application/json'},
            callback=partial(self._on_login_response, url, spider=spider),
            dont_filter=True,
            meta={'skip_autologin': True},
            priority=1000)

    def process_response(self, request, response, spider):
        ''' If we were logged out, login again and retry request.
        '''
        if self.is_logout(response):
            logger.debug('Logout at %s %s',
                         response.url, response.cookiejar)
            autologin_meta = request.meta['_autologin']
            retryreq = autologin_meta['request'].copy()
            retryreq.dont_filter = True
            # We could have already done relogin after initial logout
            if any(autologin_meta['cookie_dict'].get(c['name']) != c['value']
                    for c in self.auth_cookies):
                logger.debug('Request %s was stale, will retry %s',
                             response, retryreq)
                return retryreq
            if self.waiting_for_login:
                logger.debug('Enqueue %s', retryreq)
                self._enqueue(retryreq)
                raise IgnoreRequest
            else:
                # This request will not be retried
                logger.debug('Logged out at %s, will re-login', response)
                return self._login_request(response.url, spider)
        return response

    def is_logout(self, response):
        if self.auth_cookies and \
                getattr(response, 'cookiejar', None) is not None:
            auth_cookies = {c['name'] for c in self.auth_cookies if c['value']}
            response_cookies = {m.name for m in response.cookiejar if m.value}
            return bool(auth_cookies - response_cookies)


def _cookies_to_har(cookies):
    # Leave only documented cookie attributes
    return [{
        'name': c['name'],
        'value': c['value'],
        'path': c.get('path', '/'),
        'domain': c.get('domain', ''),
        } for c in cookies]
