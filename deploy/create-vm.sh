#!/usr/bin/env bash
# Создаёт в Yandex Cloud сеть, подсеть, группу безопасности и ВМ с Docker.
# Требуется: установленный и настроенный `yc` (yc init), jq, ssh-ключ.
# Повторный запуск безопасен: существующие ресурсы не пересоздаются.
set -euo pipefail

VM_NAME="${VM_NAME:-instrument}"
ZONE="${ZONE:-ru-central1-a}"
NETWORK="${NETWORK:-instrument-net}"
SUBNET="${SUBNET:-instrument-subnet-a}"
SUBNET_RANGE="${SUBNET_RANGE:-10.10.0.0/24}"
SG="${SG:-instrument-sg}"
SSH_ALLOW_CIDR="${SSH_ALLOW_CIDR:-0.0.0.0/0}"   # лучше свой IP: 203.0.113.10/32
VM_USER="${VM_USER:-deploy}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519.pub}"
IMAGE_FAMILY="${IMAGE_FAMILY:-ubuntu-2204-lts}"
CORES="${CORES:-2}"
MEMORY="${MEMORY:-4}"            # ГБ
CORE_FRACTION="${CORE_FRACTION:-50}"
DISK_SIZE="${DISK_SIZE:-20}"     # ГБ

cd "$(dirname "$0")"

for bin in yc jq; do
  command -v "$bin" >/dev/null || { echo "Не найден $bin" >&2; exit 1; }
done
[[ -f "$SSH_KEY" ]] || { echo "Нет публичного ключа $SSH_KEY (ssh-keygen -t ed25519)" >&2; exit 1; }

echo "==> Сеть $NETWORK"
yc vpc network get --name "$NETWORK" >/dev/null 2>&1 \
  || yc vpc network create --name "$NETWORK"

echo "==> Подсеть $SUBNET ($ZONE, $SUBNET_RANGE)"
yc vpc subnet get --name "$SUBNET" >/dev/null 2>&1 \
  || yc vpc subnet create --name "$SUBNET" --zone "$ZONE" \
       --range "$SUBNET_RANGE" --network-name "$NETWORK"

echo "==> Группа безопасности $SG (входящие 22/tcp, 80/tcp)"
yc vpc security-group get --name "$SG" >/dev/null 2>&1 \
  || yc vpc security-group create --name "$SG" --network-name "$NETWORK" \
       --rule "direction=ingress,port=22,protocol=tcp,v4-cidrs=[$SSH_ALLOW_CIDR]" \
       --rule "direction=ingress,port=80,protocol=tcp,v4-cidrs=[0.0.0.0/0]" \
       --rule "direction=egress,protocol=any,v4-cidrs=[0.0.0.0/0]"
SG_ID="$(yc vpc security-group get --name "$SG" --format json | jq -r .id)"

if yc compute instance get --name "$VM_NAME" >/dev/null 2>&1; then
  echo "==> ВМ $VM_NAME уже существует"
else
  echo "==> Создаю ВМ $VM_NAME"
  USER_DATA="$(mktemp)"
  trap 'rm -f "$USER_DATA"' EXIT
  sed -e "s|__VM_USER__|$VM_USER|g" \
      -e "s|__SSH_PUBKEY__|$(cat "$SSH_KEY")|g" \
      cloud-init.yaml.tpl > "$USER_DATA"

  yc compute instance create \
    --name "$VM_NAME" \
    --zone "$ZONE" \
    --platform standard-v3 \
    --cores "$CORES" --memory "$MEMORY" --core-fraction "$CORE_FRACTION" \
    --create-boot-disk "image-folder-id=standard-images,image-family=$IMAGE_FAMILY,size=$DISK_SIZE,type=network-ssd" \
    --network-interface "subnet-name=$SUBNET,nat-ip-version=ipv4,security-group-ids=$SG_ID" \
    --metadata-from-file "user-data=$USER_DATA"
fi

IP="$(yc compute instance get --name "$VM_NAME" --format json \
  | jq -r '.network_interfaces[0].primary_v4_address.one_to_one_nat.address')"

echo
echo "Готово. Публичный IP: $IP"
echo "cloud-init ставит Docker ~2-3 минуты. Проверка:"
echo "  ssh $VM_USER@$IP 'cloud-init status --wait && docker compose version'"
echo "Дальше деплой:"
echo "  VM_HOST=$IP bash deploy/deploy.sh"
