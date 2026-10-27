#!/usr/bin/env bash
# 仅用于经确认后从快照创建的新副本的 Lightsail launch script。
# 新副本的云防火墙必须限制 SSH 为管理 IP 与 Lightsail browser SSH。
set -euo pipefail
umask 077
backup_dir="/var/backups/ala-ssh-recovery-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$backup_dir"
cp -a /etc/ufw "$backup_dir/ufw"
hostname > "$backup_dir/hostname.txt"
ufw status verbose > "$backup_dir/ufw-before.txt"
# 调用前设置 ADMIN_SSH_CIDRS，例如 "203.0.113.10/32"，不在仓库记录真实管理出口。
# 先仅恢复指定出口的密钥登录。登录后另行核验并配置浏览器备用通道。
# 不修改密钥、SSH 认证方式或其他端口，不清空现有防火墙。
read -r -a management_cidrs <<< "${ADMIN_SSH_CIDRS:?Set the approved management IP/CIDR list first}"
for cidr in "${management_cidrs[@]}"; do
    ufw allow from "$cidr" to any port 22 proto tcp comment 'Approved management IP recovery'
done
ufw status verbose > "$backup_dir/ufw-after.txt"
sshd -t
printf 'SSH recovery completed on %s\n' "$(hostname)" > /var/log/ala-ssh-recovery.log
