#cloud-config
# Шаблон: create-vm.sh подставляет __VM_USER__ и __SSH_PUBKEY__.
users:
  - name: __VM_USER__
    groups: sudo
    shell: /bin/bash
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    ssh_authorized_keys:
      - __SSH_PUBKEY__

package_update: true
packages:
  - ca-certificates
  - curl
  - rsync
  - docker.io
  - docker-compose-v2

write_files:
  # Зеркало Docker Hub: если hub недоступен из РФ, образы тянутся через него.
  - path: /etc/docker/daemon.json
    content: |
      {"registry-mirrors": ["https://mirror.gcr.io"]}

runcmd:
  - usermod -aG docker __VM_USER__
  - systemctl enable docker
  - systemctl restart docker
  - mkdir -p /home/__VM_USER__/instrument /home/__VM_USER__/backups
  - chown -R __VM_USER__:__VM_USER__ /home/__VM_USER__/instrument /home/__VM_USER__/backups
