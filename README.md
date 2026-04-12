# OpenClaw Manager

A lightweight multi-user OpenClaw instance management platform.

一个轻量级的 OpenClaw 多用户实例管理平台。

---

## 🚀 Overview | 项目简介

OpenClaw Manager 提供一种“每用户一个实例”的部署模式，在不修改 OpenClaw 源码的前提下，实现：

- 多用户隔离
- 自动化部署
- 生命周期管理（创建 / 删除 / 恢复）
- 运维可控

适用于：

- 高校 / 数据中心
- 内部 AI 平台
- 私有化部署环境

---

## ✨ Features | 功能特点

### 🧩 实例隔离

- 每用户独立 OpenClaw 容器
- 无共享状态，避免权限污染

---

### ⚙️ 用户生命周期管理

- 创建用户：create_user.sh
- 删除用户（回收站）：delete_user.sh
- 恢复用户：restore_user.sh
- 用户列表：list_users.sh

完整流程：

创建 → 使用 → 删除（回收）→ 恢复

---

### 🌐 端口访问模型（核心设计）

每个用户绑定一个端口：

userA → http://IP:30000  
userB → http://IP:30001  

特点：

- 不使用子路径（避免 WebSocket 问题）
- 不依赖域名
- 简单稳定

---

### 🔐 安全机制

- 容器隔离
- OpenClaw Token 认证
- 可选 Nginx Basic Auth

---

### ♻️ 安全删除机制

删除用户不会直接删除数据：

users/testuser  
→ deleted/testuser_时间戳  

支持恢复。

---

## 📁 Project Structure | 项目结构

openclaw-manager/
├── templates/              # docker-compose 模板
├── scripts/                # 用户管理脚本
│   ├── create_user.sh
│   ├── delete_user.sh
│   ├── restore_user.sh
│   └── list_users.sh
├── services/               # 扩展服务（如 pdf-extract）
├── nginx/                  # 可选反向代理配置
├── logs/
├── docs/
├── README.md
└── .gitignore

---

## 📁 Runtime Structure | 运行时目录（重要）

运行时数据在：

/data/docker/openclaw-public/

结构如下：

/data/docker/openclaw-public/
├── users/              # 用户实例
│   ├── userA/
│   ├── userB/
├── deleted/            # 回收站
├── ports.txt           # 端口分配记录
├── logs/               # 脚本日志

说明：

- users/：运行中的用户
- deleted/：已删除但可恢复
- ports.txt：当前端口指针（单向递增）

---

## 🧠 Architecture | 架构说明

User Browser  
↓  
http://IP:PORT  
↓  
Docker（每用户一个容器）  
↓  
OpenClaw Gateway（18789）  

设计原则：

- 不使用子路径
- 不依赖子域名
- 使用端口隔离
- 保持 OpenClaw 原生行为

---

## ⚙️ Usage | 使用方式

### 1️⃣ 创建用户

./scripts/create_user.sh <user_id>

输出：

SUCCESS  
User: testuser  
Port: 30000  
Access URL:  
http://<服务器IP>:30000  

---

### 2️⃣ 查看用户列表

./scripts/list_users.sh

---

### 3️⃣ 删除用户（温和删除）

./scripts/delete_user.sh <user_id>

---

### 4️⃣ 恢复用户

./scripts/restore_user.sh <user_id>

---

## ⚠️ Important Notes | 注意事项

### 🚨 端口策略

端口单向递增，不回收：

30000 → 30001 → 30002 …

优点：

- 避免冲突
- 简化管理
- 提高稳定性

---

### 🚨 数据安全

- 禁止手动删除 users 目录
- 必须通过脚本管理

---

### 🚨 子路径限制

不支持：

https://domain/testuser  

原因：

- OpenClaw 不支持 basePath
- WebSocket 会断开（1006）

---

## 🔐 Security Considerations | 安全说明

建议：

- 限制公网访问
- 使用 Token
- 控制插件来源
- 定期清理实例

---

## 🛠 Roadmap | 后续规划

- [x] 用户生命周期管理
- [x] 端口分配系统
- [x] 安全删除与恢复
- [ ] 端口加锁（并发安全）
- [ ] Web 管理界面
- [ ] 配额控制
- [ ] OAuth2 / SSO

---

## 📌 License

MIT

---

## 👤 Author

Maintained for internal multi-user OpenClaw deployment.

面向高校 / 数据中心 / 私有化部署场景设计。
