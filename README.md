# sunfoxcz.server-status

An Ansible role that configures local nginx and PHP-FPM status endpoints and
registers PHP-FPM pool discovery with Zabbix Agent 2.

- Configures the default nginx virtual host on `127.0.0.1:80`. It serves nginx
  `stub_status` at `/nginx-status` and returns 404 for unmatched requests.
  Set `nginx_php_fpm_status_bind_address` and `nginx_php_fpm_status_port` to
  change the listener.
- Discovers PHP-FPM pools and serves each pool's status through nginx at
  `/fpm/<version>/<pool>` by default.
- Makes the pool list available through the Zabbix item key `php-fpm.pools.discovery`.

## PHP-FPM status transport

PHP 8.0 and later use `pm.status_listen` to create a dedicated Unix socket for
each pool. PHP-FPM handles status requests independently of application
workers, so metrics remain available when those workers are busy. A Unix
socket keeps the connection between nginx and PHP-FPM local and controls
access through filesystem permissions.

PHP 7.4 and earlier support `pm.status_path`. nginx uses the pool's existing
`listen` address, whether it is a Unix socket or a TCP listener. Status
requests share the application's worker queue and may time out when all
workers are busy.

## Default paths on the target host

| Path | Purpose |
| --- | --- |
| `/etc/nginx/conf.d/default.conf` | Default nginx virtual host with a 404 fallback and an include for location files. |
| `/etc/nginx/default-vhost.d/nginx-status.conf` | nginx `stub_status` location. |
| `/etc/nginx/default-vhost.d/php<version>-<pool>.conf` | PHP-FPM status location for one pool in one PHP version. |
| `/etc/php/<version>/fpm/pool.d/*.conf` | Existing pool configurations updated with `pm.status_path` and, on PHP 8.0 or later, `pm.status_listen`. |
| `/run/php/php<version>-<pool>-status.sock` | Default path for a dedicated status socket on PHP 8.0 or later. |
| `/etc/zabbix/php-fpm-pools.json` | Default path for the Zabbix pool discovery JSON. |
| `/etc/zabbix/zabbix_agent2.d/php-fpm.conf` | Zabbix Agent 2 configuration for the discovery key. |
| `/run/lock/ansible-nginx-php-fpm-status.lock` | Lock file that prevents concurrent configuration runs. |

Other roles can add their own `.conf` files to `/etc/nginx/default-vhost.d/`
using distinct filenames.

## Requirements

Run the role with `become: true` on a systemd host with nginx and Zabbix Agent 2
already installed. The role validates configuration, reloads or restarts
services as needed, and attempts to roll back changes if application fails.
See [defaults/main.yml](defaults/main.yml) for paths, executables, and systemd
unit names. The PHP-FPM unit name pattern replaces `{version}` with the
installed PHP major and minor version. Set `ansible_managed` to customize
the managed-file comment.
