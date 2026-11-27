#!/usr/bin/python

import fcntl
import http.client
import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from ansible.module_utils.basic import AnsibleModule


ROLE_MARKER = 'Role: sunfoxcz.server-status'
LEGACY_MARKER = 'Ansible: nginx-php-fpm-status'
LISTEN_OWNER_MARKER = '; Ansible Managed: sunfoxcz.server-status'

# Keep quoted strings, escaped characters and ${variables} intact when parsing directives.
NGINX_TOKEN = re.compile(
    r'''\#[^\r\n]*|[{};]|(?:"(?:\\[\s\S]|[^"\\])*"|'''
    r"'(?:\\[\s\S]|[^'\\])*'|\$\{[^}]*\}|\\[\s\S]|[^\s{};#'\"\\])+"
)
POOL_SECTION = re.compile(r'^\s*\[([^\[\]]+)\]\s*(?:[;#].*)?$')
ASSIGNMENT = re.compile(r'^[ \t]*([^;#=\s][^=]*?)\s*=\s*(.*)$')
IDLE_TIMEOUT = re.compile(r'^([ \t]*)(?:;[ \t]*)?pm\.process_idle_timeout\s*=')
LISTEN_OWNERSHIP = {'listen.owner', 'listen.acl_users', 'listen.acl_groups'}


def nginx_directives(text):
    args = []
    for match in NGINX_TOKEN.finditer(text):
        token = match.group()
        if token.startswith('#'):
            continue
        if token in ('{', '}', ';'):
            yield args, token
            args = []
        else:
            args.append(token.strip('\"\''))


def read_text(path):
    return path.read_bytes().decode('utf-8', 'surrogateescape')


class ConfigFile:
    def __init__(self, path, mode=None):
        self.path = path.resolve()
        self.metadata = self.path.stat() if self.path.exists() else None
        if self.metadata and not stat.S_ISREG(self.metadata.st_mode):
            raise ValueError('{} is not a regular file'.format(path))
        self.original = read_text(self.path) if self.metadata else None
        self.content = self.original
        self.mode = mode

    @property
    def changed(self):
        return self.original != self.content or (
            self.mode is not None and self.metadata is not None
            and stat.S_IMODE(self.metadata.st_mode) != self.mode
        )

    def write(self, content, metadata=None, mode=None):
        # The temporary file must also be outside the *.conf include globs.
        with tempfile.NamedTemporaryFile(dir=str(self.path.parent), prefix='.status-', delete=False) as temporary:
            temp_path = Path(temporary.name)
            try:
                temporary.write(content.encode('utf-8', 'surrogateescape'))
                temporary.flush()
                if metadata:
                    os.fchown(temporary.fileno(), metadata.st_uid, metadata.st_gid)
                    if self.path.exists():
                        shutil.copystat(str(self.path), str(temp_path))
                if mode is None:
                    mode = stat.S_IMODE(metadata.st_mode) if metadata else 0o644
                os.fchmod(temporary.fileno(), mode)
                os.fsync(temporary.fileno())
                temp_path.replace(self.path)
            finally:
                if temp_path.exists():
                    temp_path.unlink()

    def apply(self):
        current = read_text(self.path) if self.path.exists() else None
        if current != self.original:
            raise ValueError('{} changed while the playbook was running'.format(self.path))
        if self.content is None:
            self.path.unlink()
        else:
            self.write(self.content, self.metadata, self.mode)

    def restore(self):
        current = read_text(self.path) if self.path.exists() else None
        if current != self.content:
            raise ValueError('{} changed before it could be restored'.format(self.path))
        if self.original is None:
            self.path.unlink()
        else:
            self.write(self.original, self.metadata)


def shared_fastcgi_address(version, pool, settings):
    listen = settings.get('listen', '').replace('$pool', pool)
    if listen and ':' not in listen and not re.fullmatch(r'[0-9]+', listen):
        prefix = settings.get('prefix', '').replace('$pool', pool)
        if not listen.startswith('/') and prefix.startswith('/'):
            listen = prefix.rstrip('/') + '/' + listen
        if re.fullmatch(r'/[^$\x00-\x1f\x7f]+', listen):
            return 'unix:' + listen

    host, _, port = listen.rpartition(':')
    if re.fullmatch(r'[0-9]{1,5}', port) and 1 <= int(port) <= 65535:
        if host in ('', '0.0.0.0'):
            return '127.0.0.1:{}'.format(port)
        if re.fullmatch(r'[A-Za-z0-9_.-]+', host):
            return '{}:{}'.format(host, port)
        if host.startswith('[') and host.endswith(']'):
            host = host[1:-1]
        if ':' in host and '%' not in host:
            try:
                address = ipaddress.IPv6Address(host)
            except ValueError:
                pass
            else:
                return '[{}]:{}'.format('::1' if address.is_unspecified else address, port)

    raise ValueError('Cannot use the existing listen address for PHP {} pool {}: {!r}'.format(
        version, pool, settings.get('listen', '')
    ))


def pool_status(version, pool, options, settings):
    if not re.fullmatch(r'[A-Za-z0-9_~.-]+', pool) or pool in ('.', '..'):
        raise ValueError('PHP {} pool name cannot be used in a status URI: {!r}'.format(version, pool))
    uri = '{}/{}/{}'.format(options['pool_uri_prefix'].rstrip('/'), version, pool)
    endpoint = {
        'name': pool,
        'version': version,
        'uri': uri,
    }
    if int(version.split('.')[0]) < 8:
        endpoint['fastcgi_address'] = shared_fastcgi_address(version, pool, settings)
    else:
        socket = '{}/php{}-{}-status.sock'.format(options['socket_dir'].rstrip('/'), version, pool)
        if len(socket.encode()) >= 108:
            raise ValueError('PHP-FPM status socket path is too long: {}'.format(socket))
        endpoint.update(socket=socket, fastcgi_address='unix:' + socket)
    return endpoint


def configure_pool(lines, version, pool, options):
    settings = {}
    kept = [lines[0]]
    idle = None
    active_idle = None
    for previous, line in zip(lines, lines[1:]):
        if line.strip() == LISTEN_OWNER_MARKER:
            continue
        setting = ASSIGNMENT.match(line.rstrip('\r\n'))
        if setting:
            key, value = setting.groups()
            key = key.lower()
            if key in ('pm.status_path', 'pm.status_listen'):
                continue
            if key == 'listen.owner' and (
                previous.strip() == LISTEN_OWNER_MARKER
                or line.rstrip().endswith(('; ' + ROLE_MARKER, '; ' + LEGACY_MARKER))
            ):
                continue
            settings[key] = value.split(';', 1)[0].strip().strip('\"\'')
        anchor = IDLE_TIMEOUT.match(line)
        kept.append(line)
        if anchor:
            idle = (len(kept), anchor.group(1))
            if setting:
                active_idle = idle
    if not settings:
        return lines, None

    endpoint = pool_status(version, pool, options, settings)
    newline = '\r\n' if lines[0].endswith('\r\n') else '\n'
    anchor = active_idle or idle
    if anchor is None:
        position = len(kept)
        while position > 1 and not kept[position - 1].strip():
            position -= 1
        indent = ''
    else:
        position, indent = anchor
    if not kept[position - 1].endswith(('\r', '\n')):
        kept[position - 1] += newline
    directives = ['pm.status_path = ' + endpoint['uri']]
    if 'socket' in endpoint:
        directives.append('pm.status_listen = ' + endpoint['socket'])
        # TCP pools can set a status socket owner without changing the application listener.
        listen = settings.get('listen', '')
        if re.fullmatch(r'(?:[^/]*:)?[0-9]+', listen) and not LISTEN_OWNERSHIP.intersection(settings):
            directives.extend([LISTEN_OWNER_MARKER, 'listen.owner = ' + options['worker_user']])
    kept[position:position] = [indent + directive + newline for directive in directives]
    return kept, endpoint


def configure_pool_file(text, version, options):
    lines = text.splitlines(keepends=True)
    sections = []
    for index, line in enumerate(lines):
        header = POOL_SECTION.fullmatch(line.rstrip('\r\n'))
        if header:
            sections.append((index, header.group(1).strip()))
    sections.append((len(lines), None))
    result = lines[:sections[0][0]]
    pools = []
    for (start, pool), (end, _) in zip(sections, sections[1:]):
        section = lines[start:end]
        if pool.lower() != 'global':
            section, endpoint = configure_pool(section, version, pool, options)
            if endpoint:
                pools.append(endpoint)
        result.extend(section)
    return ''.join(result), pools


def zabbix_status_config(text, key, userparameter):
    existing = re.compile(r'^\s*UserParameter\s*=\s*' + re.escape(key) + r'\s*,')
    line = userparameter + '\n'
    lines = []
    added = False
    for old in text.splitlines(keepends=True):
        if not existing.match(old):
            lines.append(old)
        elif not added:
            lines.append(line)
            added = True
    if not added:
        if lines and not lines[-1].endswith('\n'):
            lines.append('\n')
        lines.append(line)
    return ''.join(lines)


def nginx_worker_user(options):
    user = options['worker_user']
    if user is None:
        for args, delimiter in nginx_directives(read_text(Path(options['nginx_main_config']))):
            if delimiter == '{':
                break
            if delimiter == ';' and len(args) >= 2 and args[0] == 'user':
                user = args[1]
                break
    if user is None:
        stdout, stderr = command([options['nginx_binary'], '-V'])
        option = re.search(r"(?:^|\s)--user=['\"]?([A-Za-z0-9_.-]+)", stdout + stderr)
        user = option.group(1) if option else 'nobody'
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', user):
        raise ValueError('Invalid nginx worker user: {!r}'.format(user))
    return user


def discover_php(options):
    options = dict(options, worker_user=nginx_worker_user(options))
    php_dir = Path(options['php_dir'])
    php_bin_dir = Path(options['php_bin_dir'])
    binary_prefix = options['php_binary_prefix']
    binary_pattern = re.compile(re.escape(binary_prefix) + r'([0-9]+\.[0-9]+)')
    versions, configs, pools = {}, {}, {}
    for binary in sorted(php_bin_dir.glob(binary_prefix + '*')):
        match = binary_pattern.fullmatch(binary.name)
        if not match or not os.access(str(binary), os.X_OK):
            continue
        version = match.group(1)
        fpm_dir = php_dir / version / 'fpm'
        versions[version] = (binary, fpm_dir / 'php-fpm.conf')
        for path in sorted(fpm_dir.glob('pool.d/*.conf')):
            if not path.is_file():
                continue
            config = ConfigFile(path)
            if config.path in configs:
                raise ValueError('Pool configuration included multiple times: {}'.format(path))
            config.content, endpoints = configure_pool_file(config.original, version, options)
            configs[config.path] = config
            for endpoint in endpoints:
                name = endpoint['name']
                key = (version, name.lower())
                if key in pools:
                    raise ValueError('Multiple sections for PHP {} pool {}'.format(version, name))
                pools[key] = endpoint
    return versions, list(configs.values()), [pools[key] for key in sorted(pools)]


def is_managed_location(content):
    return content is not None and (
        content.startswith('# ' + LEGACY_MARKER + '\n') or content.endswith('\n# ' + ROLE_MARKER + '\n')
    )


def nginx_status_files(options):
    default_vhost = Path(options['nginx_default_vhost'])
    locations_dir = Path(options['nginx_locations_dir'])
    default = ConfigFile(default_vhost)
    default.content = options['nginx_config'].rstrip('\n') + '\n'
    files = {default_vhost: default}
    for filename, location in options['nginx_locations'].items():
        if not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_~.-]*\.conf', filename):
            raise ValueError('Invalid nginx location filename: {!r}'.format(filename))
        if not isinstance(location, str):
            raise ValueError('Expected nginx configuration text for {}'.format(filename))
        path = locations_dir / filename
        if path.is_symlink():
            raise ValueError('Refusing to replace nginx location symlink {}'.format(path))
        config = ConfigFile(path)
        if config.original is not None and not is_managed_location(config.original):
            raise ValueError('Refusing to overwrite unmanaged nginx location {}'.format(path))
        config.content = location.rstrip('\n') + '\n# ' + ROLE_MARKER + '\n'
        files[path] = config

    for path in sorted(locations_dir.glob('*.conf')):
        if path in files or path.is_symlink() or not path.is_file():
            continue
        config = ConfigFile(path)
        if is_managed_location(config.original):
            config.content = None
            files[path] = config
    return files


def run(argv):
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, encoding='utf-8',
                              errors='replace', env=dict(os.environ, LC_ALL='C'), timeout=120)
    except subprocess.TimeoutExpired as error:
        raise ValueError('{} timed out'.format(' '.join(argv))) from error


def command(argv):
    # Keep command failures inside the transaction so files can be restored.
    result = run(argv)
    if result.returncode:
        raise ValueError('{} failed ({}): {}'.format(
            ' '.join(argv), result.returncode, result.stderr.strip() or result.stdout.strip()))
    return result.stdout, result.stderr


def check_web_configurations(versions, nginx_files, options):
    for binary, config in versions.values():
        command([str(binary), '-t', '-y', str(config)])
    loaded, _ = command([options['nginx_binary'], '-T', '-c', options['nginx_main_config']])
    lines = set(loaded.splitlines())
    for path, config in nginx_files.items():
        if config.content is not None and '# configuration file {}:'.format(path) not in lines:
            raise ValueError('nginx does not include {}'.format(path))


def check_zabbix_discovery(key, options):
    output, _ = command([options['zabbix_binary'], '-c', options['zabbix_config'], '-t', key])
    value = re.search(r'\[(?:t|s)\|(.*)\]\s*$', output, re.DOTALL)
    if not value or json.loads(value.group(1)) != options['discovery']:
        raise ValueError('Zabbix Agent 2 did not return the expected {} JSON'.format(key))


def unavailable_endpoints(pools, address, port):
    endpoints = [('/nginx-status', r'^Active connections:')]
    for pool in pools:
        name = pool['name']
        endpoints.append((pool['uri'], r'^pool:[ \t]+' + re.escape(name) + r'[ \t]*$'))
    unavailable = []
    for uri, expected in endpoints:
        connection = http.client.HTTPConnection(address, port, timeout=2)
        try:
            connection.request('GET', uri, headers={'Host': 'localhost'})
            response = connection.getresponse()
            if response.status != 200 or not re.search(expected, response.read(4096).decode(errors='replace'), re.MULTILINE):
                unavailable.append('{} (HTTP {})'.format(uri, response.status))
        except (OSError, http.client.HTTPException) as error:
            unavailable.append('{} ({})'.format(uri, error))
        finally:
            connection.close()
    return unavailable


def is_active(service):
    return run(['systemctl', 'is-active', '--quiet', service]).returncode == 0


def apply_configuration(versions, pools, php_files, nginx_files, zabbix_files, options, discovery_key):
    applied = []
    activated = {}
    created_locations = False
    locations_dir = Path(options['nginx_locations_dir'])
    nginx_service = options['nginx_service']
    zabbix_service = options['zabbix_service']
    bind_address = options['bind_address']
    address = str(bind_address)
    if bind_address.is_unspecified:
        address = '::1' if bind_address.version == 6 else '127.0.0.1'
    port = options['port']
    web_files = [*php_files, *nginx_files.values()]

    def apply(files):
        for config in files:
            if config.changed:
                config.apply()
                applied.append(config)

    def activate(service, action):
        activated[service] = is_active(service)
        command(['systemctl', action, service])

    web_changed = any(config.changed for config in web_files)
    zabbix_changed = any(config.changed for config in zabbix_files)
    try:
        if not locations_dir.is_dir():
            locations_dir.mkdir(mode=0o755)
            created_locations = True
        apply(web_files)
        check_web_configurations(versions, nginx_files, options)

        # A busy pool can time out on the shared listener even when its configuration is correct.
        dedicated_pools = [pool for pool in pools if 'socket' in pool]
        if web_changed or unavailable_endpoints(dedicated_pools, address, port):
            for version in versions:
                activate(options['php_service'].replace('{version}', version), 'restart')
            activate(nginx_service, 'reload-or-restart')
            deadline = time.monotonic() + 15
            failures = unavailable_endpoints(pools, address, port)
            while failures and time.monotonic() < deadline:
                time.sleep(0.5)
                failures = unavailable_endpoints(pools, address, port)
            if failures:
                raise ValueError('Status endpoints did not come up: {}'.format(', '.join(failures)))

        apply(zabbix_files)
        check_zabbix_discovery(discovery_key, options)
        if zabbix_changed or not is_active(zabbix_service):
            activate(zabbix_service, 'restart')
    except (OSError, ValueError) as error:
        recovery_errors = []
        for config in reversed(applied):
            try:
                config.restore()
            except (OSError, ValueError) as recovery_error:
                recovery_errors.append(str(recovery_error))
        if created_locations:
            try:
                locations_dir.rmdir()
            except OSError as recovery_error:
                recovery_errors.append(str(recovery_error))
        for service, was_active in activated.items():
            action = 'stop' if not was_active else ('reload-or-restart' if service == nginx_service else 'restart')
            try:
                command(['systemctl', action, service])
            except (OSError, ValueError) as recovery_error:
                recovery_errors.append(str(recovery_error))
        if recovery_errors:
            raise ValueError('{}; rollback errors: {}'.format(error, '; '.join(recovery_errors))) from error
        raise
    return list(activated)


def configure(module):
    options = module.params
    if options['operation'] == 'apply':
        if not 1 <= options['port'] <= 65535:
            raise ValueError('The status port must be between 1 and 65535')
        options = dict(options, bind_address=ipaddress.ip_address(options['bind_address']))
    for key in ('pool_uri_prefix', 'socket_dir'):
        path = options[key].rstrip('/')
        if not re.fullmatch(r'(?:/[A-Za-z0-9_~.-]+)+', path) or {'.', '..'}.intersection(path.split('/')):
            raise ValueError('Invalid {}: {!r}'.format(key, options[key]))

    versions, php_files, pools = discover_php(options)
    if options['operation'] == 'discover':
        return {'changed': False, 'pools': pools}
    if pools != options['pools']:
        raise ValueError('The PHP-FPM pool list changed while templates were rendered. Run the playbook again.')

    nginx_files = nginx_status_files(options)
    discovery = options['discovery']
    discovery_path = Path(options['discovery_file'])
    if not discovery_path.is_absolute():
        raise ValueError('The Zabbix discovery file path must be absolute')
    discovery_file = ConfigFile(discovery_path, mode=0o644)
    discovery_file.content = json.dumps(discovery, indent=2) + '\n'
    agent = ConfigFile(Path(options['zabbix_userparameter_file']))
    userparameter = options['zabbix_userparameter']
    item = re.fullmatch(r'UserParameter\s*=\s*([^,\s]+)\s*,[^\r\n]+', userparameter)
    if not item:
        raise ValueError('Expected a single Zabbix UserParameter line with the discovery key and command')
    discovery_key = item.group(1)
    agent.content = zabbix_status_config(agent.original or '', discovery_key, userparameter)
    web_files = [*php_files, *nginx_files.values()]
    zabbix_files = [discovery_file, agent]
    changed_files = [str(config.path) for config in web_files + zabbix_files if config.changed]
    services = []
    if not module.check_mode:
        services = apply_configuration(versions, pools, php_files, nginx_files, zabbix_files, options, discovery_key)
    return {'changed': bool(changed_files or services), 'files': changed_files, 'services': services, 'pools': discovery}


def main():
    module = AnsibleModule(
        argument_spec={
            'operation': {'type': 'str', 'required': True, 'choices': ['discover', 'apply']},
            'bind_address': {'type': 'str'},
            'port': {'type': 'int'},
            'worker_user': {'type': 'str'},
            'pool_uri_prefix': {'type': 'str', 'required': True},
            'socket_dir': {'type': 'path', 'required': True},
            'nginx_main_config': {'type': 'path', 'required': True},
            'nginx_binary': {'type': 'str', 'required': True},
            'php_dir': {'type': 'path', 'required': True},
            'php_bin_dir': {'type': 'path', 'required': True},
            'php_binary_prefix': {'type': 'str', 'required': True},
            'pools': {'type': 'list', 'elements': 'dict'},
            'nginx_config': {'type': 'str'},
            'nginx_locations': {'type': 'dict'},
            'nginx_default_vhost': {'type': 'path'},
            'nginx_locations_dir': {'type': 'path'},
            'nginx_service': {'type': 'str'},
            'php_service': {'type': 'str'},
            'discovery_file': {'type': 'path'},
            'discovery': {'type': 'list', 'elements': 'dict'},
            'zabbix_config': {'type': 'path'},
            'zabbix_userparameter_file': {'type': 'path'},
            'zabbix_binary': {'type': 'str'},
            'zabbix_service': {'type': 'str'},
            'zabbix_userparameter': {'type': 'str'},
            'lock_file': {'type': 'path'},
        },
        required_if=[
            ('operation', 'apply', [
                'bind_address', 'port', 'pools', 'nginx_config', 'nginx_locations',
                'nginx_default_vhost', 'nginx_locations_dir', 'nginx_service', 'php_service',
                'discovery_file', 'discovery', 'zabbix_userparameter', 'zabbix_config',
                'zabbix_userparameter_file', 'zabbix_binary', 'zabbix_service', 'lock_file',
            ]),
        ],
        supports_check_mode=True,
    )
    try:
        if module.check_mode or module.params['operation'] == 'discover':
            result = configure(module)
        else:
            with Path(module.params['lock_file']).open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = configure(module)
    except BlockingIOError:
        module.fail_json(msg='Another nginx/PHP-FPM status playbook run is in progress')
    except (OSError, ValueError) as error:
        module.fail_json(msg=str(error))
    module.exit_json(**result)


if __name__ == '__main__':
    main()
