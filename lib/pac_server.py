#!/usr/bin/env python
# coding:utf-8

import os
import base64
import time
import re
import thread
import urllib2
import urlparse
import socket
import simple_http_server

from config import config
from xlog import getLogger
xlog = getLogger("gae_proxy")


default_pac = '''//
function FindProxyForURL(url, host) {
    var autoproxy = 'PROXY 127.0.0.1:8087';
    var blackhole = 'PROXY 127.0.0.1:8086';
    var defaultproxy = 'DIRECT';
    if (isPlainHostName(host) ||
        host.indexOf('127.') == 0 ||
        host.indexOf('192.168.') == 0 ||
        host.indexOf('10.') == 0 ||
        shExpMatch(host, 'localhost.*')) {
        return 'DIRECT';
    } else if (FindProxyForURLByAdblock(url, host) != defaultproxy ||
               host == 'p.tanx.com' ||
               host == 'a.alimama.cn' ||
               host == 'pagead2.googlesyndication.com' ||
               dnsDomainIs(host, '.google-analytics.com') ||
               dnsDomainIs(host, '.2mdn.net') ||
               dnsDomainIs(host, '.doubleclick.net')) {
        return blackhole;
    } else if (shExpMatch(host, '*.google*.*') ||
               dnsDomainIs(host, '.ggpht.com') ||
               dnsDomainIs(host, '.wikipedia.org') ||
               host == 'cdnjs.cloudflare.com' ||
               host == 'wp.me' ||
               host == 'po.st' ||
               host == 'goo.gl') {
        return autoproxy;
    } else {
        return FindProxyForURLByAutoProxy(url, host);
    }
}

// AUTO-GENERATED RULES, DO NOT MODIFY!
'''


user_pacfile = os.path.join(config.DATA_PATH, config.PAC_FILE)

def get_file(filename):
    user_file = os.path.join(config.DATA_PATH, filename)
    if os.path.isfile(user_file):
        return user_file
    return False


def get_opener():
    autoproxy = '127.0.0.1:%s' % config.LISTEN_PORT

    import ssl
    if getattr(ssl, "create_default_context", None):
        cafile = os.path.join(config.DATA_PATH, "CA.crt")
        context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH,
                                             cafile=cafile)
        https_handler = urllib2.HTTPSHandler(context=context)
        opener = urllib2.build_opener(urllib2.ProxyHandler({'http': autoproxy, 'https': autoproxy}), https_handler)
    else:
        opener = urllib2.build_opener(urllib2.ProxyHandler({'http': autoproxy, 'https': autoproxy}))
    return opener


class PacUtil(object):
    """GAEProxy Pac Util"""

    @staticmethod
    def update_pacfile():
        opener = get_opener()

        listen_ip = config.LISTEN_IP
        autoproxy = '%s:%s' % (listen_ip, config.LISTEN_PORT)
        blackhole = '%s:%s' % (listen_ip, config.PAC_PORT)
        default = 'PROXY %s:%s' % (config.PROXY_HOST, config.PROXY_PORT) if config.PROXY_ENABLE else 'DIRECT'

        adblock_content = False
        if config.PAC_ADBLOCK:
            try:
                xlog.info('try download %r to update pacfile', config.PAC_ADBLOCK)
                adblock_content = opener.open(config.PAC_ADBLOCK).read()
            except Exception as e:
                xlog.warn("pac_update download adblock fail:%r", e)
        try:
            xlog.info('try download %r to update pacfile', config.PAC_GFWLIST)
            pac_content = opener.open(config.PAC_GFWLIST).read()
        except Exception as e:
            xlog.warn("pac_update download gfwlist fail:%r", e)
            return

        content = default_pac
        try:
            placeholder = '// AUTO-GENERATED RULES, DO NOT MODIFY!'
            content = content[:content.index(placeholder)+len(placeholder)]
            content = re.sub(r'''blackhole\s*=\s*['"]PROXY [\.\w:]+['"]''', 'blackhole = \'PROXY %s\'' % blackhole, content)
            content = re.sub(r'''autoproxy\s*=\s*['"]PROXY [\.\w:]+['"]''', 'autoproxy = \'PROXY %s\'' % autoproxy, content)
            content = re.sub(r'''defaultproxy\s*=\s*['"](DIRECT|PROXY [\.\w:]+)['"]''', 'defaultproxy = \'%s\'' % default, content)
            content = re.sub(r'''host\s*==\s*['"][\.\w:]+['"]\s*\|\|\s*isPlainHostName''', 'host == \'%s\' || isPlainHostName' % listen_ip, content)
            if content.startswith('//'):
                line = '// Proxy Auto-Config file generated by autoproxy2pac, %s\r\n' % time.strftime('%Y-%m-%d %H:%M:%S')
                content = line + '\r\n'.join(content.splitlines()[1:])
        except ValueError:
            return
        try:
            if adblock_content:
                admode = config.PAC_ADMODE
                xlog.info('%r downloaded, try convert it with adblock2pac', config.PAC_ADBLOCK)
                jsrule = PacUtil.adblock2pac(adblock_content, 'FindProxyForURLByAdblock', blackhole, default, admode)
                content += '\r\n' + jsrule + '\r\n'
                xlog.info('%r downloaded and parsed', config.PAC_ADBLOCK)
            else:
                content += '\r\nfunction FindProxyForURLByAdblock(url, host) {return "DIRECT";}\r\n'
        except Exception as e:
            xlog.exception('update pacfile failed: %r', e)
            return
        try:
            autoproxy_content = base64.b64decode(pac_content)
            xlog.info('%r downloaded, try convert it with autoproxy2pac', config.PAC_GFWLIST)
            jsrule = PacUtil.autoproxy2pac(autoproxy_content, 'FindProxyForURLByAutoProxy', autoproxy, default)
            content += '\r\n' + jsrule + '\r\n'
            xlog.info('%r downloaded and parsed', config.PAC_GFWLIST)
        except Exception as e:
            xlog.exception('update pacfile failed: %r', e)
            return

        open(user_pacfile, 'wb').write(content)
        xlog.info('%r successfully updated', user_pacfile)


    @staticmethod
    def autoproxy2pac(content, func_name='FindProxyForURLByAutoProxy', proxy='127.0.0.1:8087', default='DIRECT', indent=4):
        """Autoproxy to Pac, based on https://github.com/iamamac/autoproxy2pac"""
        direct_domain_set = set([])
        proxy_domain_set = set([])
        for line in content.splitlines()[1:]:
            if line and not line.startswith(('!', '|!', '||!')):
                use_proxy = True
                if line.startswith("@@"):
                    line = line[2:]
                    use_proxy = False
                domain = ''
                try:
                    if line.startswith('/') and line.endswith('/'):
                        line = line[1:-1]
                        if line.startswith('^https?:\\/\\/[^\\/]+') and re.match(r'^(\w|\\\-|\\\.)+$', line[18:]):
                            domain = line[18:].replace(r'\.', '.')
                        else:
                            xlog.warning('unsupport gfwlist regex: %r', line)
                    elif line.startswith('||'):
                        domain = line[2:].lstrip('*').rstrip('/')
                    elif line.startswith('|'):
                        domain = urlparse.urlsplit(line[1:]).hostname.lstrip('*')
                    elif line.startswith(('http://', 'https://')):
                        domain = urlparse.urlsplit(line).hostname.lstrip('*')
                    elif re.search(r'^([\w\-\_\.]+)([\*\/]|$)', line):
                        domain = re.split(r'[\*\/]', line)[0]
                    else:
                        pass
                except Exception as e:
                    xlog.warning('error when process gfwlist rule: %r %s', line, e)
                if '*' in domain:
                    domain = domain.split('*')[-1]
                if not domain or re.match(r'^\w+$', domain):
                    xlog.debug('unsupport gfwlist rule: %r', line)
                    continue
                if use_proxy:
                    proxy_domain_set.add(domain)
                else:
                    direct_domain_set.add(domain)
        proxy_domain_list = sorted(set(x.lstrip('.') for x in proxy_domain_set))
        autoproxy_host = ',\r\n'.join('%s"%s": 1' % (' '*indent, x) for x in proxy_domain_list)
        template = '''\
                    var autoproxy_host = {
                    %(autoproxy_host)s
                    };
                    function %(func_name)s(url, host) {
                        var lastPos;
                        do {
                            if (autoproxy_host.hasOwnProperty(host)) {
                                return 'PROXY %(proxy)s';
                            }
                            lastPos = host.indexOf('.') + 1;
                            host = host.slice(lastPos);
                        } while (lastPos >= 1);
                        return '%(default)s';
                    }'''
        template = re.sub(r'(?m)^\s{%d}' % min(len(re.search(r' +', x).group()) for x in template.splitlines()), '', template)
        template_args = {'autoproxy_host': autoproxy_host,
                         'func_name': func_name,
                         'proxy': proxy,
                         'default': default}
        return template % template_args


    @staticmethod
    def adblock2pac(content, func_name='FindProxyForURLByAdblock', proxy='127.0.0.1:8086', default='DIRECT', admode=1, indent=4):
        """adblock list to Pac, based on https://github.com/iamamac/autoproxy2pac"""
        white_conditions = {'host': [], 'url.indexOf': [], 'shExpMatch': []}
        black_conditions = {'host': [], 'url.indexOf': [], 'shExpMatch': []}
        for line in content.splitlines()[1:]:
            if not line or line.startswith('!') or '##' in line or '#@#' in line:
                continue
            use_proxy = True
            use_start = False
            use_end = False
            use_domain = False
            use_postfix = []
            if '$' in line:
                posfixs = line.split('$')[-1].split(',')
                if any('domain' in x for x in posfixs):
                    continue
                if 'image' in posfixs:
                    use_postfix += ['.jpg', '.gif']
                elif 'script' in posfixs:
                    use_postfix += ['.js']
                else:
                    continue
            line = line.split('$')[0]
            if line.startswith("@@"):
                line = line[2:]
                use_proxy = False
            if '||' == line[:2]:
                line = line[2:]
                if '/' not in line:
                    use_domain = True
                else:
                    use_start = True
            elif '|' == line[0]:
                line = line[1:]
                use_start = True
            if line[-1] in ('^', '|'):
                line = line[:-1]
                if not use_postfix:
                    use_end = True
            line = line.replace('^', '*').strip('*')
            conditions = black_conditions if use_proxy else white_conditions
            if use_start and use_end:
                conditions['shExpMatch'] += ['*%s*' % line]
            elif use_start:
                if '*' in line:
                    if use_postfix:
                        conditions['shExpMatch'] += ['*%s*%s' % (line, x) for x in use_postfix]
                    else:
                        conditions['shExpMatch'] += ['*%s*' % line]
                else:
                    conditions['url.indexOf'] += [line]
            elif use_domain and use_end:
                if '*' in line:
                    conditions['shExpMatch'] += ['%s*' % line]
                else:
                    conditions['host'] += [line]
            elif use_domain:
                if line.split('/')[0].count('.') <= 1:
                    if use_postfix:
                        conditions['shExpMatch'] += ['*.%s*%s' % (line, x) for x in use_postfix]
                    else:
                        conditions['shExpMatch'] += ['*.%s*' % line]
                else:
                    if '*' in line:
                        if use_postfix:
                            conditions['shExpMatch'] += ['*%s*%s' % (line, x) for x in use_postfix]
                        else:
                            conditions['shExpMatch'] += ['*%s*' % line]
                    else:
                        if use_postfix:
                            conditions['shExpMatch'] += ['*%s*%s' % (line, x) for x in use_postfix]
                        else:
                            conditions['url.indexOf'] += ['http://%s' % line]
            else:
                if use_postfix:
                    conditions['shExpMatch'] += ['*%s*%s' % (line, x) for x in use_postfix]
                else:
                    conditions['shExpMatch'] += ['*%s*' % line]
        templates = ['''\
                    function %(func_name)s(url, host) {
                        return '%(default)s';
                    }''',
                    '''\
                    var blackhole_host = {
                    %(blackhole_host)s
                    };
                    function %(func_name)s(url, host) {
                        // untrusted ablock plus list, disable whitelist until chinalist come back.
                        if (blackhole_host.hasOwnProperty(host)) {
                            return 'PROXY %(proxy)s';
                        }
                        return '%(default)s';
                    }''',
                    '''\
                    var blackhole_host = {
                    %(blackhole_host)s
                    };
                    var blackhole_url_indexOf = [
                    %(blackhole_url_indexOf)s
                    ];
                    function %s(url, host) {
                        // untrusted ablock plus list, disable whitelist until chinalist come back.
                        if (blackhole_host.hasOwnProperty(host)) {
                            return 'PROXY %(proxy)s';
                        }
                        for (i = 0; i < blackhole_url_indexOf.length; i++) {
                            if (url.indexOf(blackhole_url_indexOf[i]) >= 0) {
                                return 'PROXY %(proxy)s';
                            }
                        }
                        return '%(default)s';
                    }''',
                    '''\
                    var blackhole_host = {
                    %(blackhole_host)s
                    };
                    var blackhole_url_indexOf = [
                    %(blackhole_url_indexOf)s
                    ];
                    var blackhole_shExpMatch = [
                    %(blackhole_shExpMatch)s
                    ];
                    function %(func_name)s(url, host) {
                        // untrusted ablock plus list, disable whitelist until chinalist come back.
                        if (blackhole_host.hasOwnProperty(host)) {
                            return 'PROXY %(proxy)s';
                        }
                        for (i = 0; i < blackhole_url_indexOf.length; i++) {
                            if (url.indexOf(blackhole_url_indexOf[i]) >= 0) {
                                return 'PROXY %(proxy)s';
                            }
                        }
                        for (i = 0; i < blackhole_shExpMatch.length; i++) {
                            if (shExpMatch(url, blackhole_shExpMatch[i])) {
                                return 'PROXY %(proxy)s';
                            }
                        }
                        return '%(default)s';
                    }''']
        template = re.sub(r'(?m)^\s{%d}' % min(len(re.search(r' +', x).group()) for x in templates[admode].splitlines()), '', templates[admode])
        template_kwargs = {'blackhole_host': ',\r\n'.join("%s'%s': 1" % (' '*indent, x) for x in sorted(black_conditions['host'])),
                           'blackhole_url_indexOf': ',\r\n'.join("%s'%s'" % (' '*indent, x) for x in sorted(black_conditions['url.indexOf'])),
                           'blackhole_shExpMatch': ',\r\n'.join("%s'%s'" % (' '*indent, x) for x in sorted(black_conditions['shExpMatch'])),
                           'func_name': func_name,
                           'proxy': proxy,
                           'default': default}
        return template % template_kwargs


class PACServerHandler(simple_http_server.HttpServerHandler):
    def address_string(self):
        return '%s:%s' % self.client_address[:2]

    def do_CONNECT(self):
        self.wfile.write(b'HTTP/1.1 403\r\nConnection: close\r\n\r\n')

    def do_GET(self):
        xlog.info('PAC from:%s %s %s ', self.address_string(), self.command, self.path)

        path = urlparse.urlparse(self.path).path # '/proxy.pac'
        filename = os.path.normpath('./' + path) # proxy.pac

        if filename == config.PAC_FILE:
            mimetype = 'text/plain'
            pac_filename = get_file(filename)
            outdate_time = time.time() - os.path.getmtime(pac_filename) if pac_filename else 99999999
            if self.path.endswith('.pac?flush') or outdate_time > config.PAC_EXPIRED:
                thread.start_new_thread(PacUtil.update_pacfile, ())
            if pac_filename:
                data = open(pac_filename, 'rb').read()
            else:
                return
            host = self.headers.getheader('Host')
            host, _, port = host.rpartition(":")
            data = data.replace('127.0.0.1:8087', host + ":" + str(config.LISTEN_PORT))
            data = data.replace('127.0.0.1:8086', host + ":" + str(config.PAC_PORT))
            self.wfile.write(('HTTP/1.1 200\r\nContent-Type: %s\r\nContent-Length: %s\r\n\r\n' % (mimetype, len(data))).encode())
            self.wfile.write(data)
        elif filename == 'CA.crt':
            mimetype = 'application/octet-stream'
            cer_filename = get_file(filename)
            if cer_filename:
                data = open(cer_filename, 'rb').read()
            else:
                return
            self.wfile.write(('HTTP/1.1 200\r\nContent-Type: %s\r\nContent-Length: %s\r\n\r\n' % (mimetype, len(data))).encode())
            self.wfile.write(data)
        else:
            xlog.warn("pac_server GET %s fail", filename)
            self.wfile.write(b'HTTP/1.1 404\r\n\r\n')
            return


class ProxyUtil(object):
    """ProxyUtil module, based on urllib2"""

    @staticmethod
    def get_listen_ip():
        listen_ip = '127.0.0.1'
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect(('8.8.8.8', 53))
            listen_ip = sock.getsockname()[0]
        except StandardError:
            pass
        finally:
            if sock:
                sock.close()
        return listen_ip
