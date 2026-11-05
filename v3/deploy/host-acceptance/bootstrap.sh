#!/bin/sh
# Disposable, network-isolated QEMU guest only; never install this as a production init.
set -eu
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
if [ ! -f /var/lib/mgar-acceptance/bootstrap.done ]; then
    chown root:root /
    chmod 755 /
    # Host extraction is unprivileged; normalize guest ownership before creating secrets.
    find /usr /etc /var /opt /root /boot -xdev -exec chown -h root:root {} +
    mkdir -p /mnt/mgar-install /opt/mgar /var/lib/mgar-acceptance /etc/mgar /etc/mgar-watchdog
    modprobe 9pnet_virtio
    mount -t 9p -o trans=virtio,version=9p2000.L,ro mgar-src /mnt/mgar-install
    cp -a /mnt/mgar-install/python /opt/python
    cp -a /mnt/mgar-install/source/v3 /opt/mgar/v3
    cp -a /mnt/mgar-install/venv /opt/mgar/v3/.venv
    cp -a /mnt/mgar-install/private /var/lib/mgar-acceptance/install-private
    chown -R root:root /opt /var/lib/mgar-acceptance
    chmod -R u+rwX,go+rX /opt
    chmod -R go-rwx /var/lib/mgar-acceptance
    useradd --system --uid 2000 --home /opt/mgar --shell /usr/sbin/nologin mgar
    useradd --system --uid 2001 --home /nonexistent --shell /usr/sbin/nologin mgar-watchdog
    mkdir -p /opt/mgar/v3/.local /var/lib/postgresql/mgar
    chown mgar:mgar /opt/mgar/v3/.local
    chmod 700 /opt/mgar/v3/.local
    chown postgres:postgres /var/lib/postgresql/mgar
    chmod 700 /var/lib/postgresql/mgar
    runuser -u postgres -- /usr/lib/postgresql/16/bin/initdb -D /var/lib/postgresql/mgar --encoding=UTF8 --locale=C.UTF-8 --auth-local=peer --auth-host=scram-sha-256 > /var/lib/mgar-acceptance/initdb.log 2>&1
    printf "\nlisten_addresses='127.0.0.1'\nexternal_pid_file='/run/mgar-postgres/postmaster.pid'\nfsync=on\nsynchronous_commit=on\nfull_page_writes=on\n" >> /var/lib/postgresql/mgar/postgresql.conf
    chown root:redis /etc/redis/redis.conf
    chown -R redis:redis /var/lib/redis /var/log/redis
    mkdir -p /lib/modules/6.1.0-54-amd64/kernel/fs/overlayfs
    cp /mnt/mgar-install/kernel-modules/overlay.ko /lib/modules/6.1.0-54-amd64/kernel/fs/overlayfs/overlay.ko
    depmod -a
    cp /mnt/mgar-install/docker/* /usr/local/bin/
    chmod 755 /usr/local/bin/docker* /usr/local/bin/containerd* /usr/local/bin/runc
    groupadd -f docker
    usermod -aG docker mgar
    cp /mnt/mgar-install/sandbox-image.tar.gz /var/lib/mgar-acceptance/sandbox-image.tar.gz
    cp /opt/mgar/v3/deploy/host-acceptance/mgar-docker.service /etc/systemd/system/
    cp /opt/mgar/v3/deploy/host-acceptance/mgar-postgres.service /etc/systemd/system/
    cp /opt/mgar/v3/deploy/host-acceptance/mgar-prepare.service /etc/systemd/system/
    cp /opt/mgar/v3/deploy/systemd/mgar-v3.service /etc/systemd/system/
    cp /opt/mgar/v3/deploy/systemd/mgar-v3-watchdog.service /etc/systemd/system/
    mkdir -p /etc/systemd/system/mgar-v3.service.d /etc/systemd/system/serial-getty@ttyS0.service.d
    printf '[Unit]\nAfter=mgar-prepare.service redis-server.service\nRequires=mgar-prepare.service\nWants=redis-server.service\n' > /etc/systemd/system/mgar-v3.service.d/local-database.conf
    printf '[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin root --noclear %%I 115200 vt100\n' > /etc/systemd/system/serial-getty@ttyS0.service.d/console.conf
    cp /var/lib/mgar-acceptance/install-private/server.env /etc/mgar/server.env
    cp /var/lib/mgar-acceptance/install-private/client.json /etc/mgar-watchdog/client.json
    chmod 600 /etc/mgar/server.env /etc/mgar-watchdog/client.json
    chown mgar-watchdog:mgar-watchdog /etc/mgar-watchdog/client.json
    chmod 755 /etc/mgar-watchdog
    printf 'mgar-r6-acceptance\n' > /etc/hostname
    : > /etc/machine-id
    printf '/dev/vda / ext4 defaults 0 1\n' > /etc/fstab
    systemctl enable mgar-v3.service mgar-v3-watchdog.service mgar-postgres.service mgar-docker.service redis-server.service serial-getty@ttyS0.service
    touch /var/lib/mgar-acceptance/bootstrap.done
    sync
    umount /mnt/mgar-install
fi
ip link set lo up
exec /sbin/init
